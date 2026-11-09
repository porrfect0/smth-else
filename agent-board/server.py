#!/usr/bin/env python3
"""Agent Board: watches marker folders, keeps agent statuses and reminders, serves the UI.

Stdlib only. Run: python server.py  (Windows: start.bat / AgentBoard.vbs)
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
DATA_DIR = Path(os.environ.get("AGENT_BOARD_HOME", Path.home() / "AgentBoard"))
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"

STATES = {"working", "waiting", "done", "blocked", "idle"}
NOTIFY_STATES = {"done", "waiting", "blocked"}
MAX_EVENTS = 500
POLL_SECONDS = 1.5
# A marker that is still unparsable after this long is moved to bad/ instead of retried.
PARTIAL_WRITE_GRACE = 10

DEFAULT_CONFIG = {
    "port": 8098,
    "lan": True,
    "notify": True,
    "stale_minutes": 90,
    "watch": [{"path": str(DATA_DIR / "markers"), "project": ""}],
}

lock = threading.Lock()


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json_atomic(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


DATA_DIR.mkdir(parents=True, exist_ok=True)
config = {**DEFAULT_CONFIG, **load_json(CONFIG_FILE, {})}
if not config.get("key"):
    config["key"] = secrets.token_hex(6)
write_json_atomic(CONFIG_FILE, config)

state = load_json(STATE_FILE, {})
state.setdefault("agents", {})
state.setdefault("todos", [])
state.setdefault("events", [])
state.setdefault("next_id", 1)
state["version"] = state.get("version", 0) + 1


def save_state():
    state["version"] += 1
    write_json_atomic(STATE_FILE, state)


# --- notifications --------------------------------------------------------------

TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:AB_TITLE)
$b = [Security.SecurityElement]::Escape($env:AB_BODY)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual></toast>")
# Windows PowerShell's own AppUserModelID: lets toasts show without registering an app.
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def notify(title, body):
    if not config.get("notify") or os.name != "nt":
        return
    # Agent text goes through env vars, never into the script source.
    env = {**os.environ, "AB_TITLE": title[:200], "AB_BODY": body[:500]}
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", TOAST_PS],
            env=env, creationflags=0x08000000,  # CREATE_NO_WINDOW
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


# --- marker processing ----------------------------------------------------------

def clean(value, limit=500):
    return str(value).strip()[:limit] if value is not None else ""


def parse_marker(raw, default_project, mtime):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("marker must be a JSON object")
    ev = {
        "project": clean(data.get("project") or default_project, 60),
        "agent": clean(data.get("agent"), 60),
        "state": clean(data.get("state"), 20).lower(),
        "task": clean(data.get("task")),
        "to": clean(data.get("to"), 60),
        "note": clean(data.get("note"), 1000),
        "result": clean(data.get("result"), 400),
        "time": clean(data.get("time"), 40) or datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
    }
    if not ev["project"] or not ev["agent"]:
        raise ValueError("project and agent are required")
    if ev["state"] not in STATES:
        raise ValueError(f"state must be one of {sorted(STATES)}")
    return ev


def open_todo(kind, ev):
    for t in state["todos"]:
        if t["done_at"] is None and t["kind"] == kind and t["project"] == ev["project"] and t["agent"] == ev["agent"]:
            if kind != "forward" or (t["task"] == ev["task"] and t["to"] == ev["to"]):
                return t
    return None


def apply_event(ev):
    key = f'{ev["project"]}/{ev["agent"]}'
    agents = state["agents"]
    agent = agents.get(key) or {"project": ev["project"], "agent": ev["agent"], "first_seen": ev["time"]}
    agent.update({k: ev[k] for k in ("state", "task", "to", "note", "result")})
    agent["updated"] = ev["time"]
    agents[key] = agent

    state["events"].append(ev)
    del state["events"][:-MAX_EVENTS]

    # The agent moved on, so any pending "reply to me" reminder is resolved.
    if ev["state"] not in ("waiting", "blocked"):
        for t in state["todos"]:
            if t["done_at"] is None and t["kind"] in ("reply", "blocked") and t["project"] == ev["project"] and t["agent"] == ev["agent"]:
                t["done_at"] = ev["time"]
                t["auto"] = True

    kind = {"done": "forward" if ev["to"] else None, "waiting": "reply", "blocked": "blocked"}.get(ev["state"])
    if kind:
        existing = open_todo(kind, ev)
        if existing:
            existing.update({"task": ev["task"], "note": ev["note"], "result": ev["result"], "created": ev["time"]})
        else:
            state["todos"].append({
                "id": state["next_id"], "kind": kind, "project": ev["project"], "agent": ev["agent"],
                "to": ev["to"], "task": ev["task"], "note": ev["note"], "result": ev["result"],
                "created": ev["time"], "done_at": None, "auto": False,
            })
            state["next_id"] += 1
    # Closed reminders older than the event log are dropped to keep state small.
    open_or_recent = [t for t in state["todos"] if t["done_at"] is None]
    closed = [t for t in state["todos"] if t["done_at"] is not None][-100:]
    state["todos"] = sorted(open_or_recent + closed, key=lambda t: t["id"])

    if ev["state"] in NOTIFY_STATES:
        who = f'{ev["agent"]} ({ev["project"]})'
        if ev["state"] == "done":
            title = f"{who} закончил"
            body = ev["task"] + (f"\nПереслать: {ev['to']}" if ev["to"] else "")
        elif ev["state"] == "waiting":
            title, body = f"{who} ждёт ответа", ev["task"] or ev["note"]
        else:
            title, body = f"{who} заблокирован", ev["note"] or ev["task"]
        notify(title, body.strip() or " ")


def scan_once():
    changed = False
    for watch in config["watch"]:
        folder = Path(watch["path"])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime)
        except OSError:
            continue
        for f in files:
            try:
                mtime = f.stat().st_mtime
                raw = f.read_text(encoding="utf-8-sig")
                ev = parse_marker(raw, watch.get("project", ""), mtime)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as e:
                if time.time() - mtime > PARTIAL_WRITE_GRACE:
                    bad = folder / "bad"
                    bad.mkdir(exist_ok=True)
                    try:
                        os.replace(f, bad / f.name)
                        (bad / (f.name + ".error.txt")).write_text(str(e), encoding="utf-8")
                    except OSError:
                        pass
                continue
            try:
                f.unlink()
            except OSError:
                continue  # still locked by the writer; pick it up next round
            with lock:
                apply_event(ev)
                changed = True
    if changed:
        with lock:
            save_state()


def watcher():
    while True:
        try:
            scan_once()
        except Exception as e:  # keep watching whatever happens
            print("scan error:", e, file=sys.stderr)
        time.sleep(POLL_SECONDS)


# --- HTTP -----------------------------------------------------------------------

def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png", ".woff": "font/woff",
        ".woff2": "font/woff2", ".ico": "image/x-icon", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    server_version = "AgentBoard"

    def log_message(self, *args):
        pass

    def authorized(self, query):
        if self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        cookie = self.headers.get("Cookie", "")
        return f"ab_key={config['key']}" in cookie or query.get("key", [""])[0] == config["key"]

    def send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        if not self.authorized(query):
            self.send(403, "Нет доступа: откройте ссылку из меню «Подключить телефон».".encode(), "text/plain; charset=utf-8")
            return
        if "key" in query:
            self.send(302, extra={"Location": "/", "Set-Cookie": f"ab_key={config['key']}; Max-Age=315360000; Path=/; SameSite=Strict"})
            return
        if url.path == "/api/state":
            with lock:
                payload = {k: state[k] for k in ("version", "agents", "todos")}
                payload["events"] = state["events"][-150:]
                payload["config"] = {"stale_minutes": config["stale_minutes"], "notify": config["notify"],
                                     "watch": config["watch"], "lan": config["lan"]}
                payload["server_time"] = now_iso()
            self.send_json(payload)
            return
        if url.path == "/api/pairing":
            port = self.server.server_address[1]
            self.send_json({"url": f"http://{lan_ip()}:{port}/?key={config['key']}", "lan": config["lan"]})
            return
        rel = "index.html" if url.path in ("", "/") else urllib.parse.unquote(url.path.lstrip("/"))
        target = (WEB_DIR / rel).resolve()
        if WEB_DIR not in target.parents or not target.is_file():
            self.send(404, b"not found", "text/plain")
            return
        self.send(200, target.read_bytes(), MIME.get(target.suffix, "application/octet-stream"))

    def do_POST(self):
        url = urllib.parse.urlsplit(self.path)
        if not self.authorized({}):
            self.send_json({"error": "forbidden"}, 403)
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self.send_json({"error": "bad json"}, 400)
            return
        parts = url.path.strip("/").split("/")
        with lock:
            if parts[:2] == ["api", "todo"] and len(parts) == 4 and parts[3] in ("done", "undo"):
                todo = next((t for t in state["todos"] if str(t["id"]) == parts[2]), None)
                if not todo:
                    self.send_json({"error": "not found"}, 404)
                    return
                todo["done_at"] = now_iso() if parts[3] == "done" else None
                todo["auto"] = False
            elif url.path == "/api/agent/forget":
                state["agents"].pop(f'{body.get("project")}/{body.get("agent")}', None)
            elif url.path == "/api/config":
                for k in ("notify", "lan"):
                    if isinstance(body.get(k), bool):
                        config[k] = body[k]
                if isinstance(body.get("stale_minutes"), int) and body["stale_minutes"] > 0:
                    config["stale_minutes"] = body["stale_minutes"]
                if isinstance(body.get("watch"), list):
                    config["watch"] = [{"path": clean(w.get("path"), 400), "project": clean(w.get("project"), 60)}
                                       for w in body["watch"] if isinstance(w, dict) and clean(w.get("path"))]
                write_json_atomic(CONFIG_FILE, config)
            else:
                self.send_json({"error": "not found"}, 404)
                return
            save_state()
        self.send_json({"ok": True})


def main():
    threading.Thread(target=watcher, daemon=True).start()
    host = "0.0.0.0" if config["lan"] else "127.0.0.1"
    try:
        httpd = ThreadingHTTPServer((host, config["port"]), Handler)
    except OSError:
        print(f"port {config['port']} is busy: Agent Board is probably already running", file=sys.stderr)
        sys.exit(0)
    print(f"Agent Board: http://127.0.0.1:{config['port']}/  data: {DATA_DIR}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
