#!/usr/bin/env python3
"""Agent Board: watches marker folders, keeps agent statuses and reminders, serves the UI.

Stdlib only. Run: python server.py  (Windows: install.ps1 creates shortcuts)
"""
import json
import os
import secrets
import socket
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
WAITING_FOR = {"owner_input", "owner_forward", "reviewer", "coordinator_review", "resource", "model_limit"}
MAX_EVENTS = 500
SEEN_IDS = 3000
POLL_SECONDS = 1.5
# A marker that is still unparsable after this long is moved to bad/ instead of retried.
PARTIAL_WRITE_GRACE = 10

DEFAULT_CONFIG = {
    "port": 8098,
    "lan": True,
    "notify": True,
    "popup_sound": True,
    "popup_volume": 80,
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
state.setdefault("seen_ids", [])
state.setdefault("alerts", [])
state.setdefault("next_id", 1)
state["version"] = state.get("version", 0) + 1
for _t in state["todos"]:
    _t.setdefault("forwarded_at", None)
    _t.setdefault("acked_at", None)
    _t.setdefault("message_id", f"AB-{_t['id']}")
    _t.setdefault("message", "")


def save_state():
    state["version"] += 1
    write_json_atomic(STATE_FILE, state)


# --- desktop popups ---------------------------------------------------------------
# Windows toasts are not used: the desktop app (launch.pyw) polls /api/desktop and shows
# its own Win98-style popups, so the system notification sound never plays.

MAX_ALERTS = 100
ui_show = {"seq": 0, "todo": None, "project": None}


def push_alert(kind, project, title, body, todo_id):
    if not config.get("notify"):
        return
    state["alert_seq"] = state.get("alert_seq", 0) + 1
    state["alerts"].append({"id": state["alert_seq"], "kind": kind, "project": project, "title": title[:200],
                            "body": (body or "")[:600], "todo": todo_id, "time": now_iso()})
    del state["alerts"][:-MAX_ALERTS]


# --- marker processing ----------------------------------------------------------

def clean(value, limit=500):
    return str(value).strip()[:limit] if value is not None else ""


def parse_marker(raw, folder_project, mtime):
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("marker must be a JSON object")
    try:
        seq = int(data["seq"]) if data.get("seq") is not None else None
    except (TypeError, ValueError):
        seq = None
    ev = {
        # The folder decides the project: an agent's cwd is not a reliable project identity.
        "project": clean(folder_project or data.get("project"), 60),
        "agent": clean(data.get("agent"), 60),
        "state": clean(data.get("state"), 20).lower(),
        "waiting_for": clean(data.get("waiting_for"), 40).lower(),
        "phase": clean(data.get("phase"), 60),
        "task": clean(data.get("task")),
        "to": clean(data.get("to"), 60),
        "note": clean(data.get("note"), 1000),
        "result": clean(data.get("result"), 400),
        "message": clean(data.get("message"), 20000),
        "message_id": clean(data.get("message_id"), 80),
        "ack": clean(data.get("ack"), 80),
        "needs_forward": data.get("needs_forward") is True,
        "session": clean(data.get("session_id"), 80),
        "seq": seq,
        "event_id": clean(data.get("event_id"), 120),
        "time": clean(data.get("time"), 40) or datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"),
        "received": now_iso(),
    }
    if not ev["project"] or not ev["agent"]:
        raise ValueError("project and agent are required")
    if ev["state"] not in STATES:
        raise ValueError(f"state must be one of {sorted(STATES)}")
    if ev["waiting_for"] and ev["waiting_for"] not in WAITING_FOR:
        raise ValueError(f"waiting_for must be one of {sorted(WAITING_FOR)}")
    return ev


def same_agent(t, project, agent):
    return t["project"] == project and t["agent"].casefold() == agent.casefold()


def forward_kind(ev):
    if not ev["to"]:
        return False
    return ev["state"] == "done" or ev["needs_forward"] or ev["waiting_for"] in ("owner_forward", "coordinator_review", "reviewer")


def todo_kind(ev):
    if forward_kind(ev):
        return "forward"
    if ev["state"] == "blocked":
        return "blocked"
    if ev["state"] == "waiting" and ev["waiting_for"] == "model_limit":
        return "limit"
    if ev["state"] == "waiting" and ev["waiting_for"] in ("", "owner_input"):
        return "reply"
    return None


def default_message(ev):
    lines = [f"{ev['agent']} ({ev['project']}): {ev['task'] or 'работа завершена'}"]
    if ev["note"]:
        lines.append(ev["note"])
    if ev["result"]:
        lines.append(f"Результат: {ev['result']}")
    return "\n".join(lines)


def acknowledge(ev):
    """Close forward reminders addressed to this agent that this event confirms."""
    for t in state["todos"]:
        if t["kind"] != "forward" or t["acked_at"] or t["project"] != ev["project"]:
            continue
        if t["to"].casefold() != ev["agent"].casefold():
            continue
        explicit = ev["ack"] and ev["ack"] == t["message_id"]
        implicit = not ev["ack"] and ev["state"] == "working" and t["forwarded_at"] and ev["task"] and ev["task"] == t["task"]
        if explicit or implicit:
            t["acked_at"] = ev["time"]
            t["done_at"] = t["done_at"] or ev["received"]
            t["forwarded_at"] = t["forwarded_at"] or ev["received"]


def apply_event(ev):
    key = f'{ev["project"]}/{ev["agent"]}'
    agents = state["agents"]
    agent = agents.get(key) or {"project": ev["project"], "agent": ev["agent"], "first_seen": ev["time"]}

    state["events"].append(ev)
    del state["events"][:-MAX_EVENTS]
    acknowledge(ev)

    # An older event of the same session (delivered late over SMB) must not overwrite newer status.
    if ev["session"] and ev["seq"] is not None and agent.get("session") == ev["session"] \
            and agent.get("seq") is not None and ev["seq"] <= agent["seq"]:
        return
    for k in ("state", "waiting_for", "phase", "task", "to", "note", "result", "session", "seq"):
        agent[k] = ev[k]
    agent["updated"] = ev["time"]
    agent["received"] = ev["received"]
    agents[key] = agent

    # The agent moved on, so its own "needs your answer" reminders are resolved.
    if ev["state"] not in ("waiting", "blocked"):
        for t in state["todos"]:
            if t["done_at"] is None and t["kind"] in ("reply", "blocked", "limit") and same_agent(t, ev["project"], ev["agent"]):
                t["done_at"] = ev["received"]
                t["auto"] = True

    kind = todo_kind(ev)
    todo_id = None
    if kind:
        message = ev["message"] or default_message(ev)
        existing = next((t for t in state["todos"] if t["done_at"] is None and t["kind"] == kind
                         and same_agent(t, ev["project"], ev["agent"])
                         and (kind != "forward" or (t["task"] == ev["task"] and t["to"] == ev["to"]))), None)
        if existing:
            todo_id = existing["id"]
        if existing and not existing["forwarded_at"]:
            existing.update({"note": ev["note"], "result": ev["result"], "created": ev["time"],
                             "waiting_for": ev["waiting_for"], "message": message,
                             "message_id": ev["message_id"] or existing["message_id"]})
        elif not existing:
            todo_id = state["next_id"]
            state["next_id"] += 1
            state["todos"].append({
                "id": todo_id, "kind": kind, "project": ev["project"], "agent": ev["agent"],
                "to": ev["to"], "task": ev["task"], "note": ev["note"], "result": ev["result"],
                "waiting_for": ev["waiting_for"], "message": message,
                "message_id": ev["message_id"] or f"AB-{todo_id}",
                "created": ev["time"], "forwarded_at": None, "acked_at": None, "done_at": None, "auto": False,
            })
    open_todos = [t for t in state["todos"] if t["done_at"] is None]
    closed = sorted((t for t in state["todos"] if t["done_at"] is not None), key=lambda t: t["done_at"])[-100:]
    state["todos"] = sorted(open_todos + closed, key=lambda t: t["id"])

    who = ev["agent"]
    alert = {
        "forward": (f"{who}: переслать → {ev['to']}", ev["task"] or ev["note"]),
        "reply": (f"{who} ждёт вашего ответа", ev["task"] or ev["note"]),
        "blocked": (f"{who} заблокирован", ev["note"] or ev["task"]),
        "limit": (f"{who}: закончился лимит модели", ev["note"] or ev["task"]),
    }.get(kind)
    if not alert and ev["state"] == "done":
        alert = (f"{who} закончил", ev["task"])
    if alert:
        push_alert(kind or "done", ev["project"], alert[0], alert[1], todo_id)


def archive(f, folder, sub):
    target = folder / sub / datetime.now().strftime("%Y-%m-%d")
    target.mkdir(parents=True, exist_ok=True)
    dest = target / f.name
    if dest.exists():
        dest = target / f"{f.stem}-{secrets.token_hex(3)}{f.suffix}"
    os.replace(f, dest)
    return dest


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
                    try:
                        dest = archive(f, folder, "bad")
                        dest.with_name(dest.name + ".error.txt").write_text(str(e), encoding="utf-8")
                    except OSError:
                        pass
                continue
            try:
                archive(f, folder, "processed")
            except OSError:
                continue  # still locked by the writer; pick it up next round
            with lock:
                if ev["event_id"] and ev["event_id"] in state["seen_ids"]:
                    continue
                if ev["event_id"]:
                    state["seen_ids"].append(ev["event_id"])
                    del state["seen_ids"][:-SEEN_IDS]
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
                                     "watch": config["watch"], "lan": config["lan"],
                                     "popup_sound": config.get("popup_sound", True),
                                     "popup_volume": config.get("popup_volume", 80)}
                payload["server_time"] = now_iso()
                payload["host"] = socket.gethostname()
            self.send_json(payload)
            return
        if url.path == "/api/desktop":
            # Polled by the desktop app: new popups after ?after=<id> and requests to show the main window.
            try:
                after = int(query.get("after", ["-1"])[0])
            except ValueError:
                after = -1
            with lock:
                last = state.get("alert_seq", 0)
                alerts = [a for a in state["alerts"] if after >= 0 and a["id"] > after]
                self.send_json({"last": last, "alerts": alerts, "show": dict(ui_show),
                                "sound": config.get("popup_sound", True), "volume": config.get("popup_volume", 80)})
            return
        if url.path == "/api/alert":
            with lock:
                alert = next((a for a in state["alerts"] if str(a["id"]) == query.get("id", [""])[0]), None)
            self.send_json(alert or {"error": "not found"}, 200 if alert else 404)
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
            if parts[:2] == ["api", "todo"] and len(parts) == 4 and parts[3] in ("forwarded", "done", "undo"):
                todo = next((t for t in state["todos"] if str(t["id"]) == parts[2]), None)
                if not todo:
                    self.send_json({"error": "not found"}, 404)
                    return
                if parts[3] == "forwarded":
                    # Owner's own mark: the app cannot see a paste into another window.
                    todo["forwarded_at"] = todo["forwarded_at"] or now_iso()
                elif parts[3] == "done":
                    todo["done_at"] = now_iso()
                else:
                    todo["done_at"] = None
                    todo["forwarded_at"] = None
                    todo["acked_at"] = None
                todo["auto"] = False
            elif url.path == "/api/ui/show":
                # A second launch (desktop shortcut) asks the running desktop app to show its window.
                ui_show["seq"] += 1
                ui_show["todo"] = body.get("todo") if isinstance(body.get("todo"), int) else None
                ui_show["project"] = clean(body.get("project"), 60) or None
                self.send_json({"ok": True})
                return
            elif url.path == "/api/agent/forget":
                state["agents"].pop(f'{body.get("project")}/{body.get("agent")}', None)
            elif url.path == "/api/config":
                for k in ("notify", "lan", "popup_sound"):
                    if isinstance(body.get(k), bool):
                        config[k] = body[k]
                if isinstance(body.get("popup_volume"), int) and 0 <= body["popup_volume"] <= 100:
                    config["popup_volume"] = body["popup_volume"]
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
