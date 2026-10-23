"""使用独立 launchd 服务固定 Ollama 原生模型路径，无需兼容链接。"""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import time

ROOT = Path(__file__).resolve().parent
LABEL = "local.whatlike.ollama"
TARGET = f"gui/{os.getuid()}/{LABEL}"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
MODEL_STORE = ROOT / "runtime/ollama/models"
BINARY = Path("/Applications/Ollama.app/Contents/Resources/ollama")


def service_config():
    return {"Label": LABEL,
            "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-u", str(ROOT / "ollamactl.py"), "serve"],
            "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "EnvironmentVariables": {"OLLAMA_MODELS": str(MODEL_STORE), "OLLAMA_HOST": "127.0.0.1:11434"},
            "StandardOutPath": str(ROOT / ".local/ollama.log"),
            "StandardErrorPath": str(ROOT / ".local/ollama.log")}


def unload():
    subprocess.run(["launchctl", "bootout", TARGET], capture_output=True)
    deadline = time.monotonic() + 30
    while subprocess.run(["launchctl", "print", TARGET], capture_output=True).returncode == 0:
        if time.monotonic() >= deadline:
            raise RuntimeError("旧 Ollama 服务尚未退出，未启动第二个实例")
        time.sleep(0.1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="管理当前工作目录的原生 Ollama 服务")
    parser.add_argument("action", choices=("start", "stop", "status", "serve"))
    args = parser.parse_args(argv)
    try:
        if args.action == "serve":
            # 登录后也设置环境变量，之后从桌面启动的应用可沿用同一模型路径。
            subprocess.run(["launchctl", "setenv", "OLLAMA_MODELS", str(MODEL_STORE)], check=True)
            env = {**os.environ, "OLLAMA_MODELS": str(MODEL_STORE), "OLLAMA_HOST": "127.0.0.1:11434"}
            os.execve(str(BINARY), [str(BINARY), "serve"], env)
        elif args.action == "status":
            result = subprocess.run(["launchctl", "print", TARGET], capture_output=True, text=True)
            if result.returncode:
                print("工作目录 Ollama 服务未加载。")
            else:
                for line in result.stdout.splitlines():
                    if line.strip().startswith(("state =", "pid =", "last exit code =")):
                        print(line.strip())
        else:
            if PLIST.exists() and plistlib.loads(PLIST.read_bytes()).get("WorkingDirectory") != str(ROOT):
                raise RuntimeError("发现其他目录的同名 Ollama 服务，未修改")
            if args.action == "stop":
                unload()
                PLIST.unlink(missing_ok=True)
                print("Ollama 后台服务已停止并移除登录自启。")
                return
            if not BINARY.is_file() or not (MODEL_STORE / "manifests").is_dir():
                raise RuntimeError("Ollama 程序或工作目录模型数据缺失")
            (ROOT / ".local").mkdir(mode=0o700, exist_ok=True)
            PLIST.parent.mkdir(parents=True, exist_ok=True)
            PLIST.write_bytes(plistlib.dumps(service_config()))
            PLIST.chmod(0o600)
            unload()
            subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], check=True)
            print("原生 Ollama 后台服务已启动，登录后自动启动；模型目录：" + str(MODEL_STORE))
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, "错误：" + str(exc) + "\n")


if __name__ == "__main__":
    main()
