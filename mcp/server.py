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

# Single source of truth for the Python side. Bump on any tool/schema/
# behavior change; bridge_status reports it so the agent can confirm it
# is talking to the latest checkout. (The injected dylib carries its own
# FCB_VERSION in bridge/FCPBridge.m — the two versions are independent
# and reported side-by-side.)
__version__ = "0.2.0"
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
        record(tool, args, rpcs, {"ok": True, **result}, int((time.monotonic() - t0) * 1000),
               mcp_version=__version__)
        return result
    except Exception as e:
        record(tool, args, rpcs, {"ok": False, "error": str(e)}, int((time.monotonic() - t0) * 1000),
               mcp_version=__version__)
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


def _all_existing_clips(rpc) -> list:
    """Every primary clip whose source file is on disk (Bug 3 fix).

    The old `_existing_clip` returned only the first clip, so silence
    scans and word resolution went blind past the first edit point.
    """
    state = _clips(rpc)
    cands = _primary_clips(state)
    if not cands:
        raise BridgeError("no clip with a source file on the timeline")
    live = [c for c in cands if c.get("media_path") and os.path.exists(c["media_path"])]
    if not live:
        raise BridgeError(f"{len(cands)} candidate file(s) not on disk (media moved?)"
                          f" — first: {cands[0].get('media_path', '?')[:120]}")
    return sorted(live, key=lambda c: c.get("timeline_start_s", 0))


def _clip_file_window(clip: dict) -> tuple:
    """File interval [f0, f1] currently carried by this clip."""
    f0 = float(clip.get("trim_start_s", 0))
    return (f0, f0 + float(clip.get("duration_s", 0)))


def _merge_spans(spans: list, gap: float = 0.02) -> list:
    """Sort + merge overlapping/adjacent spans (timeline seconds)."""
    if not spans:
        return []
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def _resolve_file_interval(clips: list, media_path: str, fa: float, fb: float) -> list:
    """Map a FILE interval to current TIMELINE fragments.

    Source of truth for chained edits (Bug 1 fix): file times never move,
    so after each cut we re-resolve against the live clip layout instead
    of trusting timeline times stamped at transcribe time. Returns [] when
    the whole interval was already cut away.
    """
    frags = []
    for c in clips:
        if c.get("media_path") != media_path:
            continue
        f0, f1 = _clip_file_window(c)
        lo, hi = max(fa, f0), min(fb, f1)
        if hi > lo:
            base = float(c["timeline_start_s"])
            frags.append((base + (lo - f0), base + (hi - f0)))
    return _merge_spans(frags)


def _words_with_live_times(rpc, t: dict) -> tuple:
    """Attach live t_start/t_end to each cached word via file-time resolve.

    Returns (words, missing) where missing counts words whose file
    interval is no longer on the timeline (already cut). Legacy caches
    without f_start/f_end raise a clear upgrade error.
    """
    words = t.get("words", [])
    if words and "f_start" not in words[0]:
        raise BridgeError("transcript predates file-relative times — re-run transcribe once to enable chained edits")
    media = t.get("media_path")
    if media is None:
        raise BridgeError("transcript has no media_path — re-run transcribe")
    clips = _all_existing_clips(rpc)
    live, missing = [], 0
    for w in words:
        frags = _resolve_file_interval(clips, media, float(w["f_start"]), float(w["f_end"]))
        w2 = dict(w)
        if not frags:
            missing += 1
            w2["t_start"], w2["t_end"], w2["removed"] = -1, -1, True
        else:
            # A word split across an edit point (rare) maps to fragments;
            # report the outer span — the gap between fragments is already gone.
            w2["t_start"], w2["t_end"] = frags[0][0], frags[-1][1]
            w2.pop("removed", None)
        live.append(w2)
    return live, missing


def _run(cmd: list, timeout: int) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(p.stderr or p.stdout)[-2000:]}")
    return p.stdout


BLADE_EPS = 1 / 30  # within a frame of an edit point => no blade needed


def _timeline_edit_points(rpc=None, clips: list | None = None) -> list:
    """Sorted unique edit boundaries (timeline seconds)."""
    if clips is None:
        clips = _clips(rpc).get("clips", [])
    pts = set()
    for c in clips:
        try:
            pts.add(round(float(c["timeline_start_s"]), 3))
            pts.add(round(float(c["timeline_start_s"]) + float(c["duration_s"]), 3))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(pts)


def _near_point(x: float, pts: list) -> bool:
    return any(abs(x - p) < BLADE_EPS for p in pts)


def _isolate_span(rpc, a: float, b: float) -> tuple:
    """Blade both ends of [a, b] (skipping blades at existing edit points),
    re-snapshot, select the fresh segment BY ID.

    Returns (segment_id, blades_issued). Any miss reverts the blades issued
    and raises — a wrong cut is impossible by construction.
    Identity, not playhead luck.
    """
    mid = (a + b) / 2
    pre = _clips(rpc)
    pts = _timeline_edit_points(clips=pre.get("clips", []))
    blades = 0

    def blade_at(t: float):
        nonlocal blades
        if _near_point(t, pts):
            return
        rpc("playback.seek", {"t_s": t})
        rpc("timeline.action", {"action": "blade"})
        blades += 1

    blade_at(a)
    blade_at(b)
    fresh = _clips(rpc)

    def revert():
        for _ in range(blades):
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
    return seg["id"], blades


def _cut_spans(rpc, spans: list, label: str) -> dict:
    """Delete each [a, b] (timeline seconds), right-to-left so offsets hold.

    Per span: blade at both ends (skipped at existing edit points),
    re-snapshot, select the fresh segment BY ID (identity, not playhead
    luck), delete. A select miss reverts the blades — a wrong cut is
    impossible by construction.
    Returns report with the TRUE undo depth (blades actually issued +
    deletes), so `undo(steps=report["undo_steps"])` restores one call.
    """
    if not spans:
        return {"removed": 0, "removed_s": 0.0, "undo_steps": 0}
    before = _clips(rpc)
    dur_before = before.get("duration_s", 0)
    removed_s = 0.0
    steps = 0
    undos = 0
    for a, b in sorted(spans, reverse=True):
        if b <= a:
            continue
        _, blades = _isolate_span(rpc, a, b)
        rpc("timeline.action", {"action": "delete"})
        removed_s += b - a
        steps += 1
        undos += blades + 1
    after = _clips(rpc)
    dur_after = after.get("duration_s", 0)
    expected = dur_before - removed_s
    ok = abs(dur_after - expected) < 0.15  # within ~4 frames @24fps
    return {
        "removed": steps,
        "removed_s": round(removed_s, 3),
        "duration_before_s": round(dur_before, 3),
        "duration_after_s": round(dur_after, 3),
        "undo_steps": undos,
        "verify": "ok" if ok else f"MISMATCH expected={expected:.3f}s actual={dur_after:.3f}s — undo(steps={undos}) to revert",
    }


# ---------------------------------------------------------------- reads


@mcp.tool()
def bridge_status() -> dict:
    """Bridge reachability + versions. Call first.

    Reports mcp (this server), git sha/dirty, and bridge fcp/bridge
    versions side-by-side — the agent's proof it is on the latest code.
    """
    def run(rpc):
        info: dict = {"mcp": __version__}
        try:
            sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=5,
                                 cwd=REPO_DIR)
            if sha.returncode == 0:
                info["git"] = sha.stdout.strip()
                dirty = subprocess.run(["git", "status", "--porcelain"],
                                       capture_output=True, text=True, timeout=5,
                                       cwd=REPO_DIR)
                if dirty.returncode == 0 and dirty.stdout.strip():
                    info["git"] += "-dirty"
        except (OSError, subprocess.SubprocessError):
            pass
        try:
            info.update(rpc("system.version"))
        except BridgeError as e:
            info["bridge_error"] = str(e)
        return info

    return logged("bridge_status", {}, run)


@mcp.tool()
def get_timeline() -> dict:
    """Timeline clips: name, lane, timeline span, trim offset, source file path."""
    return logged("get_timeline", {}, _clips)


@mcp.tool()
def get_playhead() -> dict:
    """Playhead time, fps, sequence duration."""
    return logged("get_playhead", {}, lambda rpc: rpc("playback.position"))


# ---------------------------------------------------------------- silences

SILENCE_FRAME = 1 / 30  # sub-frame slivers are uncuttable; drop them


def _silence_scan(rpc, threshold_db: float, min_duration_s: float, for_cut: bool, pad_s: float) -> tuple:
    """Run the detector once per unique source file, map hits into every
    on-disk primary clip carrying that file (Bug 3 fix).

    Python owns padding (the detector is always invoked with --padding 0,
    so pad_s is applied exactly once here): detect expands raw silence by
    pad and clamps to the clip; remove insets by pad so speech breathes.
    Returns (spans, per_file) with spans merged timeline seconds.
    """
    clips = _all_existing_clips(rpc)
    by_file: dict = {}
    for c in clips:
        by_file.setdefault(c["media_path"], []).append(c)
    spans, per_file = [], []
    for path, fclips in by_file.items():
        cmd = SILENCE_BIN + [
            path,
            "--threshold", str(threshold_db),
            "--min-duration", str(min_duration_s),
            "--padding", "0",
        ]
        res = json.loads(_run(cmd, timeout=300))
        mapped = 0
        for r in res.get("silentRanges", []):
            fs, fe = float(r["start"]), float(r["start"]) + float(r["duration"])
            for c in fclips:
                f0, f1 = _clip_file_window(c)
                lo, hi = max(fs, f0), min(fe, f1)
                if hi <= lo:
                    continue
                base = float(c["timeline_start_s"])
                if for_cut:
                    a = base + (lo - f0) + pad_s
                    b = base + (hi - f0) - pad_s
                    a = max(a, base)
                    b = min(b, base + float(c["duration_s"]))
                    if b - a >= SILENCE_FRAME:
                        spans.append((round(a, 3), round(b, 3)))
                        mapped += 1
                else:
                    a = max(base + (lo - f0) - pad_s, base)
                    b = min(base + (hi - f0) + pad_s, base + float(c["duration_s"]))
                    if b > a:
                        spans.append((round(a, 3), round(b, 3)))
                        mapped += 1
        per_file.append({"file": path, "clips": len(fclips), "hits": mapped})
    return _merge_spans(spans), per_file


def _silence_warning(spans: list, rpc=None, duration_s: float | None = None) -> dict:
    total = round(sum(b - a for a, b in spans), 3)
    if duration_s is None and rpc is not None:
        try:
            duration_s = _clips(rpc).get("duration_s", 0)
        except BridgeError:
            duration_s = 0
    frac = round(total / duration_s, 3) if duration_s else 0
    warn = None
    if frac >= 0.2 or total >= 120:
        warn = (f"aggressive: would remove {total}s ({frac * 100:.0f}% of timeline) — "
                "consider min_duration_s=1.0 and a dry_run review first")
    return {"would_remove_s": total, "fraction": frac, "warning": warn}


@mcp.tool()
def detect_silences(
    threshold_db: float = -34.0, min_duration_s: float = 0.5, pad_s: float = 0.1
) -> dict:
    """Find silent spans via native AVFoundation analysis of the source files.

    Scans EVERY on-disk primary clip (grouped by file — one detector run
    per unique source), mapped to timeline seconds. threshold_db=-34 ≈
    'auto' sensitivity floor.
    """
    args = {"threshold_db": threshold_db, "min_duration_s": min_duration_s, "pad_s": pad_s}

    def run(rpc):
        spans, per_file = _silence_scan(rpc, threshold_db, min_duration_s, False, pad_s)
        state = _clips(rpc)
        warn = _silence_warning(spans, duration_s=state.get("duration_s", 0))
        return {"clips_scanned": sum(f["clips"] for f in per_file), "files": per_file,
                "silences": [{"start_s": a, "end_s": b} for a, b in spans],
                "count": len(spans), **warn}

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
        spans, per_file = _silence_scan(rpc, threshold_db, min_duration_s, True, pad_s)
        warn = _silence_warning(spans, rpc)
        if dry_run or not spans:
            return {"dry_run": dry_run, "files": per_file, "spans": spans, **warn}
        report = _cut_spans(rpc, spans, "remove_silences")
        report["files"] = per_file
        if warn["warning"]:
            report["warning"] = warn["warning"]
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


def _fresh_transcript(rpc=None) -> dict:
    """Cached transcript, keyed by source file+model (content-stable).

    File times never go stale across edits — each word op re-resolves them
    against the live timeline (see _words_with_live_times) — so there is no
    duration guard here. A rippled timeline just shifts live positions.
    """
    t = _last_transcript()
    if not t:
        raise BridgeError("no transcript cached — run transcribe first")
    return t


@mcp.tool()
def transcribe(engine: str = "parakeet", model: str = "v3") -> dict:
    """Transcribe the first primary clip via on-device Parakeet subprocess.

    Model downloads once (~475MB) to ~/Library/Application Support/FluidAudio.
    Result cached on disk keyed by file+model — repeat calls are instant.
    Words store FILE-relative times (stable across edits); timeline
    positions are re-resolved live on every read/cut, so chained word cuts
    never go stale and a cache hit needs no re-transcribe.
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
            # Cache hit: re-resolve live positions (timeline may have
            # rippled since the transcribe) and re-stamp the duration, so
            # the guard can never dead-end after a cut (Bug 1 fix).
            try:
                live, missing = _words_with_live_times(rpc, cached)
            except BridgeError as e:
                return {"cached": True, "clip": clip["name"],
                        "word_count": len(cached.get("words", [])),
                        "error": str(e)}
            cached["timeline_duration_s"] = _clips(rpc).get("duration_s", 0)
            _save_cache(key, cached)
            # Sentence preview rebuilt from live positions (removed words excluded).
            preview = _sentences([w for w in live if not w.get("removed")])
            return {"cached": True, "clip": clip["name"],
                    "word_count": len(live),
                    "sentence_count": len(preview), "sentences": preview[:8],
                    "removed_words": missing}
        words = json.loads(_run([PARAKEET_BIN, clip["media_path"], "--model", model], timeout=1800))
        # Store FILE times (immutable) + timeline times (preview, re-resolved live later).
        mapped = []
        for i, w in enumerate(words):
            fs, fe = float(w.get("startTime", 0)), float(w.get("endTime", 0))
            mapped.append({"i": i, "w": str(w.get("word", w.get("text", ""))),
                           "f_start": round(fs, 3), "f_end": round(fe, 3),
                           "t_start": round(_file_to_timeline(clip, fs), 3),
                           "t_end": round(_file_to_timeline(clip, fe), 3),
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
    Timeline positions are re-resolved live, so they track edits.
    """
    args = {"search": search, "limit": limit, "detail": detail}

    def run(rpc):
        t = _fresh_transcript(rpc)
        words, missing = _words_with_live_times(rpc, t)
        present = [w for w in words if not w.get("removed")]
        sentences = _sentences(present)
        if detail == "words":
            rows = present
            if search:
                s = search.lower()
                rows = [w for w in rows if s in w.get("w", "").lower()]
            rows = [{k: v for k, v in w.items() if v is not None}
                    for w in rows[: max(1, limit)]]
            return {"clip": t.get("clip"), "word_count": len(words),
                    "removed_words": missing, "words": rows}
        if search:
            s = search.lower()
            sentences = [x for x in sentences if s in x["text"].lower()]
        return {"clip": t.get("clip"), "word_count": len(words),
                "removed_words": missing,
                "sentence_count": len(sentences),
                "sentences": sentences[: max(1, limit)]}

    return logged("get_transcript", args, run)


def _word_ranges_to_spans(rpc, ranges: list) -> tuple:
    """Resolve [[start_index, count], ...] (stable FILE indices) to merged
    timeline spans via live clip layout. Returns (spans, texts, skipped)."""
    t = _fresh_transcript(rpc)
    cache_words = t.get("words", [])
    if cache_words and "f_start" not in cache_words[0]:
        raise BridgeError("transcript predates file-relative times — re-run transcribe once")
    media = t.get("media_path")
    clips = _all_existing_clips(rpc)
    spans, texts, skipped = [], [], []
    for start_index, count in ranges:
        sel = cache_words[start_index: start_index + count]
        if len(sel) < count:
            raise BridgeError(f"only {len(cache_words) - start_index} words from index {start_index}")
        fa, fb = float(sel[0]["f_start"]), float(sel[-1]["f_end"])
        frags = _resolve_file_interval(clips, media, fa, fb)
        if not frags:
            skipped.append({"start_index": start_index, "count": count,
                            "text": " ".join(w["w"] for w in sel),
                            "reason": "already removed"})
            continue
        for a, b in frags:
            spans.append((a, b))
        texts.append(" ".join(w["w"] for w in sel))
    return _merge_spans(spans), texts, skipped


@mcp.tool()
def delete_words(start_index: int = 0, count: int = 0, dry_run: bool = True,
                 ranges: list | None = None) -> dict:
    """Delete words [start_index, start_index+count) and ripple the video.

    Plan on sentences (get_transcript default): a sentence's start_word /
    end_word IS the range to pass here. For retake sweeps pass
    ranges=[[start, count], ...] — N file-stable ranges validated whole and
    cut right-to-left in one call (one re-resolve, one undo depth to revert).
    """
    args = {"start_index": start_index, "count": count, "dry_run": dry_run, "ranges": ranges}

    def run(rpc):
        batch = [tuple(r) for r in ranges] if ranges else [(start_index, count)]
        if not ranges and count <= 0:
            return {"ok": False, "error": "count must be > 0 (or pass ranges=[[start, count], ...])"}
        try:
            spans, texts, skipped = _word_ranges_to_spans(rpc, batch)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        if dry_run or not spans:
            out = {"dry_run": True, "spans": spans, "texts": texts, "skipped": skipped,
                   "would_remove_s": round(sum(b - a for a, b in spans), 3)}
            if not ranges and spans:
                # compat: single-range callers expect span/text
                out["span"] = spans[0]
                out["text"] = texts[0] if texts else ""
            return out
        report = _cut_spans(rpc, spans, "delete_words")
        report["texts"] = texts
        report["skipped"] = skipped
        return report

    return logged("delete_words", args, run)


@mcp.tool()
def move_words(start_index: int, count: int, dest_index: int, dry_run: bool = True) -> dict:
    """Reorder by words: select the span BY ID, cut, paste at the destination.

    Destination is anchor-aware: a dest after the span shifts left by the cut
    length (magnetic close). A dest inside the span is refused. Word indices
    are file-stable; the span is re-resolved live so moves chain after cuts.
    """
    args = {"start_index": start_index, "count": count,
            "dest_index": dest_index, "dry_run": dry_run}

    def run(rpc):
        t = _fresh_transcript(rpc)
        cache_words = t.get("words", [])
        sel = cache_words[start_index: start_index + count]
        if len(sel) < count or not (0 <= dest_index < len(cache_words)):
            return {"ok": False, "error": "word index out of range"}
        try:
            spans, _, skipped = _word_ranges_to_spans(rpc, [(start_index, count)])
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        if skipped or not spans:
            return {"ok": False, "error": "moved words were already removed — re-read get_transcript"}
        if len(spans) > 1:
            return {"ok": False, "error": "moved span is split across an edit point — delete + re-add instead"}
        span = spans[0]
        span_len = span[1] - span[0]
        live, _ = _words_with_live_times(rpc, t)
        dest_t = live[dest_index]["t_start"]
        if live[dest_index].get("removed"):
            return {"ok": False, "error": "destination word was already removed — re-read get_transcript"}
        if span[0] <= dest_t <= span[1]:
            return {"ok": False, "error": "destination inside the moved span"}
        if dry_run:
            adj = dest_t - span_len if dest_t > span[1] else dest_t
            return {"dry_run": True, "span": span, "dest_t": round(adj, 3),
                    "text": " ".join(w["w"] for w in sel)}
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
                "verify": "ok" if ok else f"MISMATCH duration {dur0:.3f}s -> {dur1:.3f}s — undo(steps=2) to revert"}

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
                "verify": "ok" if ok else f"MISMATCH duration {dur0:.3f}s -> {dur1:.3f}s — undo(steps=2) to revert"}

    return logged("move_clip", args, run)


# ---------------------------------------------------------------- safety


@mcp.tool()
def verify_action(note: str = "") -> dict:
    """Re-read timeline + playhead. Call after any batch."""
    return logged("verify_action", {"note": note}, lambda rpc: {
        "state": _clips(rpc), "position": rpc("playback.position")})


@mcp.tool()
def undo(steps: int = 1) -> dict:
    """Undo `steps` entries via FCP's undo manager.

    One delete_words/remove_silences span costs up to 3 entries (2 blades +
    1 delete, fewer at edit-point boundaries); each tool reports its exact
    `undo_steps` — pass it back here to revert the whole call. Blades leave
    split points behind, so a single undo restores content but NOT the
    un-split timeline (Bug 2): always undo the full reported depth.
    """
    args = {"steps": steps}

    def run(rpc):
        if steps < 1:
            return {"ok": False, "error": "steps must be >= 1"}
        names = []
        for _ in range(steps):
            r = rpc("timeline.undo")
            names.append(r.get("actionName", ""))
        return {"undone": steps, "actionNames": names}

    return logged("undo", args, run)


@mcp.tool()
def redo() -> dict:
    """Redo one step via FCP's undo manager."""
    return logged("redo", {}, lambda rpc: rpc("timeline.redo"))


if __name__ == "__main__":
    mcp.run()
