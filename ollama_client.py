"""本地 Ollama 调用层；QQ 接入程序可以导入 chat()。"""

import argparse
import json
from pathlib import Path
import time
from urllib import error, request


DEFAULT_PROMPT = Path(__file__).with_name("prompt.txt")


def post_json(base_url, endpoint, payload, timeout):
    req = request.Request(
        base_url.rstrip("/") + endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except error.HTTPError as exc:
        raise RuntimeError(f"Ollama 请求失败（HTTP {exc.code}），检查模型名及服务版本") from exc
    except (error.URLError, TimeoutError) as exc:
        raise RuntimeError("无法连接 Ollama 或生成超时，请检查本地服务") from exc


def post_stream_json(base_url, endpoint, payload, timeout, on_chunk):
    """逐行读取 Ollama NDJSON；回调看到增量，返回值保持与非流式调用一致。"""
    req = request.Request(base_url.rstrip("/") + endpoint,
                          data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    thinking, content, raw = [], [], []
    final = None
    try:
        with request.urlopen(req, timeout=timeout) as response:
            for line in response:
                if time.monotonic() - started > timeout:
                    raise TimeoutError("模型生成超过总时间上限")
                if not line.strip():
                    continue
                part = json.loads(line)
                if part.get("error"):
                    raise RuntimeError("Ollama 生成失败：" + str(part["error"]))
                message = part.get("message", {})
                thinking.append(message.get("thinking", ""))
                content.append(message.get("content", ""))
                raw.append(part.get("response", ""))
                on_chunk(part if endpoint == "/api/chat" else {**part, "message": {"content": part.get("response", "")}})
                if part.get("done"):
                    final = part
                    break
    except error.HTTPError as exc:
        raise RuntimeError(f"Ollama 请求失败（HTTP {exc.code}），检查模型名及服务版本") from exc
    except (error.URLError, TimeoutError) as exc:
        raise RuntimeError("无法连接 Ollama 或生成超时，请检查本地服务") from exc
    if final is None:
        raise RuntimeError("Ollama 流意外中断，未收到完整回复")
    return {**final, "message": {"thinking": "".join(thinking), "content": "".join(content)}, "response": "".join(raw)}


def deepseek_nothink_prompt(system, messages):
    """DeepSeek R1 文本协议：预填空思考段，再让模型直接续写最终回复。"""
    parts = ["<｜begin▁of▁sentence｜>", system]
    for item in messages:
        if item["role"] == "system":
            continue
        content = item["content"]
        # 聊天正文中的控制标记按普通文字处理，避免伪造角色边界。
        for marker in ("<｜begin▁of▁sentence｜>", "<｜end▁of▁sentence｜>", "<｜User｜>", "<｜Assistant｜>"):
            content = content.replace(marker, marker.replace("<", "‹").replace(">", "›"))
        if item["role"] == "user":
            parts.extend(("<｜User｜>", content))
        else:
            parts.extend(("<｜Assistant｜>", content, "<｜end▁of▁sentence｜>"))
    parts.append("<｜Assistant｜><think>\n\n</think>\n")
    return "".join(parts)


def final_content(message):
    """只取最终回复，兼容把 <think> 混在 content 里的旧版模板。"""
    content = message.get("content", "")
    while "<think>" in content:
        start = content.index("<think>")
        end = content.find("</think>", start)
        if end == -1:
            content = content[:start]
            break
        content = content[:start] + content[end + len("</think>"):]
    if "</think>" in content:
        content = content.rsplit("</think>", 1)[1]
    return content.strip()


def chat(text, *, history=None, model="deepseek-r1:8b",
         prompt_path=DEFAULT_PROMPT, base_url="http://127.0.0.1:11434",
         timeout=180, think=None, no_think_prefill=False, num_ctx=None, num_predict=None, on_chunk=None):
    """history 由 QQ 适配层按会话隔离，仅包含 user/assistant 消息。"""
    if not text.strip():
        raise ValueError("消息不能为空")
    messages = []
    # None 使用模型 Modelfile 内置的 SYSTEM，避免覆盖用户的角色设定。
    if prompt_path is not None:
        prompt = Path(prompt_path).read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError("prompt 文件不能为空")
        messages.append({"role": "system", "content": prompt})
    for item in (history or []):
        if item.get("role") not in ("user", "assistant"):
            raise ValueError("history 只允许 user/assistant 消息")
        messages.append({"role": item["role"], "content": item["content"]})
    messages.append({"role": "user", "content": text})
    if no_think_prefill and think is False:
        # 当前 llamacpp 后端可能忽略 Go 模板里的 .Think；raw 明确绕过其模板。
        info = post_json(base_url, "/api/show", {"model": model}, min(timeout, 10))
        template = info.get("template", "") + info.get("modelfile", "")
        if "<｜User｜>" not in template or "<｜Assistant｜>" not in template:
            raise ValueError("no_think_prefill 仅适用于 DeepSeek R1 文本模板")
        system = messages[0]["content"] if messages[0]["role"] == "system" else info.get("system", "")
        payload = {
            "model": model,
            "prompt": deepseek_nothink_prompt(system, messages),
            "raw": True,
            "think": False,
            "stream": on_chunk is not None,
        }
        options = {k: v for k, v in {"num_ctx": num_ctx, "num_predict": num_predict}.items() if v is not None}
        if options:
            payload["options"] = options
        result = (post_stream_json(base_url, "/api/generate", payload, timeout, on_chunk) if on_chunk is not None
                  else post_json(base_url, "/api/generate", payload, timeout))
        answer = final_content({"content": result.get("response", "")})
        if not answer:
            raise RuntimeError("关闭思考调用未生成最终回复，请检查模型模板和输出上限")
        return answer
    payload = {
        "model": model,
        "messages": messages,
        "stream": on_chunk is not None,
        # 思考和最终回复共用输出额度；温度等其余参数继承模型设定。
        "options": {"num_ctx": num_ctx if num_ctx is not None else 4096,
                    "num_predict": num_predict if num_predict is not None else 2048},
    }
    if think is not None:
        payload["think"] = think
    result = (post_stream_json(base_url, "/api/chat", payload, timeout, on_chunk) if on_chunk is not None
              else post_json(base_url, "/api/chat", payload, timeout))
    answer = final_content(result.get("message", {}))
    if not answer:
        raise RuntimeError("模型没有生成最终回复，可能已耗尽生成 token；请缩短问题或调高 num_predict")
    return answer


def main():
    parser = argparse.ArgumentParser(description="测试 QQ 机器人使用的本地模型和角色 prompt")
    parser.add_argument("text", help="发送给模型的测试消息")
    parser.add_argument("--model", default="deepseek-r1:8b")
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    args = parser.parse_args()
    try:
        print(chat(args.text, model=args.model, prompt_path=args.prompt))
    except (RuntimeError, ValueError, OSError) as exc:
        parser.exit(1, f"错误：{exc}\n")


if __name__ == "__main__":
    main()
