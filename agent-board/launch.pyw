"""Windows launcher for Agent Board.

pythonw launch.pyw               -> server + desktop app with the main window
pythonw launch.pyw --hidden      -> server + desktop app with the window hidden (autostart; popups only)
pythonw launch.pyw --background  -> server only

The desktop app is a frameless WebView2 window (pywebview) whose only title bar is the page's
Win98 one. It also shows its own Win98-style popups instead of Windows notifications.
"""
import ctypes
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import wave
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AGENT_BOARD_HOME", Path.home() / "AgentBoard"))
TITLE = "Agent Board"
POPUP_W, POPUP_H, POPUP_GAP, MAX_POPUPS = 340, 150, 6, 4


def port():
    try:
        return int(json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8")).get("port", 8098))
    except (OSError, ValueError):
        return 8098


def running(p):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", p)) == 0


def http_json(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


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


# --- popup sound ---------------------------------------------------------------------

def popup_sound_file():
    """A short strummed chord in the spirit of Win98 sounds (synthesized, not Microsoft's file)."""
    path = DATA_DIR / "popup.wav"
    if path.exists():
        return path
    rate, length = 22050, 0.7
    notes = [(523.25, 0.00), (659.25, 0.03), (783.99, 0.06), (1046.5, 0.09)]
    frames = bytearray()
    for i in range(int(rate * length)):
        t = i / rate
        v = 0.0
        for freq, start in notes:
            if t >= start:
                dt = t - start
                env = math.exp(-dt * 5.5) * min(1.0, dt * 400)
                v += env * (math.sin(2 * math.pi * freq * dt) + 0.3 * math.sin(4 * math.pi * freq * dt))
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v * 0.22)) * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def play_popup_sound():
    try:
        import winsound

        winsound.PlaySound(str(popup_sound_file()), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except (ImportError, RuntimeError, OSError):
        pass


# --- desktop app ---------------------------------------------------------------------

class WindowApi:
    """Called from the main window's Win98 title bar and menu (the native frame is hidden)."""

    def __init__(self, desktop):
        self._desktop = desktop
        self._restore_rect = None

    def _hwnd(self):
        return self._desktop.main.native.Handle.ToInt32()

    def minimize(self):
        self._desktop.main.minimize()

    def toggle_maximize(self):
        # A borderless WinForms window maximized by Windows would cover the taskbar,
        # so "maximize" is done by hand: fill the monitor's work area.
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
        self._desktop.main.resize(max(480, int(width)), max(320, int(height)))

    def set_title(self, title):
        self._desktop.main.set_title(str(title)[:100])

    def close(self):
        # Closing only hides the window: the app keeps running to show popups.
        self._desktop.main.hide()

    def quit(self):
        self._desktop.quit()


class PopupApi:
    def __init__(self, desktop, alert):
        self._desktop = desktop
        self._alert = alert
        self._window = None

    def open(self):
        self._desktop.show_main(self._alert.get("todo"), self._alert.get("project"))
        self.close()

    def close(self):
        if self._window:
            self._desktop.close_popup(self._window)


class Desktop:
    def __init__(self, base, webview):
        self.base = base
        self.webview = webview
        self.main = None
        self.popups = {}  # slot -> window
        self.lock = threading.Lock()
        self.quitting = False

    # Main window ------------------------------------------------------------------
    def create_main(self, hidden):
        self.main = self.webview.create_window(
            TITLE, self.base + "#native", js_api=WindowApi(self), width=1100, height=760, min_size=(480, 320),
            frameless=True, easy_drag=False, background_color="#C0C0C0", text_select=True, hidden=hidden,
        )
        self.main.events.closing += self.on_main_closing

    def on_main_closing(self):
        if self.quitting:
            return True
        self.main.hide()
        return False  # cancel: Alt+F4 / taskbar close only hides

    def show_main(self, todo=None, project=None):
        self.main.show()
        self.main.restore()
        if todo is not None or project:
            self.main.evaluate_js(f"window.abFocus && window.abFocus({json.dumps(project)}, {json.dumps(todo)})")

    def quit(self):
        self.quitting = True
        for w in list(self.popups.values()):
            w.destroy()
        self.main.destroy()

    # Popups -----------------------------------------------------------------------
    def popup_position(self, slot):
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        work = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)  # SPI_GETWORKAREA, physical px
        scale = user32.GetDpiForSystem() / 96 if hasattr(user32, "GetDpiForSystem") else 1
        x = work.right / scale - POPUP_W - POPUP_GAP
        y = work.bottom / scale - (POPUP_H + POPUP_GAP) * (slot + 1)
        return int(x), int(y)

    def show_popup(self, alert, sound):
        with self.lock:
            if len(self.popups) >= MAX_POPUPS:
                oldest = min(self.popups)
                self.popups.pop(oldest).destroy()
            slot = next(i for i in range(MAX_POPUPS + 1) if i not in self.popups)
            x, y = self.popup_position(slot)
            api = PopupApi(self, alert)
            win = self.webview.create_window(
                f"{TITLE} — {alert.get('project', '')}", f"{self.base}popup.html?id={alert['id']}", js_api=api,
                width=POPUP_W, height=POPUP_H, x=x, y=y, resizable=False, frameless=True, easy_drag=False,
                on_top=True, focus=False, background_color="#C0C0C0",
            )
            api._window = win
            win.events.before_show += hide_from_taskbar
            win.events.closed += lambda: self.forget_popup(win)
            self.popups[slot] = win
        if sound:
            play_popup_sound()

    def close_popup(self, win):
        self.forget_popup(win)
        win.destroy()

    def forget_popup(self, win):
        with self.lock:
            for slot, w in list(self.popups.items()):
                if w is win:
                    del self.popups[slot]

    # Server polling ---------------------------------------------------------------
    def poll(self):
        after, show_seq = None, None
        while not self.quitting:
            try:
                d = http_json(f"{self.base}api/desktop?after={-1 if after is None else after}")
            except (OSError, ValueError):
                time.sleep(3)
                continue
            if after is None or d["last"] < after:
                # First poll (or server state was reset): old alerts are not replayed.
                after, show_seq = d["last"], d["show"]["seq"]
            for alert in d["alerts"]:
                after = max(after, alert["id"])
                try:
                    self.show_popup(alert, d.get("sound", True))
                except Exception as e:  # one broken popup must not stop the loop
                    print("popup failed:", e, file=sys.stderr)
            if d["show"]["seq"] != show_seq:
                show_seq = d["show"]["seq"]
                self.show_main(d["show"].get("todo"), d["show"].get("project"))
            time.sleep(1.5)


def hide_from_taskbar(window):
    """Popups are tool windows: no taskbar button. Runs before the form is first shown."""
    hwnd = window.native.Handle.ToInt32()
    user32 = ctypes.windll.user32
    ex = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, (ex | 0x00000080) & ~0x00040000)  # +TOOLWINDOW, -APPWINDOW


_mutex = None


def claim_single_instance():
    """True if this process is the only desktop app; the handle lives as long as the process."""
    global _mutex
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AgentBoardDesktop")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run_desktop(base, hidden):
    try:
        import webview
    except ImportError:
        return False
    if not claim_single_instance():
        if not hidden:
            http_json(base + "api/ui/show", {})
        return True
    desktop = Desktop(base, webview)
    desktop.create_main(hidden)
    icon = APP_DIR / "web" / "icons" / "app.ico"
    webview.start(desktop.poll, private_mode=False, storage_path=str(DATA_DIR / "webview"),
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
    hidden = "--hidden" in sys.argv
    url = f"http://127.0.0.1:{p}/"
    if os.name == "nt":
        try:
            if run_desktop(url, hidden):
                return
        except Exception as e:  # WebView2/pythonnet problems: fall back to a browser window
            print("desktop app failed:", e, file=sys.stderr)
        if hidden:
            return
        browser = chromium_browser()
        if browser:
            subprocess.Popen([browser, f"--app={url}", "--window-size=1100,760"])
            return
    webbrowser.open(url)


if __name__ == "__main__":
    main()
