#!/usr/bin/env python3
"""SSJ VPN subscription server (stdlib only).

Serves /<token> as a base64 link list with the headers Happ and V2RayTun read
(profile-title, profile-update-interval). Everything else is 404.
"""
import base64
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SUB_FILE = os.environ.get("SSJ_SUB_FILE", "/etc/ssj-sub/links.txt")
TOKEN = os.environ["SSJ_SUB_TOKEN"]
TITLE = os.environ.get("SSJ_SUB_TITLE", "SSJ VPN")
UPDATE_HOURS = os.environ.get("SSJ_SUB_UPDATE_HOURS", "12")
PORT = int(os.environ.get("SSJ_SUB_PORT", "2096"))


class Handler(BaseHTTPRequestHandler):
    server_version = "nginx"
    sys_version = ""

    def do_GET(self):
        if self.path.split("?", 1)[0].rstrip("/") != "/" + TOKEN:
            self.send_error(404)
            return
        with open(SUB_FILE, "rb") as f:
            body = base64.b64encode(f.read().strip() + b"\n")
        title = base64.b64encode(TITLE.encode()).decode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("profile-title", "base64:" + title)
        self.send_header("profile-update-interval", UPDATE_HOURS)
        self.send_header("Content-Disposition", 'inline; filename="SSJ_VPN"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
