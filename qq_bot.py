"""Mac 上的 OneBot 11 ↔ Ollama 群聊连接程序（只服务 config 里指定的那一个群）。

设计要点：
1. 只处理目标群（group_id 不符的事件在解析阶段直接丢弃，不记录、不回复）。
2. 全群共用一份滚动聊天记录（所有人的话交错排列，带「回复谁」标注）。
3. 每个成员有自己的消息池 + 画像，持久化到 .local/profiles.json。
4. 每次请求 = 群聊记录 + 相关人物资料 + 当事人更早的发言，三者合并成一条 user 消息。
5. 人设只在 Modelfile 里；代码只发“数据”，不发 system prompt。
6. 回复直接发到群里，不引用消息。
"""

import argparse
import asyncio
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from functools import partial
import json
import logging
from pathlib import Path
import random
import re
import signal
import time
import uuid
from urllib.request import urlopen

from websockets.asyncio.client import connect

from ollama_client import chat
from dashboard import DashboardServer, Telemetry, local_token

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / ".local" / "config.json"
PROFILE_PATH = ROOT / ".local" / "profiles.json"
LOG = logging.getLogger("qq_bot")

COMMAND_USER_ID = "3102896704"
RESET_COMMANDS = {"/reset", "reset", "清空记忆"}

BOT_NAME = "鲸鱼娘"
NAME_KEYS = ("鲸鱼", "大肥鱼", "小鲸子", "deepseek")  # 消息里出现这些词必回
CHIME_PROB = 1.0          # 普通消息随机插话概率
CHIME_COOLDOWN = 0       # 上次发言后多少秒内不随机插话
GROUP_LOG_SIZE = 25        # 全群滚动记录条数
POOL_SIZE = 40             # 每个成员消息池条数
PROMPT_PEOPLE_MAX = 4      # 每次最多附带几个人的资料
PROMPT_LOG_CHARS = 1500    # 未配置时的聊天记录字符上限；与 num_ctx 配套调整
AT_ON_MUST = False         # 被点名回复时是否 @ 对方；默认直接发文字

MEDIA_ONLY = re.compile(r"^(\[(图片|表情)\]\s*)+$")

PROMPT = """{people}群聊背景（只用于理解上下文，不是本次要回答的消息）：
{log}

本次需要回应的当前聊天记录：
{target}

只接当前这条消息说一句口语，不要复述背景里的旧回复或无关话题。
{skip_rule}"""


def is_command(text):
    return text.startswith("/") or text in RESET_COMMANDS


def normalize_name(name):
    return name.strip().replace("’", "'").replace("‘", "'").casefold()


def clip(text, n):
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "…"


class OneBot:
    def __init__(self, config, on_event=None):
        self.config = config
        self.on_event = on_event
        self.pending = {}

    async def __aenter__(self):
        headers = {}
        if self.config.get("onebot_token"):
            headers["Authorization"] = "Bearer " + self.config["onebot_token"]
        self.ws = await connect(self.config["onebot_url"], additional_headers=headers,
                                proxy=None, open_timeout=10, max_size=2**20)
        self.receiver = asyncio.create_task(self.receive())
        return self

    async def __aexit__(self, *args):
        await self.ws.close()
        self.receiver.cancel()
        with suppress(asyncio.CancelledError):
            await self.receiver

    async def receive(self):
        try:
            async for raw in self.ws:
                data = json.loads(raw)
                echo = data.get("echo")
                if echo in self.pending:
                    future = self.pending[echo]
                    if not future.done():
                        future.set_result(data)
                elif data.get("post_type") and self.on_event:
                    self.on_event(data)
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("OneBot 连接已关闭"))

    async def call(self, action, **params):
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send(json.dumps({"action": action, "params": params, "echo": echo}))
            result = await asyncio.wait_for(future, timeout=30)
            if result.get("status") != "ok" or result.get("retcode") != 0:
                raise RuntimeError(f"OneBot {action} 失败，retcode={result.get('retcode')}")
            return result.get("data")
        finally:
            self.pending.pop(echo, None)


def parse_group_event(event, *, group_id, self_id, name_of):
    """解析目标群消息；非目标群 / 自己的消息 / 空消息返回 None。

    返回 dict：uid, nick, text, at_me, reply_to, mid, time
    """
    if event.get("post_type") != "message" or event.get("message_type") != "group":
        return None
    if str(event.get("group_id")) != str(group_id):
        return None
    uid = str(event.get("user_id"))
    if uid == str(self_id):
        return None
    segments = event.get("message")
    if not isinstance(segments, list):
        return None
    sender = event.get("sender") or {}
    nick = (sender.get("card") or sender.get("nickname") or uid).strip()

    parts, at_me, reply_to = [], False, None
    for s in segments:
        kind, data = s.get("type"), s.get("data", {})
        if kind == "text":
            parts.append(data.get("text", ""))
        elif kind == "at":
            qq = str(data.get("qq"))
            if qq == str(self_id):
                at_me = True                       # @我 不写进文本
            elif qq == "all":
                parts.append("@全体成员")
            else:
                parts.append("@" + str(name_of(qq) or data.get("name") or qq))
        elif kind == "reply":
            reply_to = str(data.get("id"))
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "face":
            parts.append("[表情]")
    text = " ".join("".join(parts).split())
    if not text:
        if not at_me:
            return None
        text = "（只@了你，没说内容）"
    return {"uid": uid, "nick": nick, "text": text, "at_me": at_me, "reply_to": reply_to,
            "mid": str(event.get("message_id")), "time": event.get("time", 0)}


BAN_PHRASES = ("需要帮忙", "可以帮你", "请问", "尽管说", "帮你解答", "帮你解释", "为你解答")


def clean_reply(text):
    """兜底清洗：去思考标签、名字前缀、括号动作、“本鱼”、客服句，限制长度。"""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"^\s*\[?" + BOT_NAME + r"\]?\s*[:：]\s*", "", text)
    text = re.sub(r"[（(][^）)]{0,40}[）)]", "", text)
    text = text.replace("本鱼", "我").replace("本小姐", "我")
    sentences = re.split(r"(?<=[。！？!?~])", text)
    sentences = [s for s in sentences if s.strip() and not any(b in s for b in BAN_PHRASES)]
    text = "".join(sentences[:2]).strip()
    if len(text) > 60:
        cut = max(text.rfind(c, 0, 60) for c in "。！？~，")
        text = text[:cut + 1] if cut > 10 else text[:60]
    return text


def load_profiles():
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class GroupBot:
    def __init__(self, config):
        self.config = config
        self.self_id = None
        self.api = None
        self.queue = asyncio.Queue(maxsize=8)
        self.group_log = deque(maxlen=config.get("group_log_size", GROUP_LOG_SIZE))
        self.profiles = load_profiles()                  # uid -> 画像 + 个人消息池
        self.msg_owner = OrderedDict()                   # mid -> (nick, text)，用于解析“回复谁”
        self.my_msg_ids = OrderedDict()                  # 自己发过的 mid，用于识别“有人回复我”
        self.recent_answers = deque(maxlen=4)
        self.ignore = {str(x) for x in config.get("ignore_user_ids", [])}  # 其他机器人账号
        self.last_reply = -CHIME_COOLDOWN
        self.clock = time.monotonic
        self.context_version = 0
        self.pending_chime = None
        self.stopping = False
        self.seen = OrderedDict()
        self.dirty = False
        self.accept_after = int(time.time()) - 5
        self.model_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ollama")
        self.telemetry = Telemetry()

    def dashboard_state(self):
        """仅从 asyncio 主循环读取消息池，HTTP 线程不会遍历正在变化的字典。"""
        return {**self.telemetry.snapshot(), "now": time.time(), "connected": self.self_id is not None,
                "stopping": self.stopping, "queue_size": self.queue.qsize(), "queue_capacity": self.queue.maxsize,
                "config": {k: self.config.get(k) for k in ("model", "group_name", "group_id", "think",
                           "num_ctx", "num_predict", "model_timeout_seconds", "prompt_log_chars")},
                "bot_name": BOT_NAME, "pool_capacity": POOL_SIZE,
                "group_capacity": self.group_log.maxlen, "group_log": deepcopy(list(self.group_log)),
                "profiles": deepcopy(self.profiles)}

    def web_command(self, action, data):
        if action == "clear_context":
            self.clear_context()
            self.save_profiles()
        elif action in ("clear_pool", "save_notes"):
            uid = str(data.get("uid", ""))
            if uid not in self.profiles:
                raise ValueError("找不到该群友")
            if action == "clear_pool":
                self.profiles[uid]["pool"] = []
                self.context_version += 1
            else:
                notes = data.get("notes")
                if not isinstance(notes, list) or len(notes) > 5 or not all(isinstance(n, str) and len(n) <= 40 for n in notes):
                    raise ValueError("备注最多 5 条，每条最多 40 字")
                self.profiles[uid]["notes"] = notes
            self.dirty = True
            self.save_profiles()
        else:
            raise ValueError("不支持的机器人操作")
        return {"ok": True}

    async def poll_model_health(self):
        def probe():
            with urlopen(self.config["ollama_url"].rstrip("/") + "/api/ps", timeout=2) as response:
                models = json.load(response).get("models", [])
            loaded = next((m for m in models if m.get("name") == self.config["model"]), None)
            return {"state": "online", "loaded": loaded is not None, "checked_at": time.time(),
                    "context_length": (loaded or {}).get("context_length"),
                    "size_vram": (loaded or {}).get("size_vram", 0)}
        while True:
            try:
                result = await asyncio.to_thread(probe)
            except Exception:
                result = {"state": "offline", "loaded": False, "checked_at": time.time()}
            with self.telemetry.lock:
                self.telemetry.ollama = result
            await asyncio.sleep(10)

    # ---------- 画像 ----------
    def name_of(self, qq):
        p = self.profiles.get(str(qq))
        return p["nick"] if p else None

    def touch_profile(self, uid, nick, t, text, mid, direct):
        p = self.profiles.get(uid)
        if p is None:
            p = self.profiles[uid] = {"nick": nick, "aliases": [], "count": 0, "first": t,
                                      "last": t, "to_bot": 0, "notes": [], "pool": []}
        if nick != p["nick"]:
            if p["nick"] not in p["aliases"]:
                p["aliases"].append(p["nick"])
            p["aliases"] = [a for a in p["aliases"] if a != nick][-4:]
            p["nick"] = nick
        p["count"] += 1
        p["last"] = t
        if direct:
            p["to_bot"] += 1
        if not MEDIA_ONLY.match(text):
            p["pool"].append([t, mid, text[:120]])
            del p["pool"][:-POOL_SIZE]
        self.dirty = True

    def add_note(self, who, note):
        who = normalize_name(who)
        for p in self.profiles.values():
            if who in {normalize_name(p["nick"])} | {normalize_name(a) for a in p["aliases"]}:
                p["notes"] = (p["notes"] + [note[:40]])[-5:]
                self.dirty = True
                return True
        return False

    def describe(self, uid, window_mids, focus, *, profiles=None):
        p = (self.profiles if profiles is None else profiles)[uid]
        n = p["count"]
        bits = ["话多" if n >= 300 else "常发言" if n >= 60 else "偶尔发言" if n >= 10 else "新面孔"]
        if p["to_bot"] >= 5:
            bits.append("常找你聊天")
        elif p["to_bot"]:
            bits.append("找你聊过")
        if p["aliases"]:
            bits.append("曾用名" + "/".join(p["aliases"][-2:]))
        bits.extend(p["notes"][-2:])
        line = f"- {p['nick']}：" + "，".join(bits)
        if focus:   # 当事人更早（已不在滚动记录里）的发言，补足上下文
            older = [clip(t, 30) for _, mid, t in p["pool"] if mid not in window_mids][-3:]
            if older:
                line += "；更早说过：" + " / ".join(older)
        return line

    def save_profiles(self):
        if not self.dirty:
            return
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PROFILE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.profiles, ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(PROFILE_PATH)
        self.dirty = False

    async def autosave(self):
        while True:
            await asyncio.sleep(60)
            try:
                self.save_profiles()
            except OSError:
                LOG.exception("消息池自动保存失败")

    def begin_shutdown(self):
        """在退出信号到达时立即落盘，不等待网络连接或模型退出。"""
        self.stopping = True
        self.telemetry.connection = "stopped"
        self.telemetry.cancel_all("程序正在停止")
        self.self_id = None
        self.context_version += 1
        try:
            self.save_profiles()
        except OSError:
            LOG.exception("停止前保存消息池失败")

    def clear_context(self):
        """清除对话和个人发言池；保留昵称、统计及管理员备注。"""
        self.context_version += 1
        self.telemetry.clear()
        self.group_log.clear()
        self.msg_owner.clear()
        self.my_msg_ids.clear()
        self.recent_answers.clear()
        for profile in self.profiles.values():
            profile["pool"] = []
        self.dirty = True
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.pending_chime = None

    def job_skip_reason(self, job):
        if self.stopping:
            return "程序正在停止"
        if job["version"] != self.context_version:
            return "上下文已重置，旧任务失效"
        if "generation_started" in job:
            limit = self.config.get("model_timeout_seconds", 180)
            if self.clock() - job["generation_started"] > limit:
                return f"模型生成超过 {limit} 秒上限"
        elif job["mode"] == "chime":
            limit = self.config.get("chime_queue_timeout_seconds", 30)
            if self.clock() - job["queued"] > limit:
                return f"插话排队超过 {limit} 秒"
        elif self.clock() - job["queued"] > 180:
            return "回复超过 180 秒有效期"
        if job["mode"] == "chime" and self.clock() - self.last_reply < self.config.get("chime_cooldown", CHIME_COOLDOWN):
            return "插话冷却尚未结束"
        return None

    def job_valid(self, job):
        return self.job_skip_reason(job) is None

    # ---------- 触发与记录 ----------
    def decide(self, text, at_me, reply_to):
        """must=必须回复，chime=随机插话（模型可 SKIP），None=不回"""
        if at_me or (reply_to and reply_to in self.my_msg_ids):
            return "must"
        if any(k in text.lower() for k in NAME_KEYS):
            return "must"
        if self.clock() - self.last_reply < self.config.get("chime_cooldown", CHIME_COOLDOWN):
            return None
        if self.pending_chime is not None:
            return None
        return "chime" if random.random() < self.config.get("chime_probability", CHIME_PROB) else None

    def remember(self, entry):
        self.msg_owner[entry["mid"]] = (entry["nick"], entry["text"])
        if len(self.msg_owner) > 500:
            self.msg_owner.popitem(last=False)
        last = self.group_log[-1] if self.group_log else None
        # 同一个人连续发图，只留第一条，免得图片挤占上下文窗口
        if (MEDIA_ONLY.match(entry["text"]) and last and last["uid"] == entry["uid"]
                and MEDIA_ONLY.match(last["text"])):
            return
        self.group_log.append(entry)

    def on_event(self, event):
        if self.self_id is None or self.stopping:
            return
        p = parse_group_event(event, group_id=self.config["group_id"], self_id=self.self_id,
                              name_of=self.name_of)
        if p is None or p["time"] < self.accept_after or p["uid"] in self.ignore:
            return
        if p["mid"] in self.seen:
            return
        self.seen[p["mid"]] = True
        if len(self.seen) > 1000:
            self.seen.popitem(last=False)

        text, say, mode = p["text"], None, None
        admin = str(self.config.get("command_user_id", COMMAND_USER_ID))
        if is_command(text):
            if p["uid"] != admin:
                return
            parts = text.split(maxsplit=2)
            command_text = text
            if parts[0] == "/ai":
                if len(parts) < 2:
                    return
                command_text = text.split(maxsplit=1)[1].strip()
            if command_text in RESET_COMMANDS:
                self.clear_context()
                mode, say = "say", "聊天记录和个人消息池清空啦，人物资料和备注还保留着"
            elif parts[0] == "/ai" and not command_text.startswith("/"):
                text, mode = command_text, "must"
            elif parts[0] == "/note" and len(parts) == 3:   # /note 昵称 备注
                ok = self.add_note(parts[1], parts[2])
                mode, say = "say", ("记住啦" if ok else "没找到这个人诶")
            else:
                return
        if mode is None:
            mode = self.decide(text, p["at_me"], p["reply_to"])

        LOG.info("收到目标群消息；触发=%s；等待任务=%s", mode or "不回复", self.queue.qsize())
        self.telemetry.receive(p["nick"], mode)

        if mode != "say":
            reply_nick = reply_snip = None
            if p["reply_to"] and p["reply_to"] in self.msg_owner:
                reply_nick, snip = self.msg_owner[p["reply_to"]]
                reply_snip = clip(snip, 14)
            entry = {"uid": p["uid"], "nick": p["nick"], "text": text[:200], "mid": p["mid"],
                     "reply_nick": reply_nick, "reply_snip": reply_snip}
            self.remember(entry)                       # 所有人的发言都先记下来
            self.touch_profile(p["uid"], p["nick"], p["time"], text, p["mid"], mode == "must")
        if mode is None or (mode == "chime" and not self.queue.empty()):
            if mode == "chime":
                LOG.info("跳过插话：已有任务排队")
            return
        job = self.make_job(mode, say, p["uid"], target=entry if mode != "say" else None)
        try:
            self.queue.put_nowait(job)
            self.telemetry.queued(job, p["nick"])
            if mode == "chime":
                self.pending_chime = job
        except asyncio.QueueFull:
            LOG.warning("请求队列已满，忽略新请求")
            self.telemetry.event("skipped", "请求队列已满，本条消息仅存入消息池")

    # ---------- 生成与发送 ----------
    @staticmethod
    def fmt_line(e):
        head = e["nick"]
        if e.get("reply_nick"):
            head += f" 回复 {e['reply_nick']}"
            if e.get("reply_snip"):
                head += f"「{e['reply_snip']}」"
        return f"[{head}]: {e['text']}"

    def make_job(self, mode, say, uid, *, target=None):
        snapshot = deepcopy(list(self.group_log))
        target = deepcopy(target or next((e for e in reversed(snapshot) if e["uid"] == uid), None))
        while sum(len(self.fmt_line(e)) for e in snapshot) > self.config.get("prompt_log_chars", PROMPT_LOG_CHARS) and len(snapshot) > 1:
            snapshot.pop(0)
        uids = [uid]
        for entry in reversed(snapshot[-8:]):
            if entry["uid"] and entry["uid"] not in uids:
                uids.append(entry["uid"])
        return {"mode": mode, "say": say, "uid": uid, "queued": self.clock(),
                "version": self.context_version, "snapshot": snapshot,
                "target": target,
                "profiles": {u: deepcopy(self.profiles[u]) for u in uids[:PROMPT_PEOPLE_MAX]
                             if u in self.profiles}}

    def build_prompt(self, job, *, focused=False):
        snapshot = job["snapshot"]
        window_mids = {e["mid"] for e in snapshot}
        people = [] if focused else [self.describe(u, window_mids, u == job["uid"], profiles=job["profiles"])
                                     for u in job["profiles"]]
        target = job["target"]
        # 仅保留最近两条不同的机器人回复，避免旧答案在背景中反复强化。
        bot_lines = set()
        background = []
        for e in reversed(snapshot):
            if target and e["mid"] == target["mid"]:
                continue
            if e["uid"] is None:
                key = self.answer_key(e["text"])
                if key in bot_lines or len(bot_lines) >= 2:
                    continue
                bot_lines.add(key)
            background.append(e)
        background.reverse()
        lines = [] if focused else [self.fmt_line(e) for e in background]
        while sum(len(x) for x in lines) > self.config.get("prompt_log_chars", PROMPT_LOG_CHARS) and len(lines) > 3:
            lines.pop(0)
        skip_rule = ("必须回复，不要输出 [SKIP]。" if job["mode"] == "must"
                     else "如果这条消息不需要你插话，只输出 [SKIP]。")
        return PROMPT.format(people=("群友资料：\n" + "\n".join(people) + "\n\n") if people else "",
                             log="\n".join(lines) or "（无）",
                             target=self.fmt_line(target) if target else "（无文字消息）",
                             skip_rule=skip_rule)

    @staticmethod
    def answer_key(text):
        return re.sub(r"[\s。.!！?？~～，,、…]+", "", text)

    def repeated_answer(self, answer, job):
        key = self.answer_key(answer)
        # 短问候、数字等可以合理重复；只拦截较长的相同回复。
        if len(key) < 12:
            return False
        previous = list(self.recent_answers) + [e["text"] for e in job["snapshot"] if e["uid"] is None]
        return any(self.answer_key(old) == key for old in previous)

    async def send(self, text, at_uid=None, *, job=None):
        # 使用 text 消息段，模型输出中的 CQ 代码不会被当作指令执行。不引用消息，直接发。
        segments = []
        if at_uid:
            segments += [{"type": "at", "data": {"qq": str(at_uid)}},
                         {"type": "text", "data": {"text": " "}}]
        segments.append({"type": "text", "data": {"text": text[:1800]}})
        if job:
            self.telemetry.stage(job, "sending", "正在通过 OneBot 发送群消息", answer=text)
        data = await self.api.call("send_group_msg", group_id=self.config["group_id"],
                                   message=segments)
        self.last_reply = self.clock()
        self.recent_answers.append(text)
        mid = str((data or {}).get("message_id"))
        if data and data.get("message_id") is not None:
            self.my_msg_ids[mid] = True
            if len(self.my_msg_ids) > 200:
                self.my_msg_ids.popitem(last=False)
            self.remember({"uid": None, "nick": BOT_NAME, "text": text[:200], "mid": mid,
                           "reply_nick": None, "reply_snip": None})
        if job:
            self.telemetry.finish(job, "sent", "OneBot 已确认发送成功", answer=text, message_id=mid)

    async def handle(self, job):
        if "trace_id" not in job:
            self.telemetry.queued(job, self.name_of(job["uid"]) or job["uid"])
        reason = self.job_skip_reason(job)
        if reason:
            LOG.info("跳过回复（%s）：%s", job["mode"], reason)
            self.telemetry.finish(job, "skipped", reason)
            return
        if job["say"]:
            await self.send(job["say"], job=job)
            return
        prompt = self.build_prompt(job)
        self.telemetry.stage(job, "context", "已冻结消息池与聊天记录，正在整理输入", prompt=prompt,
                             context_messages=len(job["snapshot"]), context_people=len(job["profiles"]))
        if self.config.get("debug_prompt"):
            LOG.info("发给模型的内容：\n%s", prompt)
        # 上下文全在这一条 user 消息里，所以 history 传空；prompt_path=None 使用 Modelfile 内置设定
        def generate_for(current_prompt, attempt=1):
            return partial(chat, current_prompt, history=[], model=self.config["model"], prompt_path=None,
                           base_url=self.config["ollama_url"],
                           timeout=self.config.get("model_timeout_seconds", 180),
                           think=self.config.get("think", False),
                           no_think_prefill=self.config.get("no_think_prefill", False),
                           num_ctx=self.config.get("num_ctx"),
                           num_predict=self.config.get("num_predict"),
                           on_chunk=partial(self.telemetry.chunk, job, attempt=attempt))
        started = self.clock()
        job["generation_started"] = started
        LOG.info("开始调用模型；类型=%s；模型=%s", job["mode"], self.config["model"])
        self.telemetry.stage(job, "loading", "等待 Ollama 加载上下文并开始输出")
        raw = await asyncio.get_running_loop().run_in_executor(self.model_pool, generate_for(prompt))
        LOG.info("模型生成完成；类型=%s；耗时=%.1f秒", job["mode"], self.clock() - started)
        # 生成期间可能发生重置、停止或插话超时，发送前必须重新核对。
        reason = self.job_skip_reason(job)
        if reason:
            LOG.info("取消发送（%s）：%s", job["mode"], reason)
            self.telemetry.finish(job, "cancelled", reason)
            return
        if job["mode"] == "chime" and raw.strip().upper() == "[SKIP]":
            LOG.info("跳过插话：模型返回 [SKIP]")
            self.telemetry.finish(job, "skipped", "模型判断本条消息无需插话")
            return
        answer = clean_reply(raw.replace("[SKIP]", ""))
        self.telemetry.stage(job, "cleaning", "过滤思考标签、整理回复并检查重复", draft=raw)
        if self.repeated_answer(answer, job):
            LOG.warning("检测到重复长回复，仅使用当前消息重新生成一次")
            focused_prompt = self.build_prompt(job, focused=True)
            self.telemetry.stage(job, "loading", "检测到重复回复，缩小上下文后重试一次", attempt=2,
                                 thinking="", draft="", prompt=focused_prompt)
            raw = await asyncio.get_running_loop().run_in_executor(
                self.model_pool, generate_for(focused_prompt, attempt=2))
            if not self.job_valid(job):
                self.telemetry.finish(job, "cancelled", self.job_skip_reason(job))
                return
            if job["mode"] == "chime" and raw.strip().upper() == "[SKIP]":
                LOG.info("跳过插话：重试返回 [SKIP]")
                self.telemetry.finish(job, "skipped", "重试后模型判断无需插话")
                return
            answer = clean_reply(raw.replace("[SKIP]", ""))
            if self.repeated_answer(answer, job):
                LOG.warning("重试仍为重复长回复，取消发送")
                self.telemetry.finish(job, "skipped", "重试仍为重复长回复，已拦截")
                return
        if not answer:
            if job["mode"] != "must":
                LOG.info("跳过插话：清洗后的回复为空")
                self.telemetry.finish(job, "skipped", "清洗后的回复为空")
                return
            answer = random.choice(["诶？", "嘿嘿", "干嘛啦", "嗯？"])
        at_uid = job["uid"] if (AT_ON_MUST and job["mode"] == "must") else None
        await self.send(answer, at_uid, job=job)
        LOG.info("已完成一次群聊回复（%s）", job["mode"])

    async def worker(self):
        while True:
            job = await self.queue.get()
            try:
                if not self.job_valid(job):
                    LOG.info("忽略排队请求：%s", self.job_skip_reason(job))
                    self.telemetry.finish(job, "skipped", self.job_skip_reason(job))
                    continue
                await self.handle(job)
            except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
                LOG.exception("回复失败：%s", type(exc).__name__)
                self.telemetry.finish(job, "error", str(exc))
                if job["mode"] == "must" and self.job_valid(job):
                    with suppress(Exception):
                        await self.send("诶，我脑子卡了，等下再问我")
            except Exception as exc:
                LOG.exception("回复失败：%s", type(exc).__name__)
                self.telemetry.finish(job, "error", str(exc))
            except asyncio.CancelledError:
                self.telemetry.finish(job, "cancelled", "连接断开或程序停止，任务已取消")
                raise
            finally:
                if self.pending_chime is job:
                    self.pending_chime = None
                self.queue.task_done()

    async def run(self):
        saver = asyncio.create_task(self.autosave())
        health = asyncio.create_task(self.poll_model_health())
        try:
            while not self.stopping:
                worker = None
                try:
                    self.telemetry.connection = "connecting"
                    async with OneBot(self.config, self.on_event) as api:
                        self.api = api
                        login = await api.call("get_login_info")
                        self.self_id = login["user_id"]
                        info = await api.call("get_group_info", group_id=self.config["group_id"])
                        if normalize_name(info["group_name"]) != normalize_name(self.config["group_name"]):
                            raise RuntimeError("目标群名称发生变化，请重新核对配置")
                        self.telemetry.connection = "online"
                        self.telemetry.event("connection", "已连接 NapCat · " + self.config["group_name"])
                        LOG.info("已连接 OneBot；仅服务目标群=%s；模型=%s；指令用户=%s",
                                 self.config["group_name"], self.config["model"],
                                 self.config.get("command_user_id", COMMAND_USER_ID))
                        LOG.info("已加载回复策略；插话概率=%.0f%%；冷却=%s秒；插话允许模型返回 [SKIP]",
                                 self.config.get("chime_probability", CHIME_PROB) * 100,
                                 self.config.get("chime_cooldown", CHIME_COOLDOWN))
                        LOG.info("超时设置；插话排队=%s秒；模型生成=%s秒",
                                 self.config.get("chime_queue_timeout_seconds", 30),
                                 self.config.get("model_timeout_seconds", 180))
                        LOG.info("思考设置；think=%s；空思考段兼容模式=%s",
                                 self.config.get("think", False), self.config.get("no_think_prefill", False))
                        LOG.info("上下文设置；num_ctx=%s；num_predict=%s；群记录=%s条/%s字",
                                 self.config.get("num_ctx", 4096), self.config.get("num_predict", 2048),
                                 self.group_log.maxlen, self.config.get("prompt_log_chars", PROMPT_LOG_CHARS))
                        worker = asyncio.create_task(self.worker())
                        await api.receiver
                except Exception as exc:
                    LOG.warning("连接中断或启动失败（%s），5 秒后重连", type(exc).__name__)
                    self.telemetry.connection = "offline"
                    self.telemetry.event("error", "OneBot 连接中断或启动失败：" + type(exc).__name__)
                finally:
                    self.self_id = None
                    if worker:
                        # 生成线程在单线程池内串行运行，重连不会并行调用模型。
                        worker.cancel()
                        with suppress(asyncio.CancelledError):
                            await worker
                    while not self.queue.empty():
                        self.queue.get_nowait()
                        self.queue.task_done()
                    self.pending_chime = None
                    self.telemetry.cancel_all("连接已断开，旧队列已清除")
                if not self.stopping:
                    await asyncio.sleep(5)
        finally:
            self.begin_shutdown()
            saver.cancel()
            health.cancel()
            with suppress(asyncio.CancelledError):
                await saver
            with suppress(asyncio.CancelledError):
                await health
            self.model_pool.shutdown(wait=False, cancel_futures=True)


async def run_bot(config):
    bot = GroupBot(config)
    loop = asyncio.get_running_loop()
    dashboard = None
    if config.get("dashboard_enabled", True):
        try:
            dashboard = DashboardServer(loop, bot.dashboard_state, config.get("bot_status_port", 8766),
                                        commands=bot.web_command, auth_token=local_token(ROOT), serve_ui=False)
            LOG.info("机器人内部状态接口已启动：%s", dashboard.url)
        except OSError:
            LOG.exception("仪表盘端口无法启动，机器人继续运行")
    task = asyncio.create_task(bot.run())

    def stop():
        bot.begin_shutdown()
        task.cancel()

    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop)
    try:
        await task
    except asyncio.CancelledError:
        if not bot.stopping:
            raise
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        if dashboard:
            await asyncio.to_thread(dashboard.close)


async def check(config):
    async with OneBot(config) as api:
        login = await api.call("get_login_info")
        groups = await api.call("get_group_list")
        matched = [g for g in groups if normalize_name(g["group_name"]) == normalize_name(config["group_name"])]
        if len(matched) != 1:
            raise RuntimeError(f"目标群匹配数量为 {len(matched)}，需要唯一群号")
        config["group_id"] = matched[0]["group_id"]
        config["self_id"] = login["user_id"]
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        CONFIG_PATH.chmod(0o600)
        print(json.dumps({"connected": True, "group_name": matched[0]["group_name"],
                          "group_id": config["group_id"], "model": config["model"]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只核对连接和目标群，不发送消息")
    args = parser.parse_args()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.check:
        asyncio.run(check(config))
    else:
        if not config.get("group_id"):
            parser.error("请先运行 --check 核对目标群")
        asyncio.run(run_bot(config))


if __name__ == "__main__":
    main()
