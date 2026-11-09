"""Windows launcher: starts the Agent Board server in the background (once) and opens its window.

pythonw launch.pyw               -> server + window
pythonw launch.pyw --background  -> server only (autostart)
"""
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AGENT_BOARD_HOME", Path.home() / "AgentBoard"))


def port():
    try:
        return int(json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8")).get("port", 8098))
    except (OSError, ValueError):
        return 8098


def running(p):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", p)) == 0


def main():
    p = port()
    if not running(p):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(DATA_DIR / "server.log", "a", encoding="utf-8")
        flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0  # detached, new group, no window
        subprocess.Popen([sys.executable, str(APP_DIR / "server.py")], cwd=APP_DIR,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL, creationflags=flags)
        for _ in range(50):
            if running(p):
                break
            time.sleep(0.1)
    if "--background" in sys.argv:
        return
    url = f"http://127.0.0.1:{p}/"
    if os.name == "nt":
        # Edge app mode gives a standalone window with its own taskbar button.
        try:
            subprocess.Popen(["cmd", "/c", "start", "", "msedge", f"--app={url}", "--window-size=1100,760"],
                             creationflags=0x08000000)
            return
        except OSError:
            pass
    webbrowser.open(url)


if __name__ == "__main__":
    main()
