"""使用用户级 launchd 管理 QQ 连接程序，不依赖终端窗口存活。"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import time

ROOT = Path(__file__).resolve().parent
LABEL = "local.whatlike.deepseek-bot"
TARGET = f"gui/{os.getuid()}/{LABEL}"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def wait_for_exit(timeout=30):
    """bootout 返回时服务可能仍在终止；等待卸载后才允许 bootstrap。"""
    deadline = time.monotonic() + timeout
    while subprocess.run(["launchctl", "print", TARGET], capture_output=True).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError("旧机器人仍在退出，请稍后再试；未重复启动新进程")
        time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("start", "stop", "status"))
    args = parser.parse_args()
    if args.action == "stop":
        if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get("WorkingDirectory") != str(ROOT):
            parser.exit(1, "发现其他项目的同名服务配置，不停止。\n")
        result = subprocess.run(["launchctl", "bootout", TARGET], capture_output=True, text=True)
        if result.returncode:
            status = subprocess.run(["launchctl", "print", TARGET], capture_output=True)
            if status.returncode == 0:
                parser.exit(1, "后台服务仍在运行，无法停止。\n")
        wait_for_exit()
        PLIST.unlink(missing_ok=True)
        print("机器人已停止，登录自启配置已移除。")
    elif args.action == "status":
        result = subprocess.run(["launchctl", "print", TARGET], capture_output=True, text=True)
        if result.returncode:
            print("机器人未运行。")
            return
        for line in result.stdout.splitlines():
            if line.strip().startswith(("state =", "pid =", "last exit code =")):
                print(line.strip())
    else:
        ROOT.joinpath(".local").mkdir(mode=0o700, exist_ok=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        if PLIST.exists():
            existing = plistlib.loads(PLIST.read_bytes())
            if existing.get("WorkingDirectory") != str(ROOT):
                parser.error("发现其他项目的同名服务，不覆盖")
        config = {
            "Label": LABEL,
            "ProgramArguments": [str(ROOT / ".venv" / "bin" / "python"), "-u", str(ROOT / "qq_bot.py")],
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "StandardOutPath": str(ROOT / ".local" / "bot.log"),
            "StandardErrorPath": str(ROOT / ".local" / "bot.log"),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }
        PLIST.write_bytes(plistlib.dumps(config))
        PLIST.chmod(0o600)
        subprocess.run(["launchctl", "bootout", TARGET], capture_output=True)
        wait_for_exit()
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
        print("后台机器人已启动，用户登录后也会自动启动。")


if __name__ == "__main__":
    main()
