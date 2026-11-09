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


CHROMIUM_EXES = {"msedge.exe", "chrome.exe", "thorium.exe", "brave.exe", "vivaldi.exe", "chromium.exe", "opera.exe"}


def chromium_browser():
    """Path of an installed Chromium-based browser (they support --app); the default one first."""
    import shlex
    import winreg

    def command(root, path):
        try:
            with winreg.OpenKey(root, path) as k:
                exe = shlex.split(winreg.QueryValue(k, None), posix=False)[0].strip('"')
                return exe if os.path.basename(exe).lower() in CHROMIUM_EXES and os.path.exists(exe) else None
        except (OSError, IndexError, ValueError):
            return None

    found = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice") as k:
            found.append(command(winreg.HKEY_CLASSES_ROOT, winreg.QueryValueEx(k, "ProgId")[0] + r"\shell\open\command"))
    except OSError:
        pass
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, r"SOFTWARE\Clients\StartMenuInternet") as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    found.append(command(root, rf"SOFTWARE\Clients\StartMenuInternet\{winreg.EnumKey(k, i)}\shell\open\command"))
        except OSError:
            pass
    return next((f for f in found if f), None)


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
        # App mode of a Chromium browser gives a standalone window with its own taskbar button.
        browser = chromium_browser()
        if browser:
            subprocess.Popen([browser, f"--app={url}", "--window-size=1100,760"])
            return
    webbrowser.open(url)


if __name__ == "__main__":
    main()
