"""本机仪表盘：任务轨迹与带同源校验的 HTTP 控制服务。"""

import asyncio
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hmac
from pathlib import Path
from threading import RLock, Thread
import time
from urllib.parse import urlsplit, parse_qs
import uuid
import secrets
import os


WEB_ROOT = Path(__file__).with_name("web")
TERMINAL = {"sent", "skipped", "error", "cancelled"}


def local_token(root):
    path = Path(root) / ".local/bot-api-token"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as stream:
        stream.write(token)
    return token


class Telemetry:
    def __init__(self):
        self.lock = RLock()
        self.started_at = time.time()
        self.traces = deque(maxlen=20)
        self.events = deque(maxlen=60)
        self.received = 0
        self.sent = 0
        self.current_id = None
        self.connection = "connecting"
        self.ollama = {"state": "checking", "loaded": False}

    def event(self, kind, text):
        with self.lock:
            self.events.appendleft({"id": uuid.uuid4().hex, "time": time.time(), "kind": kind, "text": text})

    def receive(self, nick, mode):
        with self.lock:
            self.received += 1
            self.event("message", f"{nick} · " + ("准备回复" if mode else "已存入消息池"))

    def queued(self, job, nick):
        with self.lock:
            trace_id = job.setdefault("trace_id", uuid.uuid4().hex)
            if self._find(job):
                return
            self.traces.appendleft({"id": trace_id, "uid": job["uid"], "nick": nick,
                                    "input": (job.get("target") or {}).get("text", "") or job.get("say", ""),
                                    "mode": job["mode"], "stage": "queued", "status": "queued",
                                    "queued_at": time.time(), "started_at": None, "ended_at": None,
                                    "thinking": "", "draft": "", "answer": "", "prompt": "", "attempt": 1,
                                    "steps": [{"stage": "queued", "time": time.time(), "note": "消息已入队"}]})

    def _find(self, job):
        return next((t for t in self.traces if t["id"] == job.get("trace_id")), None)

    def stage(self, job, stage, note="", **fields):
        with self.lock:
            trace = self._find(job)
            if not trace or trace["status"] in TERMINAL:
                return
            if trace["stage"] != stage or note:
                trace["steps"].append({"stage": stage, "time": time.time(), "note": note})
            trace.update(fields, stage=stage, status="active")
            if trace["started_at"] is None:
                trace["started_at"] = time.time()
            self.current_id = trace["id"]

    def chunk(self, job, part, attempt):
        """由推理线程调用；不触碰 asyncio 对象或群消息池。"""
        with self.lock:
            trace = self._find(job)
            if not trace or trace["status"] in TERMINAL or trace["attempt"] != attempt:
                return
            message = part.get("message", {})
            for key, field, stage in (("thinking", "thinking", "thinking"), ("content", "draft", "generating")):
                delta = message.get(key, "")
                if delta:
                    self.stage(job, stage)
                    trace[field] = (trace[field] + delta)[:24576]
            if part.get("done"):
                trace["metrics"] = {k: part[k] for k in (
                    "prompt_eval_count", "eval_count", "total_duration", "eval_duration", "done_reason") if k in part}

    def finish(self, job, status, note="", **fields):
        with self.lock:
            trace = self._find(job)
            if not trace or trace["status"] in TERMINAL:
                return
            trace.update(fields, stage=status, status=status, ended_at=time.time())
            trace["steps"].append({"stage": status, "time": time.time(), "note": note})
            if status == "sent":
                self.sent += 1
            self.event(status, note or status)
            if self.current_id == trace["id"]:
                self.current_id = None

    def cancel_all(self, note):
        with self.lock:
            for trace in list(self.traces):
                if trace["status"] not in TERMINAL:
                    self.finish({"trace_id": trace["id"]}, "cancelled", note)

    def clear(self):
        with self.lock:
            self.traces.clear()
            self.events.clear()
            self.current_id = None
            self.event("reset", "管理员已清空上下文与个人消息池")

    def snapshot(self):
        with self.lock:
            return deepcopy({"started_at": self.started_at, "received": self.received, "sent": self.sent,
                             "current_id": self.current_id, "connection": self.connection,
                             "ollama": self.ollama, "traces": list(self.traces), "events": list(self.events)})


class DashboardServer:
    def __init__(self, loop, snapshot, port=8765, *, commands=None, api_get=None,
                 auth_token=None, csrf_token=None, serve_ui=True):
        async def get_snapshot():
            return snapshot()

        async def dispatch(action, data):
            return commands(action, data)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def permitted(self):
                # 禁止经其他域名读取群聊数据；不启用跨域访问。
                allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
                if self.headers.get("Host") not in allowed:
                    self.respond(403, b"Forbidden", "text/plain")
                    return False
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://{h}" for h in allowed}:
                    self.respond(403, b"Forbidden", "text/plain")
                    return False
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self.respond(403, b"Forbidden", "text/plain")
                    return False
                if auth_token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + auth_token):
                    self.respond(403, b"Forbidden", "text/plain")
                    return False
                return True

            def do_GET(self):
                if not self.permitted():
                    return
                path = urlsplit(self.path).path
                if path == "/api/state":
                    future = asyncio.run_coroutine_threadsafe(get_snapshot(), loop)
                    try:
                        body = json.dumps(future.result(timeout=3), ensure_ascii=False).encode("utf-8")
                        self.respond(200, body, "application/json; charset=utf-8")
                    except Exception:
                        future.cancel()
                        self.respond(503, b'{"error":"Bot state unavailable"}', "application/json")
                    return
                if api_get and path.startswith("/api/"):
                    try:
                        result = api_get(path, parse_qs(urlsplit(self.path).query))
                        if isinstance(result, tuple):
                            body, content_type, headers = result
                            self.respond(200, body, content_type, headers)
                        else:
                            self.respond(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                    except (ValueError, KeyError) as exc:
                        self.respond(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), "application/json")
                    except Exception:
                        self.respond(503, b'{"error":"Service temporarily unavailable"}', "application/json")
                    return
                if not serve_ui:
                    self.respond(404, b"Not found", "text/plain")
                    return
                files = {"/": ("index.html", "text/html; charset=utf-8"),
                         "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                         "/management.js": ("management.js", "text/javascript; charset=utf-8"),
                         "/style.css": ("style.css", "text/css; charset=utf-8")}
                if path not in files:
                    self.respond(404, b"Not found", "text/plain")
                    return
                filename, content_type = files[path]
                self.respond(200, (WEB_ROOT / filename).read_bytes(), content_type)

            def do_POST(self):
                if not self.permitted():
                    return
                if not commands or urlsplit(self.path).path != "/api/action":
                    self.respond(404, b"Not found", "text/plain")
                    return
                if csrf_token and not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                    self.respond(403, b'{"error":"Invalid request token"}', "application/json")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 262144:
                        raise ValueError("请求内容为空或过大")
                    data = json.loads(self.rfile.read(length))
                    if not isinstance(data, dict) or not isinstance(data.get("action"), str) or not isinstance(data.get("data", {}), dict):
                        raise ValueError("请求格式错误")
                    future = asyncio.run_coroutine_threadsafe(dispatch(data["action"], data.get("data", {})), loop)
                    result = future.result(timeout=5)
                    self.respond(200, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")
                except (ValueError, KeyError) as exc:
                    self.respond(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), "application/json")
                except Exception as exc:
                    self.respond(503, json.dumps({"error": str(exc) or "服务暂不可用"}, ensure_ascii=False).encode(), "application/json")

            def respond(self, status, body, content_type, headers=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src http://127.0.0.1:6099; frame-ancestors 'none'")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = Thread(target=self.server.serve_forever, name="dashboard", daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
