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

import difflib
import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time

from mcp.server.fastmcp import FastMCP

from log import LOG_DIR, LOG_PATH, record

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 9876
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Single source of truth for the Python side. Bump on any tool/schema/
# behavior change; bridge_status reports it so the agent can confirm it
# is talking to the latest checkout. (The injected dylib carries its own
# FCB_VERSION in bridge/FCPBridge.m — the two versions are independent
# and reported side-by-side.)
__version__ = "0.5.5"
DEFAULT_CUT_PIECES = 40
# Transcript-cache schema. Bumped on any words/cache layout change; the key
# AND the payload both carry it, so an upgrade never dead-ends on a stale
# cache hit ("safe to reuse but not when our schema changes").
# v4 adds additive verbatim enrichment (filler/repeat word flags, see
# VERBATIM_FILLERS): v3 caches migrate in place via _ensure_word_tags
# instead of forcing a re-transcribe.
SCHEMA_VERSION = 4
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
    t = _effective_trim(clip)
    if t is None:
        t = float(clip.get("trim_start_s", 0) or 0)
    return clip["timeline_start_s"] + (file_s - t)


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
    f0 = _effective_trim(clip)
    if f0 is None:  # candidates disagree — reads go best-effort on the raw value
        f0 = float(clip.get("trim_start_s", 0) or 0)
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


def _effective_trim(clip: dict) -> float | None:
    """Best-known source-file offset for a clip.

    Prefers the bridge's `trim_candidates` consensus (see bridge 0.2.0):
    nonzero candidates must agree (within a frame); exact-0 votes are
    abstentions, not votes, because `unclippedRange` is known to fail to 0.
    Two different NONZERO candidates → None (unknown — the caller must
    block, never guess). Unanimous zero → 0.0 (the degenerate signal is
    handled by _trim_health). Without candidates (old bridge) falls back
    to `trim_start_s`.
    """
    cands = clip.get("trim_candidates")
    if cands:
        try:
            vals = [float(v) for v in cands.values()]
        except (TypeError, ValueError):
            return None
        nz = [v for v in vals if v > 0]
        if nz:
            if max(nz) - min(nz) > 0.05:
                return None
            return max(nz)
        return 0.0
    return float(clip.get("trim_start_s", 0) or 0)


def _trim_health(clips: list) -> list:
    """Source files whose multi-clip layout is unresolvable.

    The bridge reports trim_start_s via `unclippedRange`, which currently
    fails and returns 0 for every bladed clip. A single-clip timeline is
    unaffected (trim 0 is correct there), but with >1 clip sharing a source
    file the mapping is ambiguous when trims are unknown: every clip claims
    to carry file [0, duration]. Unknown means all trims read 0 (old bridge)
    or the bridge's own candidates disagree (new bridge, trim_candidates).
    Returns the list of affected media paths. Cuts on those files must
    REFUSE (block, never wrong-cut); reads proceed best-effort with a
    `trim_degenerate` flag.
    """
    by_file: dict = {}
    for c in clips:
        mp = c.get("media_path")
        if mp:
            by_file.setdefault(mp, []).append(c)
    bad = []
    for mp, cs in by_file.items():
        if len(cs) < 2:
            continue
        eff = [_effective_trim(c) for c in cs]
        if any(t is None for t in eff) or all(t == 0 for t in eff):
            bad.append(mp)
    return bad


def _resolve_file_interval(clips: list, media_path: str, fa: float, fb: float,
                           strict: bool = False) -> list:
    """Map a FILE interval to current TIMELINE fragments.

    Source of truth for chained edits (Bug 1 fix): file times never move,
    so after each cut we re-resolve against the live clip layout instead
    of trusting timeline times stamped at transcribe time. Returns [] when
    the whole interval was already cut away.

    strict=True (cut planning): raises on degenerate trim layouts instead
    of returning fiction — a wrong outer span could cut nearly the whole
    timeline. strict=False (reads): best effort; callers must surface the
    `trim_degenerate` flag from _trim_health.
    """
    if strict and media_path in _trim_health(clips):
        raise BridgeError(
            "trim_start_s is 0 for every clip sharing "
            f"{media_path[:80]}… (bridge unclippedRange bug) — file→timeline "
            "is ambiguous, refusing to plan cuts. Fix the bridge (see "
            "docs/bridge.md), then re-read get_timeline."
        )
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
    """Sorted primary-storyline edit boundaries (timeline seconds)."""
    if clips is None:
        clips = _clips(rpc).get("clips", [])
    pts = set()
    for c in clips:
        if c.get("lane") not in (None, "primary") or c.get("container"):
            continue
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
             if c.get("id") and c.get("lane") in (None, "primary")
             and not c.get("container")
             and c["timeline_start_s"] <= mid
             < c["timeline_start_s"] + c["duration_s"]]
    if len(cands) != 1:
        revert()
        raise BridgeError(f"expected one primary clip at {mid:.3f}s after blading "
                          f"[{a}, {b}], found {len(cands)} — reverted")
    seg = cands[0]
    start = float(seg["timeline_start_s"])
    end = start + float(seg["duration_s"])
    if abs(start - a) > BLADE_EPS + 0.02 or abs(end - b) > BLADE_EPS + 0.02:
        revert()
        raise BridgeError(f"isolated clip [{start:.3f}, {end:.3f}] does not match "
                          f"requested [{a:.3f}, {b:.3f}] — reverted")
    try:
        rpc("timeline.select", {"id": seg["id"]})
    except BridgeError as e:
        revert()
        raise BridgeError(f"select failed for [{a}, {b}]: {e} — reverted.")
    return seg["id"], blades


# Mutations serialize here. Parallel client batches otherwise plan on the
# same snapshot and race (group renames, double-cuts). Reads never lock.
_WRITE_LOCK = threading.Lock()


def _settle(rpc, tries: int = 3, pause_s: float = 0.4) -> tuple:
    """Poll the timeline duration until quiescent (FCP ripples async).

    Returns (duration_s, settled). At most `tries` reads — bounded latency,
    no infinite wait. Callers verify against the settled value and report
    the flag, so a premature read can never present as a MISMATCH.
    """
    dur = _clips(rpc).get("duration_s", 0)
    for _ in range(tries - 1):
        time.sleep(pause_s)
        d2 = _clips(rpc).get("duration_s", 0)
        if abs(d2 - dur) < 0.001:
            return d2, True
        dur = d2
    return dur, False


def _cut_spans(rpc, spans: list, label: str, max_spans: int | None = None) -> dict:
    """Delete each [a, b] (timeline seconds), right-to-left so offsets hold.

    Split merged ranges at primary clip edges first. Per clip piece: blade
    at both ends (skipped at existing edit points), re-snapshot, select by
    ID, delete, and verify that the primary duration shrank by that piece.
    Stops on the first failed piece with its coordinates in `failed`.

    max_spans caps clip pieces, not original merged ranges. Defaults to 40
    to keep calls under the MCP client timeout. Pending pieces remain valid
    because cuts run right-to-left; file-anchored planners also re-resolve.
    """
    spans = _merge_spans([(float(a), float(b)) for a, b in spans])
    tiny = [(a, b) for a, b in spans if b - a < SILENCE_FRAME]
    if tiny:
        # Fail closed: a sub-frame blade selects the whole host clip and
        # deletes seconds instead of milliseconds (2026-09-23 postmortem:
        # a 1ms span removed 6.2s). Content under a frame is inaudible —
        # drop these spans, never execute them.
        show = ", ".join(f"[{a:.3f}, {b:.3f}] ({(b - a) * 1000:.0f}ms)" for a, b in tiny[:5])
        raise BridgeError(f"refusing: {len(tiny)} sub-frame span(s) {show} — "
                          "a blade here selects the host clip (overcut). Remove them "
                          "from the list; the content is inaudible.")
    spans = [(a, b) for a, b in spans if b > a]
    if not spans:
        return {"removed": 0, "removed_s": 0.0, "undo_steps": 0}
    if max_spans is not None and max_spans < 1:
        raise BridgeError("max_spans must be >= 1")
    with _WRITE_LOCK:
        return _cut_spans_locked(rpc, spans, label, max_spans)


def _split_at_primary_edges(spans: list, clips: list) -> list:
    """Turn each merged range into pieces that each belong to one primary clip."""
    primary = [c for c in clips if c.get("lane") in (None, "primary")
               and c.get("id") and not c.get("container")]
    pieces = []
    for a, b in spans:
        edges = {a, b}
        for c in primary:
            # Planner spans are millisecond-rounded. Use the same grid for
            # edit points or a 0.3ms FCP edge creates a phantom zero piece.
            start = round(float(c["timeline_start_s"]), 3)
            end = round(float(c["timeline_start_s"]) + float(c["duration_s"]), 3)
            if a < start < b:
                edges.add(start)
            if a < end < b:
                edges.add(end)
        ordered = sorted(edges)
        for lo, hi in zip(ordered, ordered[1:]):
            if hi - lo < SILENCE_FRAME:
                # FCP often reports adjacent edges 1ms apart after frame
                # rounding. A sub-frame piece cannot be bladed safely.
                continue
            mid = (lo + hi) / 2
            owners = [c for c in primary if float(c["timeline_start_s"]) <= mid
                      < float(c["timeline_start_s"]) + float(c["duration_s"])]
            if len(owners) != 1:
                raise BridgeError(f"span [{lo:.3f}, {hi:.3f}] has {len(owners)} "
                                  "primary owners — refusing ambiguous cut")
            pieces.append((round(lo, 3), round(hi, 3)))
    return sorted(pieces, reverse=True)


def _await_cut_duration(rpc, expected: float, tolerance: float = 0.2) -> tuple:
    """Wait for one ripple; unchanged early snapshots are not settled evidence."""
    actual = 0.0
    for attempt in range(5):
        if attempt:
            time.sleep(0.3)
        actual = float(_clips(rpc).get("duration_s", 0))
        if abs(actual - expected) <= tolerance:
            return actual, True
    return actual, False


def _cut_spans_locked(rpc, spans: list, label: str,
                      max_spans: int | None = None) -> dict:
    """Execute pre-validated spans right-to-left. Caller holds _WRITE_LOCK."""
    _ = label  # executor name, kept for log readability
    before = _clips(rpc)
    dur_before = before.get("duration_s", 0)
    total = dur_before
    for a, b in spans:
        if not (0 <= a < b <= total + 0.05):
            raise BridgeError(f"span [{a}, {b}] outside timeline (0, {total:.3f}) — re-plan")
    desc = _split_at_primary_edges(spans, before.get("clips", []))
    if not desc:
        raise BridgeError("no frame-sized primary clip pieces in planned spans")
    limit = max_spans if max_spans is not None else DEFAULT_CUT_PIECES
    pending = desc[limit:]
    desc = desc[:limit]
    removed_s = 0.0
    undos = 0
    applied = []
    failure = None
    actual = float(dur_before)
    for a, b in desc:
        try:
            _, blades = _isolate_span(rpc, a, b)
        except BridgeError as e:
            failure = {"span": [a, b], "reason": str(e)}
            break
        try:
            rpc("timeline.action", {"action": "delete"})
        except BridgeError as e:
            for _ in range(blades):
                rpc("timeline.undo")
            failure = {"span": [a, b], "reason": f"delete rejected: {e}; blades reverted"}
            break
        undos += blades + 1
        expected_step = actual - (b - a)
        try:
            observed, done = _await_cut_duration(rpc, expected_step)
        except BridgeError as e:
            failure = {"span": [a, b], "reason": f"cannot verify delete: {e}"}
            break
        if not done:
            failure = {"span": [a, b], "expected_duration_s": round(expected_step, 3),
                       "actual_duration_s": round(observed, 3),
                       "reason": "delete did not ripple by the requested span"}
            actual = observed
            break
        actual = observed
        removed_s += b - a
        applied.append([a, b])
    remaining = ([failure["span"]] if failure else []) + desc[len(applied) + (1 if failure else 0):] + pending
    dur_after = actual
    expected = (failure.get("expected_duration_s", dur_before - removed_s)
                if failure else dur_before - removed_s)
    ok = failure is None
    report = {
        "ok": ok,
        "removed": len(applied),
        "removed_s": round(removed_s, 3),
        "actual_removed_s": round(dur_before - dur_after, 3),
        "duration_before_s": round(dur_before, 3),
        "duration_after_s": round(dur_after, 3),
        "undo_steps": undos,
        "settled": ok,
        "verify": "ok" if ok else f"FAILED expected={expected:.3f}s actual={dur_after:.3f}s: {failure['reason']}",
        "remaining": len(remaining),
    }
    if remaining:
        report["pending"] = remaining
    if failure:
        report["failed"] = failure
        report["note"] = "stopped after the first failed ripple; inspect before retrying"
    elif pending:
        report["note"] = (f"chunked: cut {len(applied)} clip pieces, {len(pending)} pending — "
                          "re-call with the same args until remaining is 0")
    return report


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
def get_timeline(start_s: float | None = None, end_s: float | None = None,
                 clip_id: str | None = None, offset: int = 0,
                 limit: int = 50, detail: str = "compact") -> dict:
    """Read a bounded timeline window or one clip by ID.

    Compact clips include ID, lane, timeline position, duration and source
    trim. Use start_s/end_s for a region, clip_id for one clip, or page with
    next_offset. detail='full' adds raw bridge fields for the selected page.
    """
    args = {"start_s": start_s, "end_s": end_s, "clip_id": clip_id,
            "offset": offset, "limit": limit, "detail": detail}

    def run(rpc):
        if detail not in ("compact", "full"):
            return {"ok": False, "error": "detail must be compact or full"}
        if limit < 1 or offset < 0:
            return {"ok": False, "error": "limit must be >= 1 and offset >= 0"}
        if start_s is not None and end_s is not None and start_s >= end_s:
            return {"ok": False, "error": "start_s must be before end_s"}
        state = _clips(rpc)
        all_clips = state.get("clips", [])
        rows = all_clips
        if clip_id is not None:
            rows = [c for c in rows if c.get("id") == clip_id]
        if start_s is not None:
            rows = [c for c in rows if c.get("timeline_start_s", 0)
                    + c.get("duration_s", 0) > start_s]
        if end_s is not None:
            rows = [c for c in rows if c.get("timeline_start_s", 0) < end_s]
        count = len(rows)
        page = rows[offset:offset + min(limit, 100)]
        if detail == "compact":
            keys = ("id", "name", "lane", "class", "timeline_start_s",
                    "duration_s", "trim_start_s", "parent")
            page = [{k: c[k] for k in keys if k in c} for c in page]
        return {"sequence_name": state.get("sequence_name"),
                "duration_s": state.get("duration_s"), "fps": state.get("fps"),
                "clip_count": len(all_clips), "matched_clip_count": count,
                "clips": page,
                "next_offset": offset + len(page) if offset + len(page) < count else None}

    return logged("get_timeline", args, run)


@mcp.tool()
def get_playhead() -> dict:
    """Playhead time, fps, sequence duration."""
    return logged("get_playhead", {}, lambda rpc: rpc("playback.position"))


# ---------------------------------------------------------------- silences

SILENCE_FRAME = 1 / 30  # sub-frame slivers are uncuttable; drop them

# Kept islands at/below this shape that sit between two cut spans are
# breathing / false-start slop, not content: zero transcribed words
# (breath, mouth noise the detector scored above threshold) or a couple
# of filler/low-confidence words ("Okay so", "uh", retry stutter).
FILLER_WORDS = frozenset({
    "okay", "ok", "so", "um", "uh", "uh-huh", "yeah", "yep", "like",
    "you", "know", "well", "right", "hmm", "hm", "oh", "ah", "er",
    "and", "but", "so-", "i", "i-", "let", "lets", "let's",
})

# ---------------------------------------------------------------- verbatim
# Unified verbatim enrichment (single engine, single schema — no ASR fork).
#
# Adaptation of the verbatim-cut insight (verbatim transcript + gap events
# + filler/repeat tags): Parakeet stays the only ASR, so there is exactly
# one timestamp behavior and one cut routing. These pure-Python taggers run
# on cached words at transcribe time and backfill older caches on load.
# A second ASR engine (faster-whisper verbatim) is deliberately NOT added:
# it would fork timestamp bias, cache keys, and the test matrix for fillers
# Parakeet already drops (unrecoverable post-hoc anyway).
#
# Words carry optional additive flags (absent == False, so v3-era tests and
# hand-built caches keep working):
#   filler: word matches the filler list (may still be content — "like")
#   repeat: first take of an immediately repeated word/phrase ("I I think")
VERBATIM_FILLERS = (
    "um,umm,uh,uhh,erm,er,ah,hmm,mm,mhm,like,you know,i mean,sort of,kind of"
)
VERBATIM_MIN_GAP_S = 0.15  # gaps shorter than this are not events


def _norm_token(t: str) -> str:
    return re.sub(r"[^a-z0-9' ]", "", (t or "").lower()).strip()


def _tag_filler_words(words: list, fillers: str = VERBATIM_FILLERS) -> int:
    """Mark filler words in place. Returns the count newly flagged.

    Multi-word fillers ("you know", "i mean", "sort of") match consecutive
    words. Single-word entries match one word. Idempotent.
    """
    seqs = [s for s in (_norm_token(x).split() for x in fillers.split(",")) if s]
    if not seqs or not words:
        return 0
    norms = [_norm_token(w.get("w", "")) for w in words]
    changed = 0
    i = 0
    while i < len(words):
        for f in sorted(seqs, key=len, reverse=True):
            n = len(f)
            if " ".join(norms[i:i + n]) == " ".join(f) and norms[i:i + n]:
                for w in words[i:i + n]:
                    if not w.get("filler"):
                        w["filler"] = True
                        changed += 1
                i += n - 1
                break
        i += 1
    return changed


def _tag_repeat_words(words: list, max_n: int = 3, window_s: float = 2.0) -> int:
    """Mark the first take of immediately repeated words/phrases in place.

    If words[i:i+n] == words[i+n:i+2n] within window_s, the first copy gets
    repeat=True (the second take is usually the clean one). Idempotent.
    Returns the count of words newly flagged.
    """
    if not words:
        return 0
    norms = [_norm_token(w.get("w", "")) for w in words]
    changed = 0
    for n in range(max_n, 0, -1):
        for i in range(len(words) - 2 * n + 1):
            a, b = words[i:i + n], words[i + n:i + 2 * n]
            if any(t.get("repeat") for t in a + b):
                continue
            try:
                if ([norms[i + k] for k in range(n)]
                        == [norms[i + n + k] for k in range(n)]
                        and all(norms[i + k] for k in range(n))
                        and float(b[-1]["f_end"]) - float(a[0]["f_start"]) <= window_s):
                    for t in a:
                        if not t.get("repeat"):
                            t["repeat"] = True
                            changed += 1
            except (KeyError, TypeError, ValueError):
                continue
    return changed


def _ensure_word_tags(words: list) -> bool:
    """Backfill filler/repeat flags on a loaded cache. Returns True if changed."""
    if not words or "f_start" not in words[0]:
        return False
    before = sum(1 for w in words if w.get("filler") or w.get("repeat"))
    _tag_filler_words(words)
    _tag_repeat_words(words)
    return sum(1 for w in words if w.get("filler") or w.get("repeat")) != before


def _word_gaps(words: list, min_gap_s: float = VERBATIM_MIN_GAP_S) -> list:
    """Inter-word gaps (FILE seconds) longer than min_gap_s — pure, no I/O.

    Every word has f_start/f_end, so every gap is a candidate event (the
    verbatim-cut trick). Energy labeling happens separately in
    _label_gaps_with_silences, which needs a detector run; reads use the
    unlabeled gap (kind="gap") so they never spawn engines.
    """
    out = []
    for a, b in zip(words, words[1:]):
        try:
            gs, ge = float(a["f_end"]), float(b["f_start"])
        except (KeyError, TypeError, ValueError):
            continue
        if ge - gs >= min_gap_s:
            out.append({"f_start": round(gs, 3), "f_end": round(ge, 3),
                        "dur_s": round(ge - gs, 3), "kind": "gap"})
    return out


def _label_gaps_with_silences(gaps: list, file_silences: list) -> list:
    """Label file-space gaps as silence vs noise using detector ranges.

    file_silences: [{"start","duration"}] in FILE seconds (the detector's
    native space). A gap fully covered by a silent range is "silence";
    a gap with sound but no word is "noise" (breath, laugh, dropped
    filler, room noise — deliberately not sub-classified). Pure function.
    """
    sil = []
    for r in file_silences or []:
        try:
            s = float(r["start"])
            sil.append((s, s + float(r["duration"])))
        except (KeyError, TypeError, ValueError):
            continue
    labeled = []
    for g in gaps:
        gs, ge = float(g["f_start"]), float(g["f_end"])
        covered = any(s <= gs + 1e-6 and ge <= e + 1e-6 for s, e in sil)
        labeled.append({**g, "kind": "silence" if covered else "noise"})
    return labeled


def _collapse_silence_islands(spans: list, rpc, max_island_s: float) -> tuple:
    """Merge cut spans across tiny kept islands (breath / false-start slop).

    `spans` are merged timeline-second cut spans. An interior gap
    [prev_end, next_start] with duration <= max_island_s is absorbed (the
    two spans merged) when the gap holds no real speech: zero transcribed
    words, or only a few filler/low-confidence words. Returns
    (spans, collapsed_count, details).
    """
    if not spans or len(spans) < 2 or max_island_s <= 0:
        return spans, 0, []
    try:
        t = _last_transcript()
    except Exception:
        t = None
    words = (t.get("words", []) if t else []) or []
    atom = []
    for w in words:
        try:
            atom.append((float(w["f_start"]), float(w["f_end"]),
                         str(w.get("w", "")).strip().strip(",.?!…'\"").lower(),
                         float(w.get("confidence", 1.0) or 0.0),
                         bool(w.get("filler")), bool(w.get("repeat"))))
        except (KeyError, TypeError, ValueError):
            continue
    try:
        clips = _all_existing_clips(rpc)
    except BridgeError:
        return spans, 0, []
    by_start = {round(a, 3): round(b, 3) for a, b in spans}
    ordered = sorted((round(a, 3), round(b, 3)) for a, b in spans)
    out = [list(ordered[0])]
    collapsed, details = 0, []
    for next_a, next_b in ordered[1:]:
        cur_b = out[-1][1]
        gap = round(next_a - cur_b, 3)
        if gap <= 0:
            out[-1][1] = max(cur_b, next_b)
            continue
        if gap > max_island_s:
            out.append([next_a, next_b])
            continue
        # Map the kept gap back to file intervals and collect words inside.
        gap_words = []
        for c in clips:
            try:
                tb = float(c["timeline_start_s"])
                dur = float(c["duration_s"])
            except (KeyError, TypeError, ValueError):
                continue
            lo, hi = max(cur_b, tb), min(next_a, tb + dur)
            if hi <= lo:
                continue
            f0, _ = _clip_file_window(c)
            fa, fb = f0 + (lo - tb), f0 + (hi - tb)
            for fs, fe, wl, conf, is_filler, is_repeat in atom:
                if fe > fa and fs < fb and (wl, conf) not in [(w, c) for w, c, _, _ in gap_words]:
                    gap_words.append((wl, conf, is_filler, is_repeat))
        if not gap_words:
            reason = "no transcribed words (breath/noise)"
            absorb = True
        elif (len(gap_words) <= 4
                and all(w in FILLER_WORDS or fl or rp or cf < 0.7
                        for w, cf, fl, rp in gap_words)):
            tagged = [w for w, _, fl, rp in gap_words if fl or rp]
            if tagged:
                reason = f"false-start filler ({' '.join(tagged)[:60]})"
            else:
                reason = f"false-start filler ({' '.join(w for w, _, _, _ in gap_words)[:60]})"
            absorb = True
        else:
            absorb = False
        if absorb:
            out[-1][1] = next_b
            collapsed += 1
            details.append({"gap_start": cur_b, "gap_end": next_a,
                            "gap_s": gap,
                            "words": [w for w, _, _, _ in gap_words],
                            "reason": reason})
        else:
            out.append([next_a, next_b])
    merged = _merge_spans([(a, b) for a, b in out])
    return merged, collapsed, details


def _word_guard_intervals(media_path: str, margin_s: float) -> list:
    """Speech intervals (FILE seconds) expanded by margin_s, merged.

    File times are immutable across edits, so the cached transcript is a
    valid guard even after the timeline rippled. Returns [] when no usable
    transcript exists for this file (caller proceeds unguarded).
    """
    try:
        t = _last_transcript()
    except Exception:
        return []
    if not t or t.get("media_path") != media_path:
        return []
    words = t.get("words", [])
    if words and "f_start" not in words[0]:
        return []
    ivs = sorted((float(w["f_start"]) - margin_s, float(w["f_end"]) + margin_s)
                 for w in words if "f_start" in w and "f_end" in w)
    out = []
    for a, b in ivs:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _subtract_intervals(base_a: float, base_b: float, cuts: list) -> list:
    """Subtract sorted merged `cuts` from [base_a, base_b]; return remainders."""
    out, cur = [], base_a
    for a, b in cuts:
        if b <= cur or a >= base_b:
            continue
        if a > cur:
            out.append((cur, min(a, base_b)))
        cur = max(cur, b)
        if cur >= base_b:
            break
    if cur < base_b:
        out.append((cur, base_b))
    return out


def _silence_scan(rpc, threshold_db: float, min_duration_s: float, for_cut: bool, pad_s: float,
                  word_guard_s: float = 0.3) -> tuple:
    """Run the detector once per unique source file, map hits into every
    on-disk primary clip carrying that file (Bug 3 fix).

    Python owns padding (the detector is always invoked with --padding 0,
    so pad_s is applied exactly once here): detect expands raw silence by
    pad and clamps to the clip; remove insets by pad so speech breathes.
    When cutting (for_cut), speech intervals from the cached transcript
    (expanded by word_guard_s) are subtracted from each silence BEFORE
    mapping — the 2026-09-22 cut proved a fixed -34 dB threshold alone
    eats word onsets/offsets (132/169 spans overlapped words). for_cut on
    a degenerate trim layout raises instead of mapping fiction (see
    _trim_health). Returns (spans, per_file) with spans merged timeline seconds.
    Each per_file entry also carries gap_events: inter-word gaps from the
    cached transcript (when it covers this file) labeled silence vs noise
    against this same detector run — no extra subprocess, no new routing.
    """
    clips = _all_existing_clips(rpc)
    if for_cut:
        bad = _trim_health(clips)
        if bad:
            raise BridgeError(
                "trim_start_s is 0 for every clip sharing "
                f"{bad[0][:80]}… (bridge unclippedRange bug) — silence hits "
                "cannot be mapped, refusing to plan cuts. Fix the bridge "
                "(see docs/bridge.md), then retry."
            )
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
        guarded_hits = 0
        guard = _word_guard_intervals(path, word_guard_s) if for_cut else []
        for r in res.get("silentRanges", []):
            fs, fe = float(r["start"]), float(r["start"]) + float(r["duration"])
            # Word guard (cut path only): never cut over speech. Subtract
            # first in FILE space so the pad inset below only ever shrinks
            # true inter-word gaps.
            pieces = _subtract_intervals(fs, fe, guard) if guard else [(fs, fe)]
            if for_cut and guard and sum(b - a for a, b in pieces) < (fe - fs) - 1e-9:
                guarded_hits += 1
            for ps, pe in pieces:
                for c in fclips:
                    f0, f1 = _clip_file_window(c)
                    lo, hi = max(ps, f0), min(pe, f1)
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
                        continue
                    a = max(base + (lo - f0) - pad_s, base)
                    b = min(base + (hi - f0) + pad_s, base + float(c["duration_s"]))
                    if b > a:
                        spans.append((round(a, 3), round(b, 3)))
                        mapped += 1
        entry = {"file": path, "clips": len(fclips), "mapped_hits": mapped}
        if for_cut and guarded_hits:
            entry["word_guarded"] = guarded_hits
        try:
            t = _last_transcript()
        except Exception:
            t = None
        if t and t.get("media_path") == path and t.get("words"):
            gaps = _word_gaps(t["words"])
            labeled = _label_gaps_with_silences(gaps, res.get("silentRanges", []))
            counts: dict = {}
            for g in labeled:
                counts[g["kind"]] = counts.get(g["kind"], 0) + 1
            entry["gap_events"] = labeled
            entry["gap_counts"] = counts
        per_file.append(entry)
    return _merge_spans(spans), per_file


def _silence_warning(spans: list, rpc=None, duration_s: float | None = None,
                     min_duration_s: float | None = None) -> dict:
    total = round(sum(b - a for a, b in spans), 3)
    if duration_s is None and rpc is not None:
        try:
            duration_s = _clips(rpc).get("duration_s", 0)
        except BridgeError:
            duration_s = 0
    frac = round(total / duration_s, 3) if duration_s else 0
    warn = None
    if frac >= 0.2 or total >= 120:
        # Never recommend a param the caller already set (postmortem 8/10).
        if min_duration_s is not None and min_duration_s >= 1.0:
            hint = "raise threshold_db (e.g. -30) or accept — min_duration_s is already strict"
        else:
            hint = "consider min_duration_s=1.0 and a dry_run review first"
        warn = (f"aggressive: would remove {total}s ({frac * 100:.0f}% of timeline) — {hint}")
    return {"would_remove_s": total, "fraction": frac, "warning": warn}


@mcp.tool()
def detect_silences(
    threshold_db: float = -34.0, min_duration_s: float = 0.5, pad_s: float = 0.2
) -> dict:
    """Find silent spans via native AVFoundation analysis of the source files.

    Scans EVERY on-disk primary clip (grouped by file — one detector run
    per unique source), mapped to timeline seconds. threshold_db=-34 ≈
    'auto' sensitivity floor. Per-file `mapped_hits` counts pre-merge
    detections; `count` is post-merge spans (merged <= mapped, always).
    Per-file entries also carry `gap_events` (inter-word gaps labeled
    silence vs noise against the same detector run) and `gap_counts`.
    """
    args = {"threshold_db": threshold_db, "min_duration_s": min_duration_s, "pad_s": pad_s}

    def run(rpc):
        spans, per_file = _silence_scan(rpc, threshold_db, min_duration_s, False, pad_s)
        state = _clips(rpc)
        warn = _silence_warning(spans, duration_s=state.get("duration_s", 0),
                                min_duration_s=min_duration_s)
        out = {"clips_scanned": sum(f["clips"] for f in per_file), "files": per_file,
               "silences": [{"start_s": a, "end_s": b} for a, b in spans],
               "count": len(spans), **warn}
        bad = _trim_health(state.get("clips", []))
        if bad:
            out["trim_degenerate"] = bad
            out["trim_note"] = ("bridge reports trim_start_s=0 for every clip sharing "
                                "these files — detect positions are best-effort and "
                                "cuts are blocked until the bridge is fixed")
        return out

    return logged("detect_silences", args, run)


@mcp.tool()
def remove_silences(
    threshold_db: float = -34.0,
    min_duration_s: float = 0.5,
    pad_s: float = 0.2,
    dry_run: bool = True,
    max_spans: int | None = None,
    word_guard_s: float = 0.3,
    collapse_islands: bool = True,
    max_island_s: float = 1.5,
) -> dict:
    """Cut every silence (pad kept each side so speech breathes).

    dry_run=True (default) returns the cut list without writing. Speech from
    the cached transcript (expanded by word_guard_s) is subtracted from each
    silence before cutting, so a hot threshold can't eat word onsets.
    When collapse_islands is true (default), tiny kept islands between two
    silences (<= max_island_s, holding no real speech: breath/noise or a
    filler false-start like "Okay so" / "uh") are absorbed into the cut, so
    `[speech] [breath] [Okay so][retry] [speech]` becomes `[speech][speech]`
    instead of leaving 0.5s breathing slivers behind.
    Cuts at most 40 primary clip pieces per call by default; max_spans
    overrides this cap. Re-call with the same args (file-anchored spans
    re-resolve) until `remaining` is 0. Each chunk is logged.
    """

    args = {"threshold_db": threshold_db, "min_duration_s": min_duration_s,
            "pad_s": pad_s, "dry_run": dry_run, "max_spans": max_spans,
            "word_guard_s": word_guard_s, "collapse_islands": collapse_islands,
            "max_island_s": max_island_s}

    def run(rpc):
        try:
            spans, per_file = _silence_scan(rpc, threshold_db, min_duration_s, True,
                                            pad_s, word_guard_s)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        islands_collapsed, island_details = 0, []
        if collapse_islands and spans:
            spans, islands_collapsed, island_details = _collapse_silence_islands(
                spans, rpc, max_island_s)
        warn = _silence_warning(spans, rpc, min_duration_s=min_duration_s)
        if dry_run or not spans:
            out = {"dry_run": dry_run, "files": per_file, "spans": spans, **warn}
            if collapse_islands:
                out["islands_collapsed"] = islands_collapsed
                if island_details:
                    out["island_details"] = island_details[:20]
            return out
        try:
            report = _cut_spans(rpc, spans, "remove_silences", max_spans)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        report["files"] = per_file
        if collapse_islands:
            report["islands_collapsed"] = islands_collapsed
            if island_details:
                report["island_details"] = island_details[:20]
        if warn["warning"]:
            report["warning"] = warn["warning"]
        return report

    return logged("remove_silences", args, run)


@mcp.tool()
def cut_spans(spans: list[list[float]], dry_run: bool = True,
              max_spans: int | None = None) -> dict:
    """Cut explicit timeline-second spans [[a, b], ...], right-to-left.

    The chunk executor: validate whole, split at primary clip edges, cut
    at most `max_spans` pieces (40 by default, rightmost first), and
    report `pending` for the next call. Stops on a failed ripple.
    """
    args = {"spans": spans, "dry_run": dry_run, "max_spans": max_spans}

    def run(rpc):
        try:
            clean = _merge_spans([(float(a), float(b)) for a, b in spans])
        except (TypeError, ValueError):
            return {"ok": False, "error": "spans must be [[start_s, end_s], ...]"}
        if dry_run:
            return {"dry_run": True, "spans": clean,
                    "would_remove_s": round(sum(b - a for a, b in clean), 3),
                    "count": len(clean)}
        try:
            return {"ok": True, **_cut_spans(rpc, clean, "cut_spans", max_spans)}
        except BridgeError as e:
            return {"ok": False, "error": str(e)}

    return logged("cut_spans", args, run)


@mcp.tool()
def apply_cut_list(keep_ranges: list[list[float]], dry_run: bool = True,
                   max_spans: int | None = None) -> dict:
    """Rough cut in one verb: keep [[start_s, end_s]...], drop the rest, close gaps.

    Ranges validated whole (sorted, non-overlapping, inside the timeline) before
    any write. Cuts run right-to-left so offsets hold; duration verified after.
    For large drops pass max_spans=N: cuts the rightmost N primary clip pieces,
    returns `pending` — finish with cut_spans(pending) (same spans stay valid
    because right-to-left cuts never shift earlier positions), looping until
    `remaining` is 0. Sum each call's `undo_steps` for a full revert.
    """
    args = {"keep_ranges": keep_ranges, "dry_run": dry_run, "max_spans": max_spans}

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
        try:
            report = _cut_spans(rpc, drops, "apply_cut_list", max_spans)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        report["kept"] = len(ranges)
        return report

    return logged("apply_cut_list", args, run)


# ---------------------------------------------------------------- transcript


def _cache_key(media_path: str, engine: str, model: str) -> str:
    st = os.stat(media_path)
    h = hashlib.sha1(
        f"{media_path}|{st.st_mtime_ns}|{st.st_size}|{engine}|{model}|schema{SCHEMA_VERSION}".encode()
    )
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


# ---------------------------------------------------------------- story
# Text-first editing: stable fragment IDs over FILE words, keep-list plans.
#
# The model never does range math. It reads compact lines (id + text),
# declares the desired shape (keep these ids in this order), and the
# server resolves ids -> file intervals -> live timeline spans at commit.
# Fragment ids (L{start_word:04d}) derive from immutable file word indices,
# so unlike positional sentence numbers they never shift after a cut.

CRUMB_WARN_S = 0.25  # kept fragments shorter than this are flagged, never silently absorbed


def _story_fragments(cache_words: list, max_words: int = 40) -> list:
    """Chunk FILE-ordered words with the same policy as _sentences.

    Ids are file-anchored (L{first_word_index:04d}) — stable across edits.
    """
    out, cur = [], []

    def flush():
        if cur:
            frag = {
                "id": f"L{cur[0]['i']:04d}",
                "start_word": cur[0]["i"],
                "end_word": cur[-1]["i"],
                "text": " ".join(w["w"] for w in cur),
                "f_start": float(cur[0]["f_start"]),
                "f_end": float(cur[-1]["f_end"]),
            }
            if any(w.get("filler") for w in cur):
                frag["has_filler"] = True
            if any(w.get("repeat") for w in cur):
                frag["has_repeat"] = True
            out.append(frag)
            cur.clear()

    for w in cache_words:
        cur.append(w)
        if re.search(r"[.?!…]['\"]?$", w.get("w", "")) or len(cur) >= max_words:
            flush()
    flush()
    return out


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t.lower())).strip()


def _story_crumb(f: dict) -> bool:
    """A surviving line too short to cut safely or count as a live take."""
    return (not f.get("removed") and f.get("t_start", -1) >= 0
            and 0 <= f.get("t_end", -1) - f["t_start"] < SILENCE_FRAME)


def _take_groups(frags: list, window: int = 10, ratio: float = 0.6) -> list:
    """Group near-duplicate fragments (retakes) within a sliding window.

    Union-find over normalized SequenceMatcher ratio. Group ids derive
    from the smallest member's FILE word index across ALL members ever
    (removed included — pass the full fragment list), so an id never
    renames when a take is resolved (2026-09-23 postmortem item 9).
    Groups with <=1 active member are `resolved` (kept = the survivor).
    """
    n = len(frags)
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Retake identities must stay tied to the original transcription even
    # when a later word trim changes the text shown to the editor.
    norms = [_norm_text(f.get("_source_text", f["text"])) for f in frags]
    for i in range(n):
        if len(norms[i]) < 12:
            continue  # too short to fuzzy-match meaningfully
        for j in range(i + 1, min(n, i + 1 + window)):
            if len(norms[j]) < 12:
                continue
            if difflib.SequenceMatcher(None, norms[i], norms[j]).ratio() >= ratio:
                union(i, j)
    buckets: dict = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)
    groups = []
    for members in buckets.values():
        if len(members) >= 2:
            ids = [frags[k]["id"] for k in members]
            active = [frags[k]["id"] for k in members
                      if not frags[k].get("removed") and not _story_crumb(frags[k])]
            gid = f"G{min(frags[k]['start_word'] for k in members):04d}"
            groups.append({"group": gid, "members": ids, "active": active,
                           "resolved": len(active) <= 1,
                           "kept": active[0] if len(active) == 1 else None,
                           "texts": [frags[k]["text"] for k in members]})
    return sorted(groups, key=lambda g: g["group"])


def _story_live(rpc, t: dict) -> tuple:
    """Stable fragments with live timeline positions attached.

    Returns (frags, missing). frags are in FILE order; each carries
    t_start/t_end (outer span) or removed=True when fully cut away.
    """
    cache_words = t.get("words", [])
    if cache_words and "f_start" not in cache_words[0]:
        raise BridgeError("transcript predates file-relative times — re-run transcribe once")
    media = t.get("media_path")
    if media is None:
        raise BridgeError("transcript has no media_path — re-run transcribe")
    clips = _all_existing_clips(rpc)
    frags = _story_fragments(cache_words)
    words_by_index = {w["i"]: w for w in cache_words}
    missing = 0
    for f in frags:
        f["_source_text"] = f["text"]
        hits = _resolve_file_interval(clips, media, f["f_start"], f["f_end"])
        if not hits:
            missing += 1
            f["t_start"], f["t_end"], f["removed"] = -1, -1, True
        else:
            substantial = [(a, b) for a, b in hits if b - a >= SILENCE_FRAME]
            # A few milliseconds of a source line can survive a blade far
            # away from its main clip. Never let that sliver stretch its
            # apparent timeline extent across unrelated footage.
            core = substantial[0] if len(substantial) == 1 else None
            f["t_start"], f["t_end"] = core if core else (hits[0][0], hits[-1][1])
            if len(hits) > 1:
                f["live_spans"] = hits
            if len(substantial) > 1:
                f["discontinuous"] = True
            visible = []
            for i in range(f["start_word"], f["end_word"] + 1):
                w = words_by_index.get(i)
                if w is None:
                    continue
                word_hits = _resolve_file_interval(clips, media, w["f_start"], w["f_end"])
                if sum(b - a for a, b in word_hits) >= SILENCE_FRAME:
                    visible.append(w["w"])
            if visible:
                f["text"] = " ".join(visible)
    # Groups computed over ALL frags (removed included) so ids are stable
    # across resolutions; removed members keep their group for history.
    for g in _take_groups(frags):
        for mid in g["members"]:
            for f in frags:
                if f["id"] == mid:
                    f["take_group"] = g["group"]
    return frags, missing


def _expand_to_midpoints(frag, by_index: dict, cache_words: list) -> tuple:
    """Expand a drop fragment's file interval to inter-word midpoints.

    Blades then land in silence between words instead of mid-phoneme —
    the fix for orphan micro-word crumbs after frame-snap.
    """
    pos = {w["i"]: k for k, w in enumerate(cache_words)}
    first, last = cache_words[pos[frag["start_word"]]], cache_words[pos[frag["end_word"]]]
    fa, fb = float(first["f_start"]), float(last["f_end"])
    lo, hi = pos[frag["start_word"]], pos[frag["end_word"]]
    if lo > 0:
        prev = cache_words[lo - 1]
        if "f_end" in prev:
            fa = (float(prev["f_end"]) + fa) / 2
    if hi + 1 < len(cache_words):
        nxt = cache_words[hi + 1]
        if "f_start" in nxt:
            fb = (fb + float(nxt["f_start"])) / 2
    _ = by_index  # reserved for future cross-file maps
    return (round(fa, 3), round(fb, 3))


def _story_plan(rpc, keep_ids: list) -> dict:
    """Validate a keep-list and resolve it to drops + moves (no writes).

    Returns error dict or plan dict with text-first fields the model can
    verify by eye: will_remove (id+text), moves (id after anchor), plus
    machine fields (spans, would_remove_s) for the executor.
    """
    t = _fresh_transcript(rpc)
    cache_words = t.get("words", [])
    media = t.get("media_path")
    frags, _ = _story_live(rpc, t)
    known = {f["id"] for f in frags}
    if not keep_ids:
        return {"ok": False, "error": "keep must be a non-empty list of fragment ids"}
    if len(set(keep_ids)) != len(keep_ids):
        dupes = sorted({k for k in keep_ids if keep_ids.count(k) > 1})
        return {"ok": False, "error": f"duplicate ids in keep: {dupe_str(dupes)}"}
    unknown = [k for k in keep_ids if k not in known]
    if unknown:
        return {"ok": False, "error": f"unknown fragment ids (re-read get_story): {unknown[:8]}"}
    by_id = {f["id"]: f for f in frags}
    gone = [k for k in keep_ids if by_id[k].get("removed")]
    if gone:
        return {"ok": False, "error": f"already-removed ids in keep (re-read get_story): {gone[:8]}"}
    keep_set = set(keep_ids)
    live_order = [f["id"] for f in sorted(
        (f for f in frags if not f.get("removed")), key=lambda f: f["t_start"])]
    drop_frags = [by_id[i] for i in live_order if i not in keep_set]
    # Drops resolve via midpoint-expanded file intervals (crumb fix).
    by_index = {}
    spans: list = []
    unremoved: list = []
    if media in _trim_health(_all_existing_clips(rpc)):
        return {"ok": False, "error": "trim layout degenerate — word cuts blocked until the bridge is fixed"}
    clips = _all_existing_clips(rpc)
    for f in drop_frags:
        fa, fb = _expand_to_midpoints(f, by_index, cache_words)
        hits = _resolve_file_interval(clips, media, fa, fb, strict=True)
        if not hits or sum(b - a for a, b in hits) < SILENCE_FRAME:
            # Honest books: the fragment stays on the timeline (sub-frame
            # crumb — inaudible). will_remove must not claim it.
            unremoved.append({"id": f["id"], "text": f["text"],
                              "reason": "sub-frame crumb left in place (inaudible)"})
            continue
        for a, b in hits:
            spans.append((a, b))
    spans = _merge_spans(spans)
    spans = [(a, b) for a, b in spans if b - a >= SILENCE_FRAME]
    # Moves: minimal plan — longest already-ordered subsequence stays.
    cur_pos = {fid: k for k, fid in enumerate(live_order) if fid in keep_set}
    want = [fid for fid in keep_ids]
    # LIS over current positions in desired order.
    import bisect as _bisect
    tails: list = []
    for fid in want:
        p = cur_pos[fid]
        k = _bisect.bisect_left(tails, p)
        if k == len(tails):
            tails.append(p)
        else:
            tails[k] = p
    need_move = len(want) - len(tails)
    moves: list = []
    if need_move:
        placed = set()
        # Simulate left-to-right placement; anything out of place moves after its predecessor.
        cur = [fid for fid in live_order if fid in keep_set]
        for k, fid in enumerate(want):
            if k < len(cur) and cur[k] == fid:
                placed.add(fid)
                continue
            anchor = want[k - 1] if k else None
            moves.append({"id": fid, "after": anchor} if anchor else {"id": fid, "before": want[1] if len(want) > 1 else None})
            c = [x for x in cur if x != fid]
            ins = (c.index(anchor) + 1) if anchor and anchor in c else 0
            c.insert(ins, fid)
            cur = c
    crumbs = [{"id": f["id"], "text": f["text"],
               "dur_s": round(f["t_end"] - f["t_start"], 3)}
              for f in frags if f["id"] in keep_set
              and not f.get("removed") and (f["t_end"] - f["t_start"]) < CRUMB_WARN_S]
    state = _clips(rpc)
    dur = state.get("duration_s", 0)
    try:
        pieces = _split_at_primary_edges(spans, state.get("clips", []))
    except BridgeError as e:
        return {"ok": False, "error": str(e)}
    if spans and not pieces:
        return {"ok": False, "error": "planned drops contain no frame-sized primary clip pieces"}
    edge_slivers_s = round(sum(b - a for a, b in spans) - sum(b - a for a, b in pieces), 3)
    remove_s = round(sum(b - a for a, b in pieces), 3)
    out = {"ok": True, "keep": keep_ids, "kept": len(keep_ids),
           "will_remove": [{"id": f["id"], "text": f["text"]} for f in drop_frags
                           if f["id"] not in {u["id"] for u in unremoved}],
           "drops": len(drop_frags) - len(unremoved), "spans": spans,
           "clip_pieces": len(pieces),
           "expected_calls": (len(pieces) + DEFAULT_CUT_PIECES - 1) // DEFAULT_CUT_PIECES,
           "edge_slivers_s": edge_slivers_s,
           "would_remove_s": remove_s,
           "duration_before_s": round(dur, 3),
           "duration_after_s": round(dur - remove_s, 3),
           "duration_prediction": "estimate; commit checks live ripple per clip piece",
           "moves": moves, "crumbs": crumbs}
    if unremoved:
        out["unremoved"] = unremoved
    return out


def dupe_str(dupes: list) -> str:
    return ", ".join(dupes[:8])


def _move_live_span(rpc, span: tuple, dest_t: float) -> tuple:
    """Cut one live span and paste at dest_t; return (destination, undo entries)."""
    span_len = span[1] - span[0]
    _, blades = _isolate_span(rpc, span[0], span[1])
    rpc("timeline.action", {"action": "cut"})
    dest_adj = dest_t - span_len if dest_t > span[1] else dest_t
    rpc("playback.seek", {"t_s": max(0, dest_adj)})
    rpc("timeline.action", {"action": "paste"})
    return dest_adj, blades + 2


def _move_extent(frag: dict) -> tuple:
    """One movable line interval, excluding sub-frame source remnants."""
    spans = frag.get("live_spans") or [(frag["t_start"], frag["t_end"])]
    core = [(a, b) for a, b in spans if b - a >= SILENCE_FRAME]
    if len(core) != 1:
        raise BridgeError(f"{frag['id']} has {len(core)} frame-sized live spans "
                          f"{[[round(a, 3), round(b, 3)] for a, b in core]} — "
                          "move_line needs one contiguous span")
    return core[0]


def _move_order_check(rpc, move: dict) -> dict:
    """Explain the moved line's actual neighbor and live clip bounds."""
    frags, _ = _story_live(rpc, _fresh_transcript(rpc))
    active = [f for f in frags if not f.get("removed") and not _story_crumb(f)]
    order = [f["id"] for f in sorted(active, key=lambda f: f["t_start"])]
    by_id = {f["id"]: f for f in active}
    src = move["id"]
    anchor = move.get("after") or move.get("before")
    if src not in by_id or anchor not in by_id:
        return {"verified": False, "reason": "source or anchor absent after move",
                "source": src, "anchor": anchor}
    index = order.index(src)
    after = "after" in move
    neighbor = (order[index - 1] if index else None) if after else (
        order[index + 1] if index + 1 < len(order) else None)
    try:
        source_span = _move_extent(by_id[src])
        anchor_span = _move_extent(by_id[anchor])
    except BridgeError as e:
        return {"verified": False, "reason": str(e),
                "expected_neighbor": anchor, "actual_neighbor": neighbor}
    return {"verified": neighbor == anchor,
            "expected_neighbor": anchor, "actual_neighbor": neighbor,
            "source_span": [round(x, 3) for x in source_span],
            "anchor_span": [round(x, 3) for x in anchor_span]}


def _load_cache(key: str) -> dict | None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
        if data.get("schema") == SCHEMA_VERSION:
            return data
        # v3 -> v4: additive filler/repeat flags only. Backfill in place
        # instead of forcing a re-transcribe; anything older is a real MISS.
        if data.get("schema") == SCHEMA_VERSION - 1 and data.get("words"):
            w0 = data["words"][0] if data["words"] else {}
            if "f_start" not in w0 or not data.get("media_path"):
                return None
            _ensure_word_tags(data["words"])
            data["schema"] = SCHEMA_VERSION
            try:
                with open(p, "w") as f:
                    json.dump(data, f)
            except OSError:
                pass
            return data
        return None  # stale schema: cache MISS (forces re-transcribe, never dead-ends)
    return None


def _save_cache(key: str, data: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    data["schema"] = SCHEMA_VERSION
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
        # Legacy schema (predates file-relative times): the upgrade error
        # would dead-end forever on a cache hit — invalidate and fall
        # through so the engine actually re-runs ("re-run once" must work).
        if cached and ((cached.get("words") and "f_start" not in cached["words"][0])
                       or not cached.get("media_path")):
            cached = None
        if cached:
            # Cache hit: re-resolve live positions (timeline may have
            # rippled since the transcribe) and re-stamp the duration, so
            # the guard can never dead-end after a cut (Bug 1 fix).
            if _ensure_word_tags(cached.get("words", [])):
                pass  # v3-era cache backfilled in memory; re-saved below
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
            out = {"cached": True, "clip": clip["name"],
                   "word_count": len(live),
                   "sentence_count": len(preview), "sentences": preview[:8],
                   "removed_words": missing,
                   "note": "preview only — read the full transcript as stable lines with get_story (compact)"}
            nf = sum(1 for w in cached.get("words", []) if w.get("filler"))
            nr = sum(1 for w in cached.get("words", []) if w.get("repeat"))
            if nf or nr:
                out["verbatim_tags"] = {"filler": nf, "repeat": nr}
            return out
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
        _tag_filler_words(mapped)
        _tag_repeat_words(mapped)
        data = {"clip": clip["name"], "media_path": clip["media_path"], "words": mapped,
                "sentences": _sentences(mapped),
                "timeline_duration_s": _clips(rpc).get("duration_s", 0),
                "speakers": sorted({w["speaker"] for w in mapped if w.get("speaker")})}
        _save_cache(key, data)
        out = {"cached": False, "clip": clip["name"], "word_count": len(mapped),
               "sentence_count": len(data["sentences"]),
               "sentences": data["sentences"][:8],
               "note": "first 8 sentences shown; read the full transcript as stable lines with get_story (compact)"}
        nf = sum(1 for w in mapped if w.get("filler"))
        nr = sum(1 for w in mapped if w.get("repeat"))
        if nf or nr:
            out["verbatim_tags"] = {"filler": nf, "repeat": nr}
        return out

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
        extra: dict = {}
        try:
            bad = _trim_health(_all_existing_clips(rpc))
        except BridgeError:
            bad = []
        if bad:
            extra["trim_degenerate"] = bad
            extra["trim_note"] = ("bridge reports trim_start_s=0 for every clip sharing "
                                  "these files — t_start/t_end are best-effort and word "
                                  "cuts are blocked until the bridge is fixed")
        if detail == "words":
            rows = present
            if search:
                s = search.lower()
                rows = [w for w in rows if s in w.get("w", "").lower()]
            rows = [{k: v for k, v in w.items()
                     if v is not None and v is not False}
                    for w in rows[: max(1, limit)]]
            return {"clip": t.get("clip"), "word_count": len(words),
                    "removed_words": missing, "words": rows, **extra}
        if search:
            s = search.lower()
            sentences = [x for x in sentences if s in x["text"].lower()]
        return {"clip": t.get("clip"), "word_count": len(words),
                "removed_words": missing,
                "sentence_count": len(sentences),
                "sentences": sentences[: max(1, limit)], **extra}

    return logged("get_transcript", args, run)


def _word_ranges_to_spans(rpc, ranges: list) -> tuple:
    """Resolve [[start_index, count], ...] (stable FILE indices) to merged
    timeline spans via live clip layout.
    Returns (spans, texts, skipped, refused). Blades land EXACTLY on the
    cached word edges (no padding, no midpoint expansion): Parakeet edges
    carry ~0.2-0.5s error, so intra-sentence ranges can blade mid-phoneme.
    Prefer delete_lines (midpoint-expanded, blades land in silence) for
    anything expressible as whole story lines; reserve this primitive for
    sub-line sweeps. Sub-frame ranges land in
    `refused` — executing them would blade-select the whole host clip
    (2026-09-23: 1ms span removed 6.2s). Refused crumbs are inaudible:
    leave them, or delete their line via delete_lines (midpoint-safe).
    """
    t = _fresh_transcript(rpc)
    cache_words = t.get("words", [])
    if cache_words and "f_start" not in cache_words[0]:
        raise BridgeError("transcript predates file-relative times — re-run transcribe once")
    media = t.get("media_path")
    clips = _all_existing_clips(rpc)
    if media in _trim_health(clips):
        raise BridgeError(
            "trim_start_s is 0 for every clip sharing "
            f"{str(media)[:80]}… (bridge unclippedRange bug) — word spans would "
            "be fiction. Fix the bridge (see docs/bridge.md), then retry."
        )
    spans, texts, skipped, refused = [], [], [], []
    for start_index, count in ranges:
        sel = cache_words[start_index: start_index + count]
        if len(sel) < count:
            raise BridgeError(f"only {len(cache_words) - start_index} words from index {start_index}")
        fa, fb = float(sel[0]["f_start"]), float(sel[-1]["f_end"])
        frags = _resolve_file_interval(clips, media, fa, fb, strict=True)
        if not frags:
            skipped.append({"start_index": start_index, "count": count,
                            "text": " ".join(w["w"] for w in sel),
                            "reason": "already removed"})
            continue
        dur = sum(b - a for a, b in frags)
        if dur < SILENCE_FRAME:
            refused.append({"start_index": start_index, "count": count,
                            "text": " ".join(w["w"] for w in sel),
                            "dur_s": round(dur, 3),
                            "reason": "sub-frame crumb — refused (a blade here deletes "
                                      "the host clip, not the crumb). Inaudible: leave it, "
                                      "or delete its line via delete_lines."})
            continue
        for a, b in frags:
            spans.append((a, b))
        texts.append(" ".join(w["w"] for w in sel))
    return _merge_spans(spans), texts, skipped, refused


def _word_range_edge_notes(rpc, batch: list) -> list:
    """Flag word ranges that split a transcribed sentence mid-speech.

    A range whose file edge sits <1.0s from a neighboring cached word will
    blade inside continuous speech (exact-edge blades, no padding). Purely
    advisory — geometry is unchanged — so agents catch the
    "ok so I- / -really" mistake at dry_run time and reach for
    delete_lines instead.
    """
    try:
        t = _fresh_transcript(rpc)
    except BridgeError:
        return []
    cw = t.get("words", []) or []
    notes = []
    for si, co in batch:
        try:
            si, co = int(si), int(co)
        except (TypeError, ValueError):
            continue
        if si < 0 or co <= 0 or si + co > len(cw):
            continue
        edge = []
        if si > 0:
            try:
                gap = float(cw[si]["f_start"]) - float(cw[si - 1]["f_end"])
                if gap < 1.0:
                    edge.append(
                        f"starts mid-sentence (prev word "
                        f"'{cw[si - 1].get('w', '')}' {gap:.2f}s before — "
                        f"blade lands mid-speech; prefer delete_lines)")
            except (KeyError, TypeError, ValueError):
                pass
        if si + co < len(cw):
            try:
                gap = float(cw[si + co]["f_start"]) - float(cw[si + co - 1]["f_end"])
                if gap < 1.0:
                    edge.append(
                        f"ends mid-sentence (next word "
                        f"'{cw[si + co].get('w', '')}' {gap:.2f}s after — "
                        f"blade lands mid-speech; prefer delete_lines)")
            except (KeyError, TypeError, ValueError):
                pass
        if edge:
            notes.append({"start_index": si, "count": co, "warnings": edge})
    return notes


@mcp.tool()
def delete_words(start_index: int = 0, count: int = 0, dry_run: bool = True,
                 ranges: list[list[int]] | None = None,
                 max_spans: int | None = None) -> dict:
    """Delete words [start_index, start_index+count) and ripple the video.

    LOW-LEVEL range primitive — prefer delete_lines with story ids (see
    get_story): no index math, midpoint-safe drops, crumbs handled. Use
    this only for sub-line sweeps the story layer can't express.
    Blades land EXACTLY on cached word edges (no padding): ranges that
    start/end mid-sentence are flagged in `edge_warnings` — eye-check them,
    or use delete_lines so blades land in silence.
    For retake sweeps pass ranges=[[start, count], ...] — N file-stable
    ranges validated whole and cut right-to-left in one call (one
    re-resolve, one undo depth to revert). Sub-frame crumb ranges are
    REFUSED (never executed — a blade there overcuts the host clip).
    For large sweeps pass max_spans=N: cuts the rightmost N primary clip pieces,
    returns `pending` — re-call with the same ranges (file-stable, safe to
    re-resolve) until `remaining` is 0. Sum each call's `undo_steps` to revert.
    """
    args = {"start_index": start_index, "count": count, "dry_run": dry_run,
            "ranges": ranges, "max_spans": max_spans}

    def run(rpc):
        batch = [tuple(r) for r in ranges] if ranges else [(start_index, count)]
        if not ranges and count <= 0:
            return {"ok": False, "error": "count must be > 0 (or pass ranges=[[start, count], ...])"}
        try:
            spans, texts, skipped, refused = _word_ranges_to_spans(rpc, batch)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        edge_warnings = _word_range_edge_notes(rpc, batch)
        if dry_run or (not spans and refused):
            out = {"dry_run": True, "spans": spans, "texts": texts, "skipped": skipped,
                   "would_remove_s": round(sum(b - a for a, b in spans), 3)}
            if refused:
                out["refused"] = refused
            if edge_warnings:
                out["edge_warnings"] = edge_warnings
            if not ranges and spans:
                # compat: single-range callers expect span/text
                out["span"] = spans[0]
                out["text"] = texts[0] if texts else ""
            return out
        if not spans:
            return {"ok": False, "error": "nothing cuttable (all ranges already removed) — re-read get_story"}
        try:
            report = _cut_spans(rpc, spans, "delete_words", max_spans)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        report["texts"] = texts
        report["skipped"] = skipped
        if refused:
            report["refused"] = refused
        if edge_warnings:
            report["edge_warnings"] = edge_warnings
        return report

    return logged("delete_words", args, run)


@mcp.tool()
def move_words(start_index: int, count: int, dest_index: int, dry_run: bool = True) -> dict:
    """Reorder by words: select the span BY ID, cut, paste at the destination.

    LOW-LEVEL range primitive — prefer move_line with story ids (see
    get_story). Destination is anchor-aware: a dest after the span shifts
    left by the cut length (magnetic close). A dest inside the span is
    refused. Word indices are file-stable; the span is re-resolved live
    so moves chain after cuts.
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
            spans, _, skipped, refused = _word_ranges_to_spans(rpc, [(start_index, count)])
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        if refused:
            return {"ok": False, "error": "selected words are a sub-frame crumb — cannot move inaudible slivers"}
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
        with _WRITE_LOCK:
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


@mcp.tool()
def split_words(at_index: int, dry_run: bool = True) -> dict:
    """Blade the timeline at a word boundary WITHOUT deleting (Enter-to-split).

    at_index is the file-stable word index starting the RIGHT half
    (1..len-1 — a word must remain on each side). The blade lands in the
    inter-word gap (midpoint when the gap exists, else the shared edge),
    never mid-phoneme. One blade, one undo entry. Duration must be
    preserved and the primary clip count must grow by exactly one —
    verified after. A boundary already sitting on an edit point returns
    already_split (idempotent, no write). A boundary whose words were
    already cut away refuses.
    """
    args = {"at_index": at_index, "dry_run": dry_run}

    def run(rpc):
        try:
            at = int(at_index)
        except (TypeError, ValueError):
            return {"ok": False, "error": "at_index must be an int word index"}
        t = _fresh_transcript(rpc)
        cw = t.get("words", [])
        if cw and "f_start" not in cw[0]:
            return {"ok": False, "error": "transcript predates file-relative times — re-run transcribe once"}
        if not (1 <= at < len(cw)):
            return {"ok": False, "error": f"at_index {at} outside splittable range 1..{len(cw) - 1} (need a word on each side)"}
        media = t.get("media_path")
        if media is None:
            return {"ok": False, "error": "transcript has no media_path — re-run transcribe"}
        clips = _all_existing_clips(rpc)
        if media in _trim_health(clips):
            return {"ok": False, "error": ("trim_start_s is 0 for every clip sharing "
                                           f"{str(media)[:80]}… (bridge unclippedRange bug) — refusing to place a blade. "
                                           "Fix the bridge (see docs/bridge.md), then retry.")}
        prev, nxt = cw[at - 1], cw[at]
        try:
            gap_a, gap_b = float(prev["f_end"]), float(nxt["f_start"])
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "transcript words lack file times — re-run transcribe"}
        # Both flanking words must still be live — else the halves are
        # already two slices (or one side is gone entirely).
        for w, side in ((prev, "left"), (nxt, "right")):
            try:
                frags = _resolve_file_interval(clips, media, float(w["f_start"]), float(w["f_end"]),
                                               strict=True)
            except BridgeError as e:
                return {"ok": False, "error": str(e)}
            if sum(b - a for a, b in frags) < SILENCE_FRAME:
                return {"ok": False, "error": f"{side} word '{w.get('w', '')}' is already cut away — nothing to split"}
        f = round((gap_a + gap_b) / 2, 3) if gap_b > gap_a else round(gap_a, 3)
        owner, of0 = None, 0.0
        for c in clips:
            f0, f1 = _clip_file_window(c)
            if f0 <= f <= f1:
                owner, of0 = c, f0
                break
        if owner is None:
            return {"ok": False, "error": "split boundary was already cut away — re-read get_story"}
        tl = round(float(owner["timeline_start_s"]) + (f - of0), 3)
        if _near_point(tl, _timeline_edit_points(clips=clips)):
            return {"ok": True, "already_split": True, "at_t": tl,
                    "between": [prev.get("w", ""), nxt.get("w", "")],
                    "note": "boundary already sits on an edit point — no blade issued"}
        if dry_run:
            return {"dry_run": True, "at_t": tl, "gap_s": round(max(0.0, gap_b - gap_a), 3),
                    "between": [prev.get("w", ""), nxt.get("w", "")]}
        with _WRITE_LOCK:
            before = _clips(rpc)
            dur0 = float(before.get("duration_s", 0))
            n0 = len([c for c in before.get("clips", [])
                      if c.get("lane") in (None, "primary") and not c.get("container")])
            rpc("playback.seek", {"t_s": tl})
            rpc("timeline.action", {"action": "blade"})
            after = _clips(rpc)
            dur1 = float(after.get("duration_s", 0))
            n1 = len([c for c in after.get("clips", [])
                      if c.get("lane") in (None, "primary") and not c.get("container")])
            if abs(dur1 - dur0) < 0.15 and n1 == n0 + 1:
                return {"ok": True, "split": True, "at_t": tl, "undo_steps": 1,
                        "between": [prev.get("w", ""), nxt.get("w", "")],
                        "duration_before_s": round(dur0, 3), "duration_after_s": round(dur1, 3),
                        "verify": "ok"}
            try:
                rpc("timeline.undo")
            except BridgeError:
                pass
            return {"ok": False, "verify": f"FAILED: split did not verify (duration {dur0:.3f}s -> {dur1:.3f}s, "
                    f"primary clips {n0} -> {n1}) — blade reverted",
                    "at_t": tl, "undo_steps": 0}

    return logged("split_words", args, run)


# ---------------------------------------------------------------- story tools


@mcp.tool()
def get_story(search: str | None = None, limit: int = 200, offset: int = 0,
              detail: str = "compact", include_removed: bool = False,
              show_resolved: bool = False) -> dict:
    """Read the transcript as stable story lines (the model's editing UI).

    detail='compact' (default): id + text only (~10 tok/line — no times,
    no word indices). detail='full': adds word ranges, file/live times,
    take_group, removed flags, has_filler/has_repeat (verbatim word tags —
    only present when true). Ids (L0020) are file-anchored and never
    shift after cuts — plan with these, commit with delete_lines /
    move_line / choose_takes. Resolved take groups hide by default.
    Never spawns an engine.
    """
    args = {"search": search, "limit": limit, "offset": offset,
            "detail": detail, "include_removed": include_removed,
            "show_resolved": show_resolved}

    def run(rpc):
        t = _fresh_transcript(rpc)
        frags, missing = _story_live(rpc, t)
        groups = _take_groups(frags)
        open_ids = {g["group"] for g in groups if not g["resolved"]}
        shown_groups = [g for g in groups if show_resolved or not g["resolved"]]
        rows = frags if include_removed else [f for f in frags if not f.get("removed")]
        if search:
            s = search.lower()
            rows = [f for f in rows if s in f["text"].lower()]
        total = len(rows)
        start = max(0, offset)
        rows = rows[start: start + max(1, limit)]
        if detail == "full":
            lines = [{k: v for k, v in f.items() if v is not None and not k.startswith("_")}
                     for f in rows]
        else:
            lines = [{"id": f["id"], "text": f["text"],
                      **({"crumb": True} if _story_crumb(f) else {}),
                      **({"take_group": f["take_group"]}
                         if f.get("take_group") in open_ids else {})}
                     for f in rows]
        out: dict = {"clip": t.get("clip"), "line_count": total,
                     "lines": lines, "removed_lines": missing,
                     "take_groups": shown_groups,
                     "next_offset": start + len(lines) if start + len(lines) < total else None}
        try:
            bad = _trim_health(_all_existing_clips(rpc))
        except BridgeError:
            bad = []
        if bad:
            out["trim_degenerate"] = bad
            out["trim_note"] = ("bridge trim unknown — story positions best-effort, "
                                "cuts blocked until fixed")
        return out

    return logged("get_story", args, run)


def _execute_story_plan(rpc, plan: dict, max_spans: int | None, label: str) -> dict:
    """Execute a validated _story_plan: drops right-to-left, then moves.

    Single undo accounting across both phases. Chunked drops return early
    (moves wait — anchors shift until drops finish). Moves serialize on
    _WRITE_LOCK. Returns the commit report.
    """
    undos = 0
    report: dict = {"planned_clip_pieces": plan.get("clip_pieces", 0)}
    if plan["spans"]:
        try:
            cut = _cut_spans(rpc, plan["spans"], label, max_spans)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        undos += cut.get("undo_steps", 0)
        report.update(cut)
        if not cut.get("ok", True):
            report["moves_pending"] = plan["moves"]
            report["will_remove"] = plan["will_remove"]
            return report
        if cut.get("remaining"):
            report["moves_pending"] = plan["moves"]
            report["will_remove"] = plan["will_remove"]
            if label == "apply_story":
                report["note"] = ("chunked: re-call apply_story with the same keep list "
                                  "until remaining is 0; each call re-plans from live clips")
            else:
                report["note"] = ("chunked: finish drops with cut_spans(pending) "
                                  "until remaining is 0, then re-plan any moves")
            return report
        # Duration alone cannot prove the requested story lines disappeared.
        # A clip can shrink by the right amount while a partially cut line
        # remains. Keep the model's verification in the same terms as its plan.
        wanted_gone = {f["id"] for f in plan["will_remove"]}
        still_live = []
        crumbs_left = []
        if wanted_gone:
            live, _ = _story_live(rpc, _fresh_transcript(rpc))
            for f in live:
                if f["id"] not in wanted_gone or f.get("removed"):
                    continue
                remainder = round(f["t_end"] - f["t_start"], 3)
                item = {"id": f["id"], "text": f["text"], "remaining_s": remainder}
                if remainder < SILENCE_FRAME:
                    crumbs_left.append({**item, "reason": "sub-frame crumb left in place"})
                else:
                    still_live.append(item)
        if still_live:
            report["ok"] = False
            report["verify"] = f"FAILED: {len(still_live)} planned story line(s) still present"
            report["still_present"] = still_live
            report["moves_pending"] = plan["moves"]
            return report
        report["removed_story_lines"] = len(wanted_gone) - len(crumbs_left)
        if crumbs_left:
            report["unremoved"] = report.get("unremoved", []) + crumbs_left
            report["story_verify"] = "ok_with_subframe_crumbs"
    moves_done = []
    dur0, _ = _settle(rpc, tries=2, pause_s=0.2)
    with _WRITE_LOCK:
        for m in plan["moves"]:
            live, _ = _story_live(rpc, _fresh_transcript(rpc))
            by_id = {f["id"]: f for f in live if not f.get("removed")}
            src = by_id.get(m["id"])
            anchor_id = m.get("after") or m.get("before")
            anchor = by_id.get(anchor_id) if anchor_id else None
            if src is None or (anchor_id and anchor is None):
                moves_done.append({**m, "done": False,
                                   "error": "anchor/src cut away mid-batch — re-plan"})
                break
            try:
                span = _move_extent(src)
                anchor_span = _move_extent(anchor) if anchor else None
            except BridgeError as e:
                moves_done.append({**m, "done": False, "error": str(e)})
                break
            if m.get("after"):
                dest_t = anchor_span[1]
            elif m.get("before"):
                dest_t = anchor_span[0]
            else:
                dest_t = 0.0
            if span[0] <= dest_t <= span[1]:
                check = _move_order_check(rpc, m)
                moves_done.append({**m, "done": check["verified"],
                                   "executed": False, "position": check})
                if not check["verified"]:
                    break
                continue
            try:
                _, move_undos = _move_live_span(rpc, span, dest_t)
            except BridgeError as e:
                moves_done.append({**m, "done": False, "error": str(e)})
                break
            undos += move_undos
            check = _move_order_check(rpc, m)
            for _ in range(2):
                if check["verified"]:
                    break
                time.sleep(0.2)
                check = _move_order_check(rpc, m)
            moves_done.append({**m, "done": check["verified"],
                               "executed": True, "position": check})
            if not check["verified"]:
                break
    dur1, settled = _settle(rpc, tries=2, pause_s=0.2)
    tol = 0.15 + 0.05 * max(1, len(moves_done))
    ok = (abs(dur1 - dur0) < tol and len(moves_done) == len(plan["moves"])
          and all(m.get("done") for m in moves_done))
    if ok and plan["moves"]:
        live, _ = _story_live(rpc, _fresh_transcript(rpc))
        active_ids = {f["id"] for f in live if not f.get("removed") and not _story_crumb(f)}
        final_order = [fid for fid in _present_order(live) if fid in active_ids
                       and fid in set(plan["keep"])]
        expected_order = [fid for fid in plan["keep"] if fid in active_ids]
        ok = final_order == expected_order
        if not ok:
            at = next((i for i, (want, got) in enumerate(zip(expected_order, final_order))
                       if want != got), min(len(expected_order), len(final_order)))
            report["order_mismatch"] = {
                "index": at,
                "expected_id": expected_order[at] if at < len(expected_order) else None,
                "actual_id": final_order[at] if at < len(final_order) else None,
                "expected_near": expected_order[max(0, at - 2):at + 3],
                "actual_near": final_order[max(0, at - 2):at + 3]}
    report["moves"] = moves_done
    report["will_remove"] = plan["will_remove"]
    report["crumbs"] = plan["crumbs"]
    if plan.get("unremoved"):
        report["unremoved"] = report.get("unremoved", []) + plan["unremoved"]
    report["undo_steps"] = undos
    report["duration_before_s"] = round(dur0, 3) if not plan["spans"] else report.get("duration_before_s")
    report["duration_after_s"] = round(dur1, 3)
    report["moved_verify"] = ("ok" if ok else
                              f"FAILED: move position or story order did not verify; "
                              f"see moves.position/order_mismatch; duration {dur0:.3f}s -> {dur1:.3f}s")
    if not ok:
        report["verify"] = report["moved_verify"]
    else:
        report["verify"] = "ok"
    report["ok"] = ok
    report.setdefault("remaining", 0)
    report["settled"] = settled and report.get("settled", True)
    return report


@mcp.tool()
def apply_story(keep: list[str], dry_run: bool = True,
                max_spans: int | None = None) -> dict:
    """Rough cut + reorder in one verb: declare what stays and in what order.

    keep=[ids in desired order] (see get_story). Drops resolve at commit
    via midpoint-expanded file intervals (blades land in silence — no
    orphan crumbs); cuts run right-to-left; reorders run as minimal
    anchor moves after the drops. dry_run=True (default) returns
    will_remove as TEXT plus moves — verify by eye, no seconds math.
    Each call cuts at most 40 primary clip pieces by default; max_spans
    overrides that cap. If `remaining` is nonzero, re-call with the same
    keep list: file-anchored drops are re-planned from live clips.
    """
    args = {"keep": keep, "dry_run": dry_run, "max_spans": max_spans}

    def run(rpc):
        plan = _story_plan(rpc, list(keep or []))
        if not plan.get("ok"):
            return plan
        if dry_run:
            plan["dry_run"] = True
            return plan
        if not plan["spans"] and not plan["moves"]:
            return {"ok": True, "already_applied": True, "remaining": 0,
                    "verify": "ok_with_subframe_crumbs" if plan.get("unremoved") else "ok",
                    "undo_steps": 0,
                    **({"unremoved": plan["unremoved"]} if plan.get("unremoved") else {})}
        return _execute_story_plan(rpc, plan, max_spans, "apply_story")

    return logged("apply_story", args, run)


@mcp.tool()
def choose_take(group: str, keep: str, dry_run: bool = True) -> dict:
    """Keep one member of a retake group, drop the rest (see get_story).

    dry_run returns the dropped takes as TEXT. Executes through the same
    midpoint-expanded drop path as apply_story (order preserved).
    """
    args = {"group": group, "keep": keep, "dry_run": dry_run}

    def run(rpc):
        t = _fresh_transcript(rpc)
        frags, _ = _story_live(rpc, t)
        groups = _take_groups(frags)  # all frags: ids stable across resolutions
        g = next((x for x in groups if x["group"] == group), None)
        if g is None:
            return {"ok": False, "error": f"unknown take group {group} — re-read get_story (resolved groups hide by default; pass show_resolved=True)"}
        if g["resolved"]:
            return {"ok": True, "resolved": True, "group": group,
                    "kept_take": g["kept"],
                    "note": "already resolved — nothing to do"}
        if keep not in g["members"]:
            return {"ok": False, "error": f"{keep} not in {group}{g['members']}"}
        present = [f["id"] for f in sorted(
            (f for f in frags if not f.get("removed")), key=lambda f: f["t_start"])]
        keep_ids = [fid for fid in present if fid not in set(g["members"]) or fid == keep]
        plan = _story_plan(rpc, keep_ids)
        if not plan.get("ok"):
            return plan
        plan["group"], plan["kept_take"] = group, keep
        if dry_run or not plan["spans"]:
            plan["dry_run"] = True
            return plan
        report = _execute_story_plan(rpc, plan, None, "choose_take")
        report["group"], report["kept_take"] = group, keep
        return report

    return logged("choose_take", args, run)


# ---------------------------------------------------------------- line verbs
# The size the model thinks at: one line, one move, one batch of takes.
# All three compile to a keep-list and run through _execute_story_plan —
# midpoint-expanded drops, minimal moves, one undo depth.


def _present_order(frags: list) -> list:
    return [f["id"] for f in sorted(
        (f for f in frags if not f.get("removed")), key=lambda f: f["t_start"])]


def _check_line_ids(frags: list, ids: list, what: str = "ids") -> str | None:
    """Validate story ids. Returns an error string or None when clean."""
    known = {f["id"] for f in frags}
    if not ids:
        return f"{what} must be a non-empty list of story ids (see get_story)"
    if len(set(ids)) != len(ids):
        dupes = sorted({k for k in ids if ids.count(k) > 1})
        return f"duplicate ids: {dupe_str(dupes)}"
    unknown = [k for k in ids if k not in known]
    if unknown:
        return f"unknown ids (re-read get_story): {unknown[:8]}"
    by_id = {f["id"]: f for f in frags}
    gone = [k for k in ids if by_id[k].get("removed")]
    if gone:
        return f"already-removed ids (re-read get_story): {gone[:8]}"
    return None


@mcp.tool()
def delete_lines(ids: list[str], dry_run: bool = True,
                 max_spans: int | None = None) -> dict:
    """Delete story lines by id (see get_story) and ripple the video.

    The verb the model reaches for: no word math, no keep-list of 200
    ids. Drops resolve via midpoint-expanded file intervals; sub-frame
    crumbs are left in place and reported (not overcut). dry_run returns
    the doomed lines as TEXT — verify by eye.
    """
    args = {"ids": ids, "dry_run": dry_run, "max_spans": max_spans}

    def run(rpc):
        ids_l = list(ids or [])
        frags, _ = _story_live(rpc, _fresh_transcript(rpc))
        err = _check_line_ids(frags, ids_l)
        if err:
            return {"ok": False, "error": err}
        present = _present_order(frags)
        keep = [fid for fid in present if fid not in set(ids_l)]
        if not keep:
            return {"ok": False, "error": "refusing to delete every line — keep at least one"}
        plan = _story_plan(rpc, keep)
        if not plan.get("ok"):
            return plan
        if dry_run:
            return {"dry_run": True, "deleted": plan["will_remove"],
                    "would_remove_s": plan["would_remove_s"],
                    "duration_after_s": plan["duration_after_s"],
                    **({"unremoved": plan["unremoved"]} if plan.get("unremoved") else {})}
        report = _execute_story_plan(rpc, plan, max_spans, "delete_lines")
        report["deleted"] = plan["will_remove"]
        return report

    return logged("delete_lines", args, run)


@mcp.tool()
def move_line(id: str, after_id: str | None = None,
              before_id: str | None = None, dry_run: bool = True) -> dict:
    """Move one story line after/before an anchor line (see get_story).

    Exactly one of after_id / before_id. Each line's movable extent uses
    its one frame-sized live span; remote source crumbs cannot stretch it.
    Discontinuous lines refuse. The final neighbor and duration are checked.
    """
    args = {"id": id, "after_id": after_id, "before_id": before_id, "dry_run": dry_run}

    def run(rpc):
        if (after_id is None) == (before_id is None):
            return {"ok": False, "error": "exactly one of after_id / before_id"}
        frags, _ = _story_live(rpc, _fresh_transcript(rpc))
        err = _check_line_ids(frags, [id, after_id or before_id], what="id/anchor")
        if err:
            return {"ok": False, "error": err}
        if id == (after_id or before_id):
            return {"ok": False, "error": "cannot move a line relative to itself"}
        present = _present_order(frags)
        rest = [fid for fid in present if fid != id]
        anchor = after_id or before_id
        ins = rest.index(anchor) + (1 if after_id else 0)
        keep = rest[:ins] + [id] + rest[ins:]
        plan = _story_plan(rpc, keep)
        if not plan.get("ok"):
            return plan
        by_id = {f["id"]: f for f in frags}
        if dry_run:
            return {"dry_run": True,
                    "move": {"id": id, "text": by_id[id]["text"],
                             **({"after": after_id} if after_id else {"before": before_id})},
                    "plan_moves": plan["moves"]}
        report = _execute_story_plan(rpc, plan, None, "move_line")
        report["moved_line"] = id
        return report

    return logged("move_line", args, run)


def _normalize_choices(choices) -> list:
    """Validate the single public shape: {group_id: keep_line_id}."""
    if not isinstance(choices, dict):
        raise BridgeError("choices must be an object {group_id: keep_line_id}")
    if not choices:
        raise BridgeError("choices is empty — nothing to resolve")
    if not all(isinstance(g, str) and isinstance(k, str) for g, k in choices.items()):
        raise BridgeError("choices keys and values must be story id strings")
    return list(choices.items())


def _validate_takes_choices(frags: list, items: list) -> tuple:
    """Validate a whole choose_takes batch BEFORE any write.

    Returns (keep_ids, per_group, noop_groups). Raises BridgeError on the
    first problem — the caller executes nothing (no half-resolved timeline).
    """
    groups = {g["group"]: g for g in _take_groups(frags)}
    by_id = {f["id"]: f for f in frags}
    noop, losers = [], set()
    per_group: dict = {}
    for group, keep in items:
        g = groups.get(group)
        if g is None:
            raise BridgeError(f"unknown group {group} — re-read get_story "
                              "(resolved groups hide by default; pass show_resolved=True). Nothing executed.")
        if g["resolved"]:
            if keep == g["kept"]:
                noop.append(group)
                per_group[group] = {"keep": keep, "drop": [], "noop": True}
                continue
            raise BridgeError(f"{group} already resolved, kept {g['kept']} — undo to revisit. Nothing executed.")
        if keep not in g["members"]:
            raise BridgeError(f"{keep} not in {group}{g['members']}. Nothing executed.")
        if by_id[keep].get("removed"):
            raise BridgeError(f"keep {keep} already removed — re-read get_story. Nothing executed.")
        drop = [m for m in g["active"] if m != keep]
        if set(drop) & losers:
            raise BridgeError(f"overlapping drops into {group} — refusing. Nothing executed.")
        losers.update(drop)
        per_group[group] = {"keep": keep, "drop": [
            {"id": m, "text": by_id[m]["text"]} for m in drop]}
    keep_ids = [fid for fid in _present_order(frags) if fid not in losers]
    if not keep_ids:
        raise BridgeError("refusing to delete every line — keep at least one")
    return keep_ids, per_group, noop


@mcp.tool()
def choose_takes(choices: dict[str, str], dry_run: bool = True,
                 max_spans: int | None = None) -> dict:
    """Resolve many retake groups in ONE validated batch (see get_story).

    choices = {group: keep_id}. Every group
    and keep is validated BEFORE any write — one bad entry refuses the
    whole batch, never a half-resolved timeline. One plan, one undo depth.
    """
    args = {"choices": choices, "dry_run": dry_run, "max_spans": max_spans}

    def run(rpc):
        try:
            items = _normalize_choices(choices)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        frags, _ = _story_live(rpc, _fresh_transcript(rpc))
        try:
            keep_ids, per_group, noop = _validate_takes_choices(frags, items)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        plan = _story_plan(rpc, keep_ids)
        if not plan.get("ok"):
            return plan
        plan["groups"] = per_group
        plan["noop_groups"] = noop
        if dry_run or not plan["spans"]:
            plan["dry_run"] = True
            return plan
        report = _execute_story_plan(rpc, plan, max_spans, "choose_takes")
        report["groups"] = per_group
        return report

    return logged("choose_takes", args, run)


def _duration_walk(max_lines: int = 2000) -> dict:
    """Session duration walk from the call log (best effort)."""
    try:
        from collections import deque
        with open(LOG_PATH) as f:
            lines = deque(f, max_lines)
    except OSError:
        return {}
    first_before = last_after = None
    for ln in lines:
        try:
            res = (json.loads(ln).get("result") or {})
        except (json.JSONDecodeError, AttributeError):
            continue
        if first_before is None and res.get("duration_before_s") is not None:
            first_before = res["duration_before_s"]
        if res.get("duration_after_s") is not None:
            last_after = res["duration_after_s"]
    if first_before is None or last_after is None:
        return {}
    return {"from_s": first_before, "to_s": last_after,
            "delta_s": round(last_after - first_before, 3)}


def _review_state(rpc) -> dict:
    """Checkable definition of done: open takes, crumbs, duration walk."""
    t = _fresh_transcript(rpc)
    frags, missing = _story_live(rpc, t)
    groups = _take_groups(frags)
    open_g = [g for g in groups if not g["resolved"]]
    by_id = {f["id"]: f for f in frags}
    crumbs = [{"id": f["id"], "text": f["text"],
               "dur_s": round(f["t_end"] - f["t_start"], 3),
               "subframe": _story_crumb(f)}
              for f in frags if not f.get("removed")
              and (f["t_end"] - f["t_start"]) < CRUMB_WARN_S]
    dur = _clips(rpc).get("duration_s", 0)
    open_items = []
    if open_g:
        open_items.append(f"{len(open_g)} take group(s) unresolved — resolve with choose_takes, then re-review")
    notes = []
    if crumbs:
        tiny = sum(1 for c in crumbs if c["subframe"])
        if tiny:
            notes.append(f"{tiny} sub-frame remnant(s) uncuttable")
        if len(crumbs) > tiny:
            notes.append(f"{len(crumbs) - tiny} short fragment(s) under "
                         f"{CRUMB_WARN_S}s may be audible — audition if they matter")
    if missing:
        notes.append(f"{missing} line(s) already removed earlier in the session")
    return {"clip": t.get("clip"), "duration_s": round(dur, 3),
            "active_lines": sum(1 for f in frags if not f.get("removed")),
            "removed_lines": missing,
            "duration_walk": _duration_walk(),
            "open_takes": [{"group": g["group"], "members": [
                {"id": m, "text": by_id[m]["text"]} for m in g["active"]]} for g in open_g],
            "open_take_count": len(open_g),
            "resolved_take_count": sum(1 for g in groups if g["resolved"]),
            "crumbs": crumbs, "subframe_crumb_count": sum(c["subframe"] for c in crumbs),
            "short_fragment_count": sum(not c["subframe"] for c in crumbs),
            "open_items": open_items, "notes": notes,
            "ready": not open_items,
            "gate": ("DONE — nothing open" if not open_items else
                     "NOT DONE — do not finish until open_items is empty")}


@mcp.tool()
def review() -> dict:
    """Story-decision gate: open takes, short fragments, duration walk.

    `ready` means no unresolved take groups. Short fragments remain listed
    for an optional listening check; this is not an audio quality verdict.
    Reads only; never writes, never spawns an engine.
    """
    return logged("review", {}, _review_state)


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
        with _WRITE_LOCK:
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


def _verify_summary(rpc) -> dict:
    """Slim verify: durations + counts. Full clip dumps cost ~58KB a read
    (2026-09-23 postmortem: the thinking-time sink) — detail='full' opts in."""
    state = _clips(rpc)
    pos = rpc("playback.position")
    return {"duration_s": round(state.get("duration_s", 0), 3),
            "clip_count": len(state.get("clips", [])),
            "playhead_s": round(pos.get("t_s", 0), 3),
            "fps": pos.get("fps", state.get("fps"))}


@mcp.tool()
def verify_action(note: str = "", detail: str = "summary") -> dict:
    """Re-read timeline + playhead. Call after any batch.

    detail='summary' (default): durations + counts only. detail='full':
    full clip dump + position (expensive — ~58KB, debugging only).
    """
    args = {"note": note, "detail": detail}

    def run(rpc):
        if detail == "full":
            return {"state": _clips(rpc), "position": rpc("playback.position")}
        return _verify_summary(rpc)

    return logged("verify_action", args, run)


@mcp.tool()
def undo(steps: int = 1) -> dict:
    """Undo `steps` entries via FCP's undo manager.

    A successful cut piece normally costs up to 3 entries (2 blades +
    1 delete). `undo_steps` counts issued actions; after a failed action,
    FCP may have created fewer undo entries. Inspect the live timeline
    and undo history before bulk undo of a failed call.
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
