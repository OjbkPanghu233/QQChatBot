"""独立网页管理台。组件停止后管理台继续运行；所有控制命令均为固定白名单。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
from threading import Event, RLock, Thread
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from dashboard import DashboardServer, local_token
import manage
from ollama_client import chat

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".local/config.json"
LOG = logging.getLogger("web_console")
FIELDS = {"model", "group_name", "group_id", "command_user_id", "onebot_url", "ollama_url", "think",
          "num_ctx", "num_predict", "group_log_size", "prompt_log_chars", "model_timeout_seconds",
          "chime_queue_timeout_seconds", "chime_probability", "chime_cooldown", "ignore_user_ids"}
LIMITS = {"group_id": (1, 10**16), "num_ctx": (2048, 32768), "num_predict": (64, 8192),
          "group_log_size": (1, 200), "prompt_log_chars": (100, 16000),
          "model_timeout_seconds": (10, 900), "chime_queue_timeout_seconds": (1, 300), "chime_cooldown": (0, 3600)}


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return deepcopy(fallback)


def atomic_write(path, text):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def validate_config(data, current):
    if not isinstance(data, dict) or set(data) - FIELDS - {"onebot_token", "clear_onebot_token"}:
        raise ValueError("配置包含不支持的字段")
    result = deepcopy(current)
    for key, value in data.items():
        if key in LIMITS:
            lower, upper = LIMITS[key]
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{key} 必须是 {lower}～{upper} 的整数")
        elif key in ("think", "clear_onebot_token"):
            if type(value) is not bool:
                raise ValueError(key + " 必须是开关值")
        elif key == "chime_probability":
            if type(value) not in (int, float) or not 0 <= value <= 1:
                raise ValueError("插话概率必须在 0～100% 之间")
        elif key == "ignore_user_ids":
            if not isinstance(value, list) or len(value) > 100 or not all(str(x).isdigit() for x in value):
                raise ValueError("忽略账号应为 QQ 号列表")
            value = [str(x) for x in value]
        elif key in ("onebot_url", "ollama_url"):
            parts = urlsplit(value) if isinstance(value, str) else None
            schemes = ("ws", "wss") if key == "onebot_url" else ("http", "https")
            if not parts or parts.scheme not in schemes or parts.hostname not in ("127.0.0.1", "localhost") or parts.username:
                raise ValueError("连接地址应为本机服务地址")
        elif not isinstance(value, str) or len(value) > 512:
            raise ValueError(key + " 格式错误")
        if key == "model" and not re.fullmatch(r"[\w.:/-]{1,150}", value):
            raise ValueError("模型名格式错误")
        if key == "group_name" and not value.strip():
            raise ValueError("群名不能为空")
        if key == "command_user_id" and not value.isdigit():
            raise ValueError("指令管理员应为 QQ 号")
        if key not in ("clear_onebot_token", "onebot_token"):
            result[key] = value
    if data.get("clear_onebot_token"):
        result["onebot_token"] = ""
    elif data.get("onebot_token"):
        result["onebot_token"] = data["onebot_token"]
    if result.get("think"):
        result["no_think_prefill"] = False
    result["dashboard_enabled"] = True
    return result


class Console:
    ACTIONS = {"bot_start", "bot_stop", "bot_restart", "stack_start", "stack_stop", "ollama_start", "ollama_stop",
               "napcat_start", "napcat_stop", "napcat_restart", "model_load", "model_unload", "model_save", "model_build",
               "config_save", "connection_check", "clear_context", "clear_pool", "save_notes", "model_test"}

    def __init__(self):
        self.lock = RLock()
        self.csrf = secrets.token_urlsafe(32)
        self.bot_token = local_token(ROOT)
        self.started_at = time.time()
        self.cache = {}
        self.last_bot = {}
        self.operation = None
        self.preview = {"thinking": "", "answer": "", "text": "", "status": "idle"}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="web-operation")
        self.stop_event = Event()
        self.thread = Thread(target=self.refresh_loop, daemon=True, name="web-status")

    def config(self):
        return read_json(CONFIG, {})

    def bot_api(self, endpoint="/api/state", data=None):
        config = self.config()
        payload = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        req = Request(f"http://127.0.0.1:{config.get('bot_status_port', 8766)}" + endpoint, data=payload,
                      headers={"Authorization": "Bearer " + self.bot_token, "Content-Type": "application/json"})
        with urlopen(req, timeout=2) as response:
            return json.load(response)

    def snapshot(self):
        with self.lock:
            result = deepcopy(self.cache)
            if not result:
                result = self.offline_state(self.config())
            result.update(now=time.time(), csrf_token=self.csrf,
                          operation=deepcopy(self.operation), preview=deepcopy(self.preview), console_started_at=self.started_at)
            return result

    def offline_state(self, config):
        result = deepcopy(self.last_bot) if self.last_bot else {
            "started_at": self.started_at, "received": 0, "sent": 0, "traces": [], "events": [],
            "group_log": [], "queue_capacity": 8, "bot_name": "鲸鱼娘", "pool_capacity": 40}
        result.update(connected=False, stopping=False, current_id=None, queue_size=0, connection="stopped",
                      bot_running=False, profiles=read_json(ROOT / ".local/profiles.json", {}),
                      group_capacity=config.get("group_log_size", 50),
                      config={k: config.get(k) for k in FIELDS},
                      ollama={"state": "checking", "loaded": False}, models=[], services={"docker": False, "napcat": "unknown"})
        for trace in result["traces"]:
            if trace["status"] in ("active", "queued"):
                trace.update(status="cancelled", stage="cancelled", ended_at=time.time())
        return result

    def refresh_loop(self):
        count = 0
        components = {"docker": False, "napcat": "unknown"}
        while not self.stop_event.is_set():
            config = self.config()
            try:
                state = self.bot_api()
                state["bot_running"] = True
                self.last_bot = deepcopy(state)
            except Exception:
                state = self.offline_state(config)
            try:
                ps = manage.api(config["ollama_url"], "/api/ps")
                models = manage.api(config["ollama_url"], "/api/tags").get("models", [])
                model = next((m for m in ps.get("models", []) if m["name"] == config["model"]), {})
                state["ollama"] = {"state": "online", "loaded": bool(model), "context_length": model.get("context_length")}
                state["models"] = [{"name": m["name"], "size": m.get("size", 0)} for m in models]
            except Exception:
                state["ollama"] = {"state": "offline", "loaded": False}
                state["models"] = []
            if count % 4 == 0:
                components = {"docker": manage.docker_ready(), "napcat": "offline"}
                if components["docker"]:
                    try:
                        r = subprocess.run(manage.docker_command("inspect", "--format", "{{.State.Status}}", "napcat-napcat-1"),
                                           capture_output=True, text=True, timeout=5)
                        components["napcat"] = r.stdout.strip() if r.returncode == 0 else "not_created"
                    except Exception:
                        components["napcat"] = "unknown"
            state["services"] = deepcopy(components)
            state["config"] = {k: config.get(k) for k in FIELDS}
            state["config"].setdefault("num_ctx", 8192)
            with self.lock:
                self.cache = state
            count += 1
            self.stop_event.wait(.7)

    def command(self, args, timeout=120):
        proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
        if proc.returncode:
            raise RuntimeError((proc.stderr or proc.stdout or "组件操作失败")[-2000:])
        return proc.stdout[-3000:]

    def bot(self, action):
        return self.command([sys.executable, str(ROOT / "botctl.py"), action])

    def ensure_ollama(self):
        config = self.config()
        if not manage.ollama_ready(config):
            self.command([sys.executable, str(ROOT / "ollamactl.py"), "start"])
            manage.wait_for_service("Ollama", lambda: manage.ollama_ready(config))

    def ensure_napcat(self):
        if not manage.docker_ready():
            self.command(["open", "-a", "Docker"])
            manage.wait_for_service("Docker Desktop", manage.docker_ready)
        self.command(manage.compose_command("up", "--detach"))

    def submit(self, action, data):
        if action not in self.ACTIONS:
            raise ValueError("不支持的操作")
        if action == "config_save":
            validate_config(data.get("config"), self.config())
        if action in ("model_save", "model_build"):
            source = data.get("modelfile")
            if not isinstance(source, str) or not source.strip() or len(source) > 65536 or not re.search(r"(?m)^FROM\s+\S+", source):
                raise ValueError("Modelfile 需包含 FROM，且不超过 64KB")
        if action == "model_test" and (not isinstance(data.get("text"), str) or not 1 <= len(data["text"].strip()) <= 4000):
            raise ValueError("请输入 1～4000 字符的测试消息")
        with self.lock:
            if self.operation and self.operation["status"] in ("queued", "running"):
                raise ValueError("当前操作尚未完成，请稍后再试")
            self.operation = {"id": secrets.token_hex(8), "action": action, "status": "queued", "started_at": time.time(), "message": "等待执行"}
            result = deepcopy(self.operation)
        self.pool.submit(self.execute, action, deepcopy(data))
        return {"ok": True, "operation": result}

    def execute(self, action, data):
        with self.lock:
            self.operation.update(status="running", message="正在执行，请稍候…")
        try:
            config = self.config()
            message = "操作完成"
            if action in ("bot_start", "bot_restart", "stack_start"):
                self.ensure_ollama()
                self.ensure_napcat()
                models = manage.api(config["ollama_url"], "/api/tags").get("models", [])
                if not any(m["name"] == config["model"] for m in models):
                    raise ValueError("当前模型不存在，请先在模型设置中构建")
                self.bot("start")
                message = "机器人已启动，QQ 连接状态请看上方状态卡；未登录时请打开 QQ 登录面板。"
            elif action in ("bot_stop", "stack_stop", "ollama_stop", "napcat_stop"):
                self.bot("stop")
                if action in ("stack_stop", "napcat_stop") and manage.docker_ready():
                    self.command(manage.compose_command("stop"))
                if action == "ollama_stop":
                    self.command([sys.executable, str(ROOT / "ollamactl.py"), "stop"])
                message = "已停止所选组件，管理网页继续运行。"
            elif action == "ollama_start":
                self.ensure_ollama()
            elif action in ("napcat_start", "napcat_restart"):
                self.ensure_napcat()
                if action == "napcat_restart":
                    self.command(manage.compose_command("restart"))
            elif action in ("model_load", "model_unload"):
                self.ensure_ollama()
                self.ollama_post("/api/generate", {"model": config["model"], "stream": False,
                                 "keep_alive": "5m" if action == "model_load" else 0,
                                 "options": {"num_ctx": config.get("num_ctx", 8192)}})
                message = "模型已加载" if action == "model_load" else "模型已从内存卸载，文件仍保留；下一次消息会重新加载。"
            elif action == "config_save":
                new = validate_config(data["config"], config)
                self.backup_file(CONFIG, "config")
                atomic_write(CONFIG, json.dumps(new, ensure_ascii=False, indent=2) + "\n")
                if self.snapshot().get("bot_running"):
                    self.bot("start")
                message = "配置已保存；运行中的机器人已重启以应用设置。"
            elif action in ("model_save", "model_build"):
                self.backup_file(manage.MODELFILE, "model")
                atomic_write(manage.MODELFILE, data["modelfile"])
                if action == "model_build":
                    self.ensure_ollama()
                    self.command([sys.executable, str(ROOT / "manage.py"), "rebuild-model"], timeout=180)
                    message = "模型已按保存的人设与参数重新构建。"
                else:
                    message = "Modelfile 已保存；点击“保存并构建”后模型才会使用新内容。"
            elif action == "connection_check":
                message = self.command([sys.executable, str(ROOT / "qq_bot.py"), "--check"], timeout=45).strip()
            elif action in ("clear_context", "clear_pool", "save_notes"):
                if self.snapshot().get("bot_running"):
                    self.bot_api("/api/action", {"action": action, "data": data})
                else:
                    self.edit_offline_profiles(action, data)
                message = "消息池已更新" if action != "save_notes" else "群友备注已保存"
            elif action == "model_test":
                self.ensure_ollama()
                with self.lock:
                    self.preview = {"text": data["text"], "thinking": "", "answer": "", "status": "running"}
                def chunk(part):
                    m = part.get("message", {})
                    with self.lock:
                        self.preview["thinking"] = (self.preview["thinking"] + m.get("thinking", ""))[:24576]
                        self.preview["answer"] = (self.preview["answer"] + m.get("content", ""))[:24576]
                result = chat(data["text"], model=config["model"], prompt_path=None, base_url=config["ollama_url"],
                              think=config.get("think", True), no_think_prefill=config.get("no_think_prefill", False),
                              num_ctx=config.get("num_ctx", 8192), num_predict=config.get("num_predict", 2048),
                              timeout=config.get("model_timeout_seconds", 300), on_chunk=chunk)
                with self.lock:
                    self.preview.update(answer=result, status="done")
                message = "本地试聊完成，未向 QQ 发送消息。"
            with self.lock:
                self.operation.update(status="done", message=message, ended_at=time.time())
            LOG.info("网页操作成功：%s", action)
        except Exception as exc:
            with self.lock:
                self.operation.update(status="error", message=str(exc), ended_at=time.time())
                if action == "model_test":
                    self.preview["status"] = "error"
            LOG.warning("网页操作失败：%s (%s)", action, type(exc).__name__)

    def backup_file(self, source, name):
        folder = ROOT / ".local/settings-backups"
        folder.mkdir(mode=0o700, exist_ok=True)
        atomic_write(folder / (name + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".txt"), source.read_text())
        for old in sorted(folder.glob(name + "-*.txt"))[:-5]:
            old.unlink()

    def edit_offline_profiles(self, action, data):
        path = ROOT / ".local/profiles.json"
        profiles = read_json(path, {})
        if action == "clear_context":
            for profile in profiles.values():
                profile["pool"] = []
            self.last_bot = {}
        else:
            uid = str(data.get("uid", ""))
            if uid not in profiles:
                raise ValueError("找不到该群友")
            if action == "clear_pool":
                profiles[uid]["pool"] = []
            else:
                notes = data.get("notes")
                if not isinstance(notes, list) or len(notes) > 5 or not all(isinstance(n, str) and len(n) <= 40 for n in notes):
                    raise ValueError("备注最多 5 条，每条最多 40 字")
                profiles[uid]["notes"] = notes
        atomic_write(path, json.dumps(profiles, ensure_ascii=False))

    def ollama_post(self, path, data):
        req = Request(self.config()["ollama_url"].rstrip("/") + path, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=180) as response:
            return json.load(response)

    def api_get(self, path, query):
        if path == "/api/health":
            return {"app": "whale-console-v1", "ok": True}
        if path == "/api/settings":
            config = self.config()
            fields = {k: config.get(k) for k in FIELDS}
            fields.update(chime_probability=config.get("chime_probability", 1), chime_cooldown=config.get("chime_cooldown", 0),
                          chime_queue_timeout_seconds=config.get("chime_queue_timeout_seconds", 30), ignore_user_ids=config.get("ignore_user_ids", []))
            return {"config": fields, "onebot_token_set": bool(config.get("onebot_token")), "modelfile": manage.MODELFILE.read_text()}
        if path == "/api/logs":
            source = query.get("source", ["bot"])[0]
            if source == "napcat":
                if not manage.docker_ready():
                    return {"text": "Docker 未启动"}
                r = subprocess.run(manage.docker_command("logs", "--tail", "100", "napcat-napcat-1"), capture_output=True, text=True, timeout=8)
                text = (r.stdout + r.stderr)[-30000:]
            else:
                files = {"bot": "bot.log", "ollama": "ollama.log", "console": "console.log"}
                if source not in files:
                    raise ValueError("未知日志类型")
                p = ROOT / ".local" / files[source]
                if not p.exists():
                    return {"text": "暂无日志"}
                with p.open("rb") as stream:
                    stream.seek(max(0, p.stat().st_size - 50000))
                    text = "\n".join(stream.read().decode("utf-8", "replace").splitlines()[-160:])
            secret = self.config().get("onebot_token")
            if secret:
                text = text.replace(secret, "[已隐藏]")
            text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
            text = re.sub(r"(?i)(token[=: ]+)[^\s&]+", r"\1[已隐藏]", text)
            return {"text": text}
        if path == "/api/export":
            return self.export()
        if path == "/api/exports":
            folder = ROOT / ".local/exports"
            return {"files": [{"name": p.name, "bytes": p.stat().st_size} for p in sorted(folder.glob("*.txt"), reverse=True) if not p.is_symlink()]}
        if path == "/api/download":
            name = query.get("name", [""])[0]
            if Path(name).name != name or not name.endswith(".txt"):
                raise ValueError("文件名无效")
            p = ROOT / ".local/exports" / name
            if not p.is_file() or p.is_symlink():
                raise ValueError("找不到导出文件")
            return p.read_bytes(), "text/plain; charset=utf-8", {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)}
        if path == "/api/napcat-token":
            source = manage.NAPCAT.read_text()
            match = re.search(r'WEBUI_TOKEN:\s*["\']?([^\s"\']+)', source)
            return {"token": match.group(1) if match else "请在 NapCat 配置中查看"}
        raise ValueError("未知接口")

    def export(self):
        state = self.snapshot()
        now = datetime.now()
        lines = ["群聊消息、模型思考与最终回复", "导出时间：" + now.strftime("%Y-%m-%d %H:%M:%S"), ""]
        for number, task in enumerate(sorted(state.get("traces", []), key=lambda t: t["queued_at"]), 1):
            lines.extend(["=" * 54, f"第 {number} 条 · {task['nick']} · {task['status']}",
                          "【触发消息】", task.get("input", ""), "", "【模型思考原文】", task.get("thinking") or "（暂无）", "",
                          "【最终回复】", task.get("answer") or ("（未确认发送）\n" + task.get("draft", "")),
                          "发送状态：" + ("OneBot 已确认成功" if task["status"] == "sent" else "未确认发送"), ""])
        name = "模型思考与回复-" + now.strftime("%Y%m%d-%H%M%S") + ".txt"
        return ("\n".join(lines).encode("utf-8"), "text/plain; charset=utf-8",
                {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)})

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=5)
        self.pool.shutdown(wait=False, cancel_futures=True)


async def main():
    console = Console()
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    server = DashboardServer(loop, console.snapshot, console.config().get("dashboard_port", 8765),
                             commands=console.submit, api_get=console.api_get, csrf_token=console.csrf)
    console.thread.start()
    LOG.info("管理台已启动：%s；机器人停止不影响此页面", server.url)
    try:
        await stopped.wait()
    finally:
        await asyncio.to_thread(server.close)
        await asyncio.to_thread(console.close)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(main())
