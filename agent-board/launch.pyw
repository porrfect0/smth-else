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


TITLE = "Agent Board"


class WindowApi:
    """Called from the page's Win98 title bar (the native frame is hidden)."""

    def __init__(self):
        self._window = None
        self._restore_rect = None

    def _hwnd(self):
        return self._window.native.Handle.ToInt32()

    def minimize(self):
        self._window.minimize()

    def toggle_maximize(self):
        # A borderless WinForms window maximized by Windows would cover the taskbar,
        # so "maximize" is done by hand: fill the monitor's work area.
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = self._hwnd()
        flags = 0x0004 | 0x0040  # SWP_NOZORDER | SWP_SHOWWINDOW
        if self._restore_rect:
            l, t, r, b = self._restore_rect
            self._restore_rect = None
            user32.SetWindowPos(hwnd, None, l, t, r - l, b - t, flags)
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        self._restore_rect = (rect.left, rect.top, rect.right, rect.bottom)
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(info))
        w = info.rcWork
        user32.SetWindowPos(hwnd, None, w.left, w.top, w.right - w.left, w.bottom - w.top, flags)
        return True

    def resize(self, width, height):
        self._restore_rect = None
        self._window.resize(max(480, int(width)), max(320, int(height)))

    def set_title(self, title):
        self._window.set_title(str(title)[:100])

    def close(self):
        self._window.destroy()


def focus_existing_window():
    """Bring an already open Agent Board window to the front; True if there was one."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if buf.value.endswith(TITLE) and cls.value.startswith("WindowsForms"):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    for hwnd in found:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    return bool(found)


def open_native_window(url):
    """Frameless WebView2 window whose only title bar is the page's Win98 one."""
    try:
        import webview
    except ImportError:
        return False
    if focus_existing_window():
        return True
    api = WindowApi()
    api._window = webview.create_window(
        TITLE, url + "#native", js_api=api, width=1100, height=760, min_size=(480, 320),
        frameless=True, easy_drag=False, background_color="#C0C0C0", text_select=True,
    )
    icon = APP_DIR / "web" / "icons" / "app.ico"
    webview.start(private_mode=False, storage_path=str(DATA_DIR / "webview"),
                  icon=str(icon) if icon.exists() else None)
    return True


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
        try:
            if open_native_window(url):
                return
        except Exception as e:  # WebView2/pythonnet problems: fall back to a browser window
            print("native window failed:", e, file=sys.stderr)
        # App mode of a Chromium browser gives a standalone window with its own taskbar button.
        browser = chromium_browser()
        if browser:
            subprocess.Popen([browser, f"--app={url}", "--window-size=1100,760"])
            return
    webbrowser.open(url)


if __name__ == "__main__":
    main()
