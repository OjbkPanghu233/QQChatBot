"""双击启动脚本使用的独立管理台服务入口；不自动启动聊天机器人。"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
import json
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
LABEL = "local.whatlike.web-console"
TARGET = f"gui/{os.getuid()}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def console_url():
    config = json.loads((ROOT / ".local/config.json").read_text())
    return f"http://127.0.0.1:{config.get('dashboard_port', 8765)}"


def ready():
    try:
        with urlopen(console_url() + "/api/health", timeout=2) as r:
            return json.load(r).get("app") == "whale-console-v1"
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("open", "start", "stop", "status"))
    args = parser.parse_args()
    if args.action == "status":
        print("管理台在线：" + console_url() if ready() else "管理台未启动")
        return
    if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get("WorkingDirectory") != str(ROOT):
        parser.exit(1, "同名管理台服务属于其他目录，未修改。\n")
    if args.action == "stop":
        subprocess.run(["launchctl", "bootout", TARGET], capture_output=True)
        PLIST.unlink(missing_ok=True)
        return
    if not ready():
        (ROOT / ".local").mkdir(mode=0o700, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        service = {"Label": LABEL, "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": True,
                   "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-u", str(ROOT / "web_console.py")],
                   "ThrottleInterval": 10, "StandardOutPath": str(ROOT / ".local/console.log"),
                   "StandardErrorPath": str(ROOT / ".local/console.log")}
        PLIST.write_bytes(plistlib.dumps(service))
        PLIST.chmod(0o600)
        subprocess.run(["launchctl", "bootout", TARGET], capture_output=True)
        deadline = time.monotonic() + 30
        while subprocess.run(["launchctl", "print", TARGET], capture_output=True).returncode == 0:
            if time.monotonic() > deadline:
                parser.exit(1, "旧管理台尚未退出，请稍后重试。\n")
            time.sleep(.1)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
        deadline = time.monotonic() + 20
        while not ready():
            if time.monotonic() > deadline:
                parser.exit(1, "管理台未能启动，请检查 .local/console.log。\n")
            time.sleep(.2)
    if args.action == "open":
        subprocess.run(["open", console_url()], check=True)
    print("管理台已打开：" + console_url())


if __name__ == "__main__":
    main()
