"""统一管理本机机器人、NapCat 容器和原生 Ollama；模型数据位于 runtime/。"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / ".local/config.json"
MODELFILE = ROOT / "models/deepseek.modelfile"
MODEL_STORE = ROOT / "runtime/ollama/models"
NAPCAT = ROOT / "runtime/napcat/compose.yaml"
DOCKER_SOCKET = Path.home() / ".docker/run/docker.sock"


def executable(name, fallback):
    value = shutil.which(name)
    if value:
        return value
    if Path(fallback).is_file():
        return fallback
    raise RuntimeError(f"未找到 {name}，请先安装对应应用")


def docker_command(*args):
    binary = executable("docker", "/Applications/Docker.app/Contents/Resources/bin/docker")
    return [binary, "--host", "unix://" + str(DOCKER_SOCKET), *args]


def compose_command(*args):
    # 固定项目名，继续管理原来的 napcat-napcat-1，避免产生第二套容器。
    return docker_command("compose", "--project-name", "napcat", "--file", str(NAPCAT), *args)


def docker_ready():
    if not DOCKER_SOCKET.exists():
        return False
    try:
        return subprocess.run(docker_command("info", "--format", "{{.ServerVersion}}"),
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return False


def api(base_url, endpoint):
    with urlopen(base_url.rstrip("/") + endpoint, timeout=3) as response:
        return json.load(response)


def ollama_ready(config):
    try:
        api(config["ollama_url"], "/api/tags")
        return True
    except (OSError, ValueError):
        return False


def wait_for_service(name, predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"等待 {name} 启动超时，请打开应用检查；未重复创建其他服务")
        time.sleep(1)


def check_layout():
    for path in (MODELFILE, NAPCAT, MODEL_STORE / "blobs", MODEL_STORE / "manifests"):
        if not path.exists():
            raise RuntimeError(f"工作目录缺少：{path}")
    if MODEL_STORE.is_symlink():
        raise RuntimeError("模型数据应直接存放在当前工作目录，而不是指向其他位置的链接")


def run(args):
    subprocess.run(args, cwd=ROOT, check=True)


def bot(action):
    run([sys.executable, str(ROOT / "botctl.py"), action])


def status(config):
    print("工作目录：", ROOT, flush=True)
    print("模型定义：", MODELFILE, flush=True)
    print("模型数据：", MODEL_STORE, flush=True)
    print("使用模型：", config["model"], flush=True)
    try:
        models = api(config["ollama_url"], "/api/tags").get("models", [])
        match = next((m for m in models if m["name"] == config["model"]), None)
        print("Ollama：在线；" + ("当前模型存在" if match else "当前模型未找到"), flush=True)
    except (OSError, ValueError):
        print("Ollama：离线", flush=True)
    if docker_ready():
        result = subprocess.run(docker_command("inspect", "--format", "{{.State.Status}}", "napcat-napcat-1"),
                                capture_output=True, text=True, timeout=5)
        print("NapCat：" + (result.stdout.strip() if result.returncode == 0 else "尚未创建容器"), flush=True)
    else:
        print("NapCat：Docker Desktop 未运行或暂不可连接", flush=True)
    print("机器人后台：", flush=True)
    bot("status")
    try:
        s = api(f"http://127.0.0.1:{config.get('dashboard_port', 8765)}", "/api/state")
        print("QQ 连接：" + ("已连接" if s["connected"] else "未连接"), flush=True)
    except (OSError, ValueError):
        print("仪表盘：暂不可连接", flush=True)
    print(f"仪表盘地址：http://127.0.0.1:{config.get('dashboard_port', 8765)}", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="统一管理 QQ 机器人工作目录")
    parser.add_argument("action", choices=("start", "stop", "restart", "status", "model", "rebuild-model", "logs", "web"))
    args = parser.parse_args(argv)
    try:
        config = json.loads(CONFIG.read_text(encoding="utf-8"))
        if args.action == "status":
            status(config)
        elif args.action in ("start", "restart"):
            check_layout()
            if not ollama_ready(config):
                print("正在启动原生 Ollama 后台服务…", flush=True)
                run([sys.executable, str(ROOT / "ollamactl.py"), "start"])
                wait_for_service("Ollama", lambda: ollama_ready(config))
            if not docker_ready():
                print("正在打开 Docker Desktop…", flush=True)
                run(["open", "-a", "Docker"])
                wait_for_service("Docker Desktop", docker_ready)
            available = api(config["ollama_url"], "/api/tags").get("models", [])
            if not any(m["name"] == config["model"] for m in available):
                raise RuntimeError("找不到当前模型，请先执行 ./manage rebuild-model")
            run(compose_command("up", "--detach"))
            if args.action == "restart":
                run(compose_command("restart"))
            bot("start")
            print("组件已启动；QQ 登录及连接状态请在 ./manage status 或仪表盘查看。", flush=True)
        elif args.action == "stop":
            bot("stop")
            if docker_ready():
                run(compose_command("stop"))
            else:
                print("Docker 未运行，NapCat 无需停止。", flush=True)
            print("Ollama 和 Docker Desktop 应用保留运行，可继续用于其他终端会话。", flush=True)
        elif args.action in ("model", "rebuild-model"):
            check_layout()
            if not ollama_ready(config):
                raise RuntimeError("Ollama 未启动，请先打开 Ollama 或执行 ./manage start")
            command = [executable("ollama", "/usr/local/bin/ollama")]
            if args.action == "model":
                command += ["run", config["model"]]
            else:
                command += ["create", config["model"], "--file", str(MODELFILE)]
            # CLI 连接到机器人配置中的同一个服务，避免 OLLAMA_HOST 指向别处。
            subprocess.run(command, cwd=ROOT, check=True,
                           env={**os.environ, "OLLAMA_HOST": config["ollama_url"]})
        elif args.action == "logs":
            run(["tail", "-f", str(ROOT / ".local/bot.log")])
        elif args.action == "web":
            run(["open", f"http://127.0.0.1:{config.get('dashboard_port', 8765)}"])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, "错误：" + str(exc) + "\n")


if __name__ == "__main__":
    main()
