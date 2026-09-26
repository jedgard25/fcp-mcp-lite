"""transcript-ui backend — the single editor frontend, stdlib only.

Reuses mcp/server.py as the brain (same validation, file->timeline
re-resolve, JSONL logging), exposes the slice-model API for index.html
(the FCP-styled card editor the in-process panel loads).

  make ui   ->  http://127.0.0.1:8765

API:
  GET  /              the editor page
  GET  /api/editor    snapshot (revision, timeline, title, duration, slices, edit_error)
  GET  /api/status    bridge reachability + versions
  POST /api/editor/edit  {revision, action, id, before_id?, words?} (commits immediately)
"""

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_DIR, "mcp"))

import server as mcp_server  # noqa: E402
import editor_service

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "index.html")


def _json(handler, obj, code=200):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    server_version = "transcript-ui/0.1.0"

    def log_message(self, fmt, *args):  # quiet; MCP JSONL is the log
        pass

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"_parse_error": True}

    def do_GET(self):
        with editor_service.LOCK:
            self._get()

    def _get(self):
        path = urlparse(self.path).path
        try:
            if path == "/" or path == "/index.html":
                with open(INDEX, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/editor":
                _json(self, editor_service.read())
            elif path == "/api/status":
                _json(self, mcp_server.bridge_status())
            else:
                _json(self, {"ok": False, "error": f"unknown route {path}"}, 404)
        except Exception as e:  # never drop the connection without an answer
            _json(self, {"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        # Serialize legacy web and native requests through snapshot + commit.
        # JSON-only writes reject cross-origin browser form submissions.
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + self.headers.get("Host", ""):
            return _json(self, {"ok": False, "error": "cross-origin writes refused"}, 403)
        if self.headers.get_content_type() != "application/json":
            return _json(self, {"ok": False, "error": "application/json required"}, 415)
        with editor_service.LOCK:
            self._post()

    def _post(self):
        path = urlparse(self.path).path
        body = self._read_json()
        try:
            if not isinstance(body, dict) or body.get("_parse_error"):
                return _json(self, {"ok": False, "error": "invalid JSON body"}, 400)
            if path == "/api/editor/edit":
                if not isinstance(body, dict):
                    return _json(self, {"ok": False, "error": "JSON object required"}, 400)
                _json(self, editor_service.edit(body))
            else:
                _json(self, {"ok": False, "error": f"unknown route {path}"}, 404)
        except Exception as e:
            _json(self, {"ok": False, "error": str(e)}, 500)


def main():
    ap = argparse.ArgumentParser(description="transcript-ui extension server (see module docstring)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"transcript-ui on http://{args.host}:{args.port} (repo {REPO_DIR})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
