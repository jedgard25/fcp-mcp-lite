"""fcp-mcp-lite MCP server (stdio).

Sits between the agent and the in-process FCP bridge (TCP JSON-RPC on
127.0.0.1:9876). Dumb bridge (7 verbs) + smart Python: all cut planning,
validation, subprocess inference (silence-detector, parakeet) and disk caches
live here, testable without FCP.

Rules (see README):
  1. destructive tools validate whole BEFORE any write, refuse on bad input
  2. every destructive tool takes dry_run (default True)
  3. every destructive tool verifies by re-reading timeline state
  4. every call is JSONL-logged via log.py
  5. reads never spawn engines (transcript/silence served from disk when cached)

Time bases (keep them straight):
  - FILE seconds: offsets inside the source media file (what parakeet /
    silence-detector report)
  - TIMELINE seconds: composition clock (what the bridge cuts on)
  - file_s -> timeline_s: clip.timeline_start_s + (file_s - clip.trim_start_s)
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import time

from mcp.server.fastmcp import FastMCP

from log import LOG_DIR, record

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 9876
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SILENCE_BIN = ["swift", os.path.join(REPO_DIR, "tools", "silence-detector.swift")]
PARAKEET_BIN = os.path.join(
    REPO_DIR, "tools", "parakeet-transcriber", ".build", "release", "parakeet-transcriber"
)
CACHE_DIR = os.path.join(LOG_DIR, "transcripts")
STATE_PATH = os.path.join(LOG_DIR, "last.json")

mcp = FastMCP("fcp-mcp-lite")


# ---------------------------------------------------------------- plumbing


class BridgeError(Exception):
    pass


def bridge_call(method: str, params: dict | None = None) -> dict:
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1}
    ).encode()
    try:
        with socket.create_connection((BRIDGE_HOST, BRIDGE_PORT), timeout=15) as s:
            s.sendall(payload + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 16 * 1024 * 1024:
                    raise BridgeError("bridge response too large")
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise BridgeError(f"bridge unreachable on {BRIDGE_HOST}:{BRIDGE_PORT} ({e}); launch patched FCP")
    try:
        resp = json.loads(buf.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise BridgeError(f"bad bridge response: {e}")
    if "error" in resp:
        raise BridgeError(resp["error"].get("message", str(resp["error"])))
    return resp.get("result", {})


def logged(tool: str, args: dict, fn):
    t0 = time.monotonic()
    rpcs: list = []

    def rpc(method: str, params: dict | None = None) -> dict:
        rpcs.append({"method": method, "params": params or {}})
        return bridge_call(method, params)

    try:
        result = fn(rpc)
        record(tool, args, rpcs, {"ok": True, **result}, int((time.monotonic() - t0) * 1000))
        return result
    except Exception as e:
        record(tool, args, rpcs, {"ok": False, "error": str(e)}, int((time.monotonic() - t0) * 1000))
        return {"ok": False, "error": str(e)}


def _clips(rpc) -> dict:
    return rpc("timeline.clips")


def _primary_clips(state: dict) -> list:
    clips = state.get("clips", [])
    prim = [c for c in clips if c.get("lane") == "primary" and "media_path" in c]
    if prim:
        return prim
    # compound / connected-storyline timelines: nest everything one level
    return [c for c in clips if "media_path" in c]


def _existing_clip(rpc) -> dict:
    """First candidate clip whose source file is actually on disk.

    FCP libraries routinely reference moved/renamed media; stale aliases
    are skipped rather than failing the whole tool.
    """
    state = _clips(rpc)
    cands = _primary_clips(state)
    if not cands:
        raise BridgeError("no clip with a source file on the timeline")
    for c in cands:
        if os.path.exists(c["media_path"]):
            return c
    raise BridgeError(f"{len(cands)} candidate file(s) not on disk (media moved?)"
                      f" — first: {cands[0]['media_path'][:120]}")


def _resolve_id(rpc, clip_id: str) -> dict:
    """Fresh snapshot lookup by stable clip ID.

    Raises BridgeError("stale id ...") when the clip left the timeline —
    the agent's cue to re-read get_timeline, never to guess times.
    """
    state = _clips(rpc)
    for c in state.get("clips", []):
        if c.get("id") == clip_id:
            return c
    raise BridgeError(f"stale id {clip_id} — re-read get_timeline for fresh IDs")


def _file_to_timeline(clip: dict, file_s: float) -> float:
    return clip["timeline_start_s"] + (file_s - clip.get("trim_start_s", 0))


def _run(cmd: list, timeout: int) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(p.stderr or p.stdout)[-2000:]}")
    return p.stdout


def _isolate_span(rpc, a: float, b: float) -> str:
    """Blade both ends of [a, b], re-snapshot, select the fresh segment BY ID.

    Returns the segment's clip ID, selected and ready for delete/cut.
    Any miss reverts the blades and raises — a wrong cut is impossible
    by construction. Identity, not playhead luck.
    """
    mid = (a + b) / 2
    n0 = len(_clips(rpc).get("clips", []))
    rpc("playback.seek", {"t_s": a})
    rpc("timeline.action", {"action": "blade"})
    rpc("playback.seek", {"t_s": b})
    rpc("timeline.action", {"action": "blade"})
    fresh = _clips(rpc)
    n1 = len(fresh.get("clips", []))

    def revert():
        for _ in range(max(0, n1 - n0)):
            rpc("timeline.undo")

    cands = [c for c in fresh.get("clips", [])
             if c.get("id") and c["timeline_start_s"] <= mid
             and c["timeline_start_s"] + c["duration_s"] >= mid]
    if not cands:
        revert()
        raise BridgeError(f"no clip found at {mid:.3f}s after blading [{a}, {b}] — reverted.")
    seg = min(cands, key=lambda c: c["duration_s"])
    try:
        rpc("timeline.select", {"id": seg["id"]})
    except BridgeError as e:
        revert()
        raise BridgeError(f"select failed for [{a}, {b}]: {e} — reverted.")
    return seg["id"]


def _cut_spans(rpc, spans: list, label: str) -> dict:
    """Delete each [a, b] (timeline seconds), right-to-left so offsets hold.

    Per span: blade at both ends, re-snapshot, select the fresh segment BY ID
    (identity, not playhead luck), delete. A select miss reverts the blades —
    a wrong cut is impossible by construction.
    Returns report with undo depth (v0: one undo entry per span).
    """
    if not spans:
        return {"removed": 0, "removed_s": 0.0, "undo_steps": 0}
    before = _clips(rpc)
    dur_before = before.get("duration_s", 0)
    n0 = len(before.get("clips", []))
    removed_s = 0.0
    steps = 0
    for a, b in sorted(spans, reverse=True):
        if b <= a:
            continue
        _isolate_span(rpc, a, b)
        rpc("timeline.action", {"action": "delete"})
        removed_s += b - a
        steps += 1
    after = _clips(rpc)
    dur_after = after.get("duration_s", 0)
    expected = dur_before - removed_s
    ok = abs(dur_after - expected) < 0.15  # within ~4 frames @24fps
    return {
        "removed": steps,
        "removed_s": round(removed_s, 3),
        "duration_before_s": round(dur_before, 3),
        "duration_after_s": round(dur_after, 3),
        "undo_steps": steps,
        "verify": "ok" if ok else f"MISMATCH expected={expected:.3f}s actual={dur_after:.3f}s — undo {steps}x to revert",
    }


# ---------------------------------------------------------------- reads


@mcp.tool()
def bridge_status() -> dict:
    """Bridge reachability + versions. Call first."""
    return logged("bridge_status", {}, lambda rpc: rpc("system.version"))


@mcp.tool()
def get_timeline() -> dict:
    """Timeline clips: name, lane, timeline span, trim offset, source file path."""
    return logged("get_timeline", {}, _clips)


@mcp.tool()
def get_playhead() -> dict:
    """Playhead time, fps, sequence duration."""
    return logged("get_playhead", {}, lambda rpc: rpc("playback.position"))


# ---------------------------------------------------------------- silences


@mcp.tool()
def detect_silences(
    threshold_db: float = -34.0, min_duration_s: float = 0.5, pad_s: float = 0.1
) -> dict:
    """Find silent spans via native AVFoundation analysis of the source file.

    Returns silences in BOTH file and timeline seconds for the first primary
    clip carrying media. threshold_db=-34 ≈ 'auto' sensitivity floor.
    """
    args = {"threshold_db": threshold_db, "min_duration_s": min_duration_s, "pad_s": pad_s}

    def run(rpc):
        clip = _existing_clip(rpc)
        cmd = SILENCE_BIN + [
            clip["media_path"],
            "--threshold", str(threshold_db),
            "--min-duration", str(min_duration_s),
            "--padding", str(pad_s),
        ]
        res = json.loads(_run(cmd, timeout=300))
        out = []
        for r in res.get("silentRanges", []):
            tl = _file_to_timeline(clip, r["start"]) - pad_s
            tr = _file_to_timeline(clip, r["start"] + r["duration"]) + pad_s
            tl = max(tl, clip["timeline_start_s"])
            tr = min(tr, clip["timeline_start_s"] + clip["duration_s"])
            if tr > tl:
                out.append({"start_s": round(tl, 3), "end_s": round(tr, 3)})
        return {"clip": clip["name"], "silences": out, "count": len(out)}

    return logged("detect_silences", args, run)


@mcp.tool()
def remove_silences(
    threshold_db: float = -34.0,
    min_duration_s: float = 0.5,
    pad_s: float = 0.1,
    dry_run: bool = True,
) -> dict:
    """Cut every silence (pad kept each side so speech breathes).

    dry_run=True (default) returns the cut list without writing.
    """
    args = {"threshold_db": threshold_db, "min_duration_s": min_duration_s,
            "pad_s": pad_s, "dry_run": dry_run}

    def run(rpc):
        clip = _existing_clip(rpc)
        cmd = SILENCE_BIN + [
            clip["media_path"],
            "--threshold", str(threshold_db),
            "--min-duration", str(min_duration_s),
            "--padding", str(pad_s),
        ]
        res = json.loads(_run(cmd, timeout=300))
        spans = []
        for r in res.get("silentRanges", []):
            core_a = _file_to_timeline(clip, r["start"]) + pad_s
            core_b = _file_to_timeline(clip, r["start"] + r["duration"]) - pad_s
            core_a = max(core_a, clip["timeline_start_s"])
            core_b = min(core_b, clip["timeline_start_s"] + clip["duration_s"])
            if core_b - core_a >= 1 / 30:  # at least a frame
                spans.append((round(core_a, 3), round(core_b, 3)))
        if dry_run or not spans:
            return {"dry_run": dry_run, "clip": clip["name"], "spans": spans,
                    "would_remove_s": round(sum(b - a for a, b in spans), 3)}
        report = _cut_spans(rpc, spans, "remove_silences")
        report["clip"] = clip["name"]
        return report

    return logged("remove_silences", args, run)


@mcp.tool()
def apply_cut_list(keep_ranges: list, dry_run: bool = True) -> dict:
    """Rough cut in one verb: keep [[start_s, end_s]...], drop the rest, close gaps.

    Ranges validated whole (sorted, non-overlapping, inside the timeline) before
    any write. Cuts run right-to-left so offsets hold; duration verified after.
    """
    args = {"keep_ranges": keep_ranges, "dry_run": dry_run}

    def run(rpc):
        state = _clips(rpc)
        total = state.get("duration_s", 0)
        try:
            ranges = sorted((float(a), float(b)) for a, b in keep_ranges)
        except (TypeError, ValueError):
            return {"ok": False, "error": "keep_ranges must be [[start_s, end_s], ...]"}
        for a, b in ranges:
            if not (0 <= a < b <= total + 0.05):
                return {"ok": False, "error": f"range [{a}, {b}] outside timeline (0, {total:.3f})"}
        for (a1, b1), (a2, b2) in zip(ranges, ranges[1:]):
            if a2 < b1:
                return {"ok": False, "error": f"overlapping ranges [{a1}, {b1}] and [{a2}, {b2}]"}
        # complement = spans to drop
        drops, cursor = [], 0.0
        for a, b in ranges:
            if a > cursor:
                drops.append((round(cursor, 3), round(a, 3)))
            cursor = max(cursor, b)
        if cursor < total:
            drops.append((round(cursor, 3), round(total, 3)))
        if dry_run or not drops:
            return {"dry_run": dry_run, "keep": ranges, "drop": drops,
                    "would_remove_s": round(sum(b - a for a, b in drops), 3)}
        report = _cut_spans(rpc, drops, "apply_cut_list")
        report["kept"] = len(ranges)
        return report

    return logged("apply_cut_list", args, run)


# ---------------------------------------------------------------- transcript


def _cache_key(media_path: str, engine: str, model: str) -> str:
    st = os.stat(media_path)
    h = hashlib.sha1(f"{media_path}|{st.st_mtime_ns}|{st.st_size}|{engine}|{model}".encode())
    return h.hexdigest()[:16]


def _sentences(words: list, max_words: int = 40) -> list:
    """Collapse word rows into sentence chunks for cheap agent reads.

    Each sentence carries its word-index range, so the agent plans on
    sentences but still cuts on stable word indices (no new verbs needed).
    ~7x fewer tokens than per-word JSON.
    """
    out, cur = [], []

    def flush():
        if cur:
            out.append({
                "s": len(out),
                "start_word": cur[0]["i"],
                "end_word": cur[-1]["i"],
                "t_start": cur[0]["t_start"],
                "t_end": cur[-1]["t_end"],
                "text": " ".join(w["w"] for w in cur),
            })
            cur.clear()

    for w in words:
        cur.append(w)
        if re.search(r"[.?!…]['\"]?$", w.get("w", "")) or len(cur) >= max_words:
            flush()
    flush()
    return out


def _load_cache(key: str) -> dict | None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def _save_cache(key: str, data: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, key + ".json"), "w") as f:
        json.dump(data, f)
    with open(STATE_PATH, "w") as f:
        json.dump({"last_key": key}, f)


def _last_transcript() -> dict | None:
    try:
        with open(STATE_PATH) as f:
            key = json.load(f).get("last_key")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None
    return _load_cache(key) if key else None


def _fresh_transcript(rpc) -> dict:
    """Cached transcript, valid only if the timeline hasn't rippled since.

    Word times are resolved at transcribe time; any edit shifts them, so a
    stale cache is refused (re-transcribe) rather than used for cuts.
    """
    t = _last_transcript()
    if not t:
        raise BridgeError("no transcript cached — run transcribe first")
    now = _clips(rpc).get("duration_s", 0)
    then = t.get("timeline_duration_s", -1)
    if abs(now - then) > 0.05:
        raise BridgeError(
            f"timeline changed since transcribe ({then:.3f}s -> {now:.3f}s) — "
            f"word times are stale; re-run transcribe before cutting words")
    return t


@mcp.tool()
def transcribe(engine: str = "parakeet", model: str = "v3") -> dict:
    """Transcribe the first primary clip via on-device Parakeet subprocess.

    Model downloads once (~475MB) to ~/Library/Application Support/FluidAudio.
    Result cached on disk keyed by file+model — repeat calls are instant.
    """
    args = {"engine": engine, "model": model}

    def run(rpc):
        if engine != "parakeet":
            return {"ok": False, "error": f"unknown engine '{engine}' (only 'parakeet' in v0)"}
        if not os.path.exists(PARAKEET_BIN):
            return {"ok": False, "error": f"parakeet binary missing; build with: swift build -c release --product parakeet-transcriber (in tools/parakeet-transcriber)"}
        clip = _existing_clip(rpc)
        key = _cache_key(clip["media_path"], engine, model)
        cached = _load_cache(key)
        if cached:
            sents = cached.get("sentences") or _sentences(cached.get("words", []))
            return {"cached": True, "clip": clip["name"],
                    "word_count": len(cached.get("words", [])),
                    "sentence_count": len(sents), "sentences": sents[:8]}
        words = json.loads(_run([PARAKEET_BIN, clip["media_path"], "--model", model], timeout=1800))
        # file seconds -> timeline seconds
        mapped = []
        for i, w in enumerate(words):
            ts = _file_to_timeline(clip, float(w.get("startTime", 0)))
            te = _file_to_timeline(clip, float(w.get("endTime", 0)))
            mapped.append({"i": i, "w": str(w.get("word", w.get("text", ""))),
                           "t_start": round(ts, 3), "t_end": round(te, 3),
                           "confidence": w.get("confidence"),
                           "speaker": w.get("speaker")})
        data = {"clip": clip["name"], "media_path": clip["media_path"], "words": mapped,
                "sentences": _sentences(mapped),
                "timeline_duration_s": _clips(rpc).get("duration_s", 0),
                "speakers": sorted({w["speaker"] for w in mapped if w.get("speaker")})}
        _save_cache(key, data)
        return {"cached": False, "clip": clip["name"], "word_count": len(mapped),
                "sentence_count": len(data["sentences"]),
                "sentences": data["sentences"][:8],
                "note": "first 8 sentences shown; use get_transcript for full (sentences by default)"}

    return logged("transcribe", args, run)


@mcp.tool()
def get_transcript(search: str | None = None, limit: int = 200,
                   detail: str = "sentences") -> dict:
    """Read the cached transcript (never spawns an engine).

    detail='sentences' (default): compact sentence chunks with word-index
    ranges — plan here, then cut with delete_words/move_words using the
    start_word/end_word range. detail='words': full per-word rows.
    """
    args = {"search": search, "limit": limit, "detail": detail}

    def run(rpc):
        t = _fresh_transcript(rpc)
        words = t.get("words", [])
        sentences = t.get("sentences") or _sentences(words)
        if detail == "words":
            if search:
                s = search.lower()
                words = [w for w in words if s in w.get("w", "").lower()]
            rows = [{k: v for k, v in w.items() if v is not None}
                    for w in words[: max(1, limit)]]
            return {"clip": t.get("clip"), "word_count": len(words), "words": rows}
        if search:
            s = search.lower()
            sentences = [x for x in sentences if s in x["text"].lower()]
        return {"clip": t.get("clip"), "word_count": len(words),
                "sentence_count": len(sentences),
                "sentences": sentences[: max(1, limit)]}

    return logged("get_transcript", args, run)


@mcp.tool()
def delete_words(start_index: int, count: int, dry_run: bool = True) -> dict:
    """Delete words [start_index, start_index+count) and ripple the video.

    Plan on sentences (get_transcript default): a sentence's start_word /
    end_word IS the range to pass here. Word timestamps -> one timeline
    span -> blade/blade/select-by-ID/delete -> verify.
    """
    args = {"start_index": start_index, "count": count, "dry_run": dry_run}

    def run(rpc):
        t = _fresh_transcript(rpc)
        words = t.get("words", [])
        sel = words[start_index: start_index + count]
        if len(sel) < count:
            return {"ok": False, "error": f"only {len(words) - start_index} words from index {start_index}"}
        span = (sel[0]["t_start"], sel[-1]["t_end"])
        if dry_run:
            return {"dry_run": True, "span": span,
                    "text": " ".join(w["w"] for w in sel)}
        return _cut_spans(rpc, [span], "delete_words")

    return logged("delete_words", args, run)


@mcp.tool()
def move_words(start_index: int, count: int, dest_index: int, dry_run: bool = True) -> dict:
    """Reorder by words: select the span BY ID, cut, paste at the destination.

    Destination is anchor-aware: a dest after the span shifts left by the cut
    length (magnetic close). A dest inside the span is refused.
    """
    args = {"start_index": start_index, "count": count,
            "dest_index": dest_index, "dry_run": dry_run}

    def run(rpc):
        t = _fresh_transcript(rpc)
        words = t.get("words", [])
        sel = words[start_index: start_index + count]
        if len(sel) < count or not (0 <= dest_index < len(words)):
            return {"ok": False, "error": "word index out of range"}
        span = (sel[0]["t_start"], sel[-1]["t_end"])
        span_len = span[1] - span[0]
        dest_t = words[dest_index]["t_start"]
        if span[0] <= dest_t <= span[1]:
            return {"ok": False, "error": "destination inside the moved span"}
        if dry_run:
            adj = dest_t - span_len if dest_t > span[1] else dest_t
            return {"dry_run": True, "span": span, "dest_t": round(adj, 3),
                    "text": " ".join(w["w"] for w in sel)}
        mid = (span[0] + span[1]) / 2
        fresh = _clips(rpc)
        n0 = len(fresh.get("clips", []))
        dur0 = fresh.get("duration_s", 0)
        _isolate_span(rpc, span[0], span[1])
        rpc("timeline.action", {"action": "cut"})
        # Re-resolve: the cut closed the gap, so a later dest moved left.
        dest_adj = dest_t - span_len if dest_t > span[1] else dest_t
        rpc("playback.seek", {"t_s": max(0, dest_adj)})
        rpc("timeline.action", {"action": "paste"})
        after = _clips(rpc)
        dur1 = after.get("duration_s", 0)
        ok = abs(dur1 - dur0) < 0.15  # move preserves duration
        return {"moved": True, "span": span, "dest_t": round(dest_adj, 3),
                "text": " ".join(w["w"] for w in sel),
                "clips_before": n0, "clips_after": len(after.get("clips", [])),
                "verify": "ok" if ok else f"MISMATCH duration {dur0:.3f}s -> {dur1:.3f}s — undo 2x to revert"}

    return logged("move_words", args, run)


# ---------------------------------------------------------------- identity ops


@mcp.tool()
def select_clip(clip_id: str) -> dict:
    """Select a clip BY ID (see get_timeline). Membership + identity confirmed.

    Refuses stale IDs and wrong-landing selections — use before any targeted
    edit, or let cut/move tools do it internally.
    """
    return logged("select_clip", {"clip_id": clip_id},
                  lambda rpc: rpc("timeline.select", {"id": clip_id}))


@mcp.tool()
def move_clip(clip_id: str, after_id: str | None = None,
              before_id: str | None = None, dry_run: bool = True) -> dict:
    """Reorder by reference: move clip_id after/before an anchor clip.

    Anchors (not timestamps) survive the magnet: the anchor is re-resolved
    after the cut closes the gap. Exactly one of after_id / before_id.
    Duration must be preserved — verified after.
    """
    args = {"clip_id": clip_id, "after_id": after_id,
            "before_id": before_id, "dry_run": dry_run}

    def run(rpc):
        if (after_id is None) == (before_id is None):
            return {"ok": False, "error": "exactly one of after_id / before_id"}
        src = _resolve_id(rpc, clip_id)
        anchor_id = after_id or before_id
        anchor = _resolve_id(rpc, anchor_id)
        if anchor["id"] == src["id"]:
            return {"ok": False, "error": "cannot move a clip relative to itself"}
        anchor_t = (anchor["timeline_start_s"] + anchor["duration_s"]
                    if after_id else anchor["timeline_start_s"])
        if dry_run:
            return {"dry_run": True, "clip": src["name"], "anchor": anchor["name"],
                    "anchor_t": round(anchor_t, 3)}
        snap = _clips(rpc)
        n0 = len(snap.get("clips", []))
        dur0 = snap.get("duration_s", 0)
        rpc("timeline.select", {"id": src["id"]})
        rpc("timeline.action", {"action": "cut"})
        # Re-resolve the anchor — it shifted if it sat after the source.
        anchor2 = _resolve_id(rpc, anchor_id)
        anchor_t2 = (anchor2["timeline_start_s"] + anchor2["duration_s"]
                     if after_id else anchor2["timeline_start_s"])
        rpc("playback.seek", {"t_s": max(0, anchor_t2)})
        rpc("timeline.action", {"action": "paste"})
        after = _clips(rpc)
        dur1 = after.get("duration_s", 0)
        ok = abs(dur1 - dur0) < 0.15
        return {"moved": True, "clip": src["name"], "anchor": anchor["name"],
                "anchor_t": round(anchor_t2, 3),
                "clips_before": n0, "clips_after": len(after.get("clips", [])),
                "verify": "ok" if ok else f"MISMATCH duration {dur0:.3f}s -> {dur1:.3f}s — undo 2x to revert"}

    return logged("move_clip", args, run)


# ---------------------------------------------------------------- safety


@mcp.tool()
def verify_action(note: str = "") -> dict:
    """Re-read timeline + playhead. Call after any batch."""
    return logged("verify_action", {"note": note}, lambda rpc: {
        "state": _clips(rpc), "position": rpc("playback.position")})


@mcp.tool()
def undo() -> dict:
    """Undo one step via FCP's undo manager."""
    return logged("undo", {}, lambda rpc: rpc("timeline.undo"))


@mcp.tool()
def redo() -> dict:
    """Redo one step via FCP's undo manager."""
    return logged("redo", {}, lambda rpc: rpc("timeline.redo"))


if __name__ == "__main__":
    mcp.run()
