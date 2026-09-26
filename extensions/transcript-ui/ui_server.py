"""transcript-ui backend — optional extension, stdlib only.

Reuses mcp/server.py as the brain (same validation, file->timeline
re-resolve, JSONL logging), exposes a tiny autocommitting HTTP API for
index.html. No new deps, nothing imported by core.

  make ui   ->  http://127.0.0.1:8765  (agent-launchable via background bash)

API (all POSTs commit immediately, dry_run=False — change = FCP moves):
  GET  /api/status   bridge reachability + versions
  GET  /api/story    full-detail story lines (id, text, start/end_word, crumbs)
  GET  /api/review   take-group gate
  POST /api/delete   {ids:[...]}            -> delete_lines(commit)
  POST /api/move     {id, after_id|before_id} -> move_line(commit, single hop)
  POST /api/reorder  {keep:[ids in order]}  -> apply_story(commit, full order)
  POST /api/trim     {start_index, count}   -> delete_words(commit, edge trim)
  POST /api/split    {at_index} | {id, after_word} -> split_words(commit, Enter-to-split)
  POST /api/cut      {spans:[[a,b],...]}    -> cut_spans(commit, chunk continue)
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
            elif path == "/api/story":
                story = mcp_server.get_story(detail="full", limit=500)
                # Lightweight word tokens so clients can render literal text
                # (id-indexed words, no timings — indices are the API).
                try:
                    t = mcp_server._last_transcript() or {}
                    story["words"] = [{"i": w["i"], "w": w["w"]}
                                      for w in t.get("words", [])]
                except Exception:
                    story["words"] = []
                _json(self, story)
            elif path == "/api/review":
                _json(self, mcp_server.review())
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
            elif path == "/api/delete":
                ids = body.get("ids") or []
                if not isinstance(ids, list) or not ids:
                    return _json(self, {"ok": False, "error": "ids:[...] required"}, 400)
                _json(self, mcp_server.delete_lines(ids=ids, dry_run=False))
            elif path == "/api/move":
                lid = body.get("id")
                after, before = body.get("after_id"), body.get("before_id")
                if not lid or (after is None) == (before is None):
                    return _json(self, {"ok": False, "error": "need id + exactly one of after_id/before_id"}, 400)
                _json(self, mcp_server.move_line(id=lid, after_id=after, before_id=before, dry_run=False))
            elif path == "/api/reorder":
                keep = body.get("keep") or []
                if not isinstance(keep, list) or not keep:
                    return _json(self, {"ok": False, "error": "keep:[ids in order] required"}, 400)
                # Auto-follow chunking: same keep list re-plans from live clips.
                report = None
                for _ in range(10):
                    report = mcp_server.apply_story(keep=keep, dry_run=False)
                    if not isinstance(report, dict) or not report.get("remaining"):
                        break
                _json(self, report if isinstance(report, dict) else {"ok": False, "error": "empty reorder result"})
            elif path == "/api/trim":
                # {start_index, count} or {ranges:[[start,count],...]} for
                # multi-range literal edits (one validated batch, one undo depth).
                ranges = body.get("ranges")
                if ranges is not None:
                    try:
                        ranges = [[int(a), int(b)] for a, b in ranges]
                    except (TypeError, ValueError):
                        return _json(self, {"ok": False, "error": "ranges must be [[start,count],...]"}, 400)
                    if not ranges or any(b <= 0 for _, b in ranges):
                        return _json(self, {"ok": False, "error": "ranges must be non-empty with count > 0"}, 400)
                    _json(self, mcp_server.delete_words(ranges=ranges, dry_run=False))
                    return
                try:
                    si, co = int(body.get("start_index", 0)), int(body.get("count", 0))
                except (TypeError, ValueError):
                    return _json(self, {"ok": False, "error": "start_index/count must be ints"}, 400)
                if co <= 0:
                    return _json(self, {"ok": False, "error": "count must be > 0"}, 400)
                _json(self, mcp_server.delete_words(start_index=si, count=co, dry_run=False))
            elif path == "/api/split":
                # {at_index} or line-scoped {id, after_word} (split AFTER that
                # word — clients speak line objects, the verb speaks indices).
                at = body.get("at_index")
                if at is None and body.get("id") is not None and body.get("after_word") is not None:
                    try:
                        aw = int(body["after_word"])
                    except (TypeError, ValueError):
                        return _json(self, {"ok": False, "error": "after_word must be an int"}, 400)
                    story = mcp_server.get_story(detail="full", limit=500)
                    line = next((ln for ln in story.get("lines", []) if ln.get("id") == body["id"]), None)
                    if line is None:
                        return _json(self, {"ok": False, "error": f"unknown line {body['id']} — refresh"}, 400)
                    if not (line["start_word"] <= aw < line["end_word"]):
                        return _json(self, {"ok": False, "error": "after_word must be inside the line (not past its last word)"}, 400)
                    at = aw + 1
                try:
                    at = int(at)
                except (TypeError, ValueError):
                    return _json(self, {"ok": False, "error": "need at_index or {id, after_word}"}, 400)
                _json(self, mcp_server.split_words(at_index=at, dry_run=False))
            elif path == "/api/cut":
                spans = body.get("spans") or body.get("pending") or []
                try:
                    spans = [[float(a), float(b)] for a, b in spans]
                except (TypeError, ValueError):
                    return _json(self, {"ok": False, "error": "spans must be [[a,b],...]"}, 400)
                if not spans:
                    return _json(self, {"ok": False, "error": "spans is empty"}, 400)
                _json(self, mcp_server.cut_spans(spans=spans, dry_run=False))
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
