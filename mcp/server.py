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
__version__ = "0.4.0"
# Transcript-cache schema. Bumped on any words/cache layout change; the key
# AND the payload both carry it, so an upgrade never dead-ends on a stale
# cache hit ("safe to reuse but not when our schema changes").
SCHEMA_VERSION = 3
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


def _cut_spans(rpc, spans: list, label: str, max_spans: int | None = None) -> dict:
    """Delete each [a, b] (timeline seconds), right-to-left so offsets hold.

    Per span: blade at both ends (skipped at existing edit points),
    re-snapshot, select the fresh segment BY ID (identity, not playhead
    luck), delete. A select miss reverts the blades — a wrong cut is
    impossible by construction.
    Returns report with the TRUE undo depth (blades actually issued +
    deletes), so `undo(steps=report["undo_steps"])` restores one call.

    max_spans (chunking): cut only the RIGHTMOST `max_spans` spans and
    report the rest as `pending` (still valid timeline seconds — cutting
    right-to-left never shifts earlier spans, and file-anchored planners
    re-resolve anyway). Loop until `remaining` is 0. This keeps each call
    under the MCP client timeout: ~0.8s/span means 169 spans ≈ 140s in one
    call (server finishes, session drops), but 20 spans ≈ 16s per call.
    """
    spans = _merge_spans([(float(a), float(b)) for a, b in spans])
    spans = [(a, b) for a, b in spans if b > a]
    if not spans:
        return {"removed": 0, "removed_s": 0.0, "undo_steps": 0}
    desc = sorted(spans, reverse=True)
    pending: list = []
    if max_spans is not None and len(desc) > max_spans:
        desc, pending = desc[:max_spans], desc[max_spans:]
    before = _clips(rpc)
    dur_before = before.get("duration_s", 0)
    total = dur_before
    for a, b in desc:
        if not (0 <= a < b <= total + 0.05):
            raise BridgeError(f"span [{a}, {b}] outside timeline (0, {total:.3f}) — re-plan")
    removed_s = 0.0
    steps = 0
    undos = 0
    for a, b in desc:
        _, blades = _isolate_span(rpc, a, b)
        rpc("timeline.action", {"action": "delete"})
        removed_s += b - a
        steps += 1
        undos += blades + 1
    after = _clips(rpc)
    dur_after = after.get("duration_s", 0)
    expected = dur_before - removed_s
    # Frame-snap scales with span count: each blade lands on a frame
    # boundary, so a fixed 4-frame window false-MISMATCHes on big sweeps.
    tol = 0.15 + 0.05 * steps
    ok = abs(dur_after - expected) < tol
    report = {
        "removed": steps,
        "removed_s": round(removed_s, 3),
        "duration_before_s": round(dur_before, 3),
        "duration_after_s": round(dur_after, 3),
        "undo_steps": undos,
        "verify": "ok" if ok else f"MISMATCH expected={expected:.3f}s actual={dur_after:.3f}s — undo(steps={undos}) to revert",
    }
    if pending:
        report["remaining"] = len(pending)
        report["pending"] = pending  # rightmost-first order; cut these next
        report["note"] = (f"chunked: cut {steps}, {len(pending)} pending — "
                          "re-call with the same args until remaining is 0")
    else:
        report["remaining"] = 0
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
def get_timeline() -> dict:
    """Timeline clips: name, lane, timeline span, trim offset, source file path."""
    return logged("get_timeline", {}, _clips)


@mcp.tool()
def get_playhead() -> dict:
    """Playhead time, fps, sequence duration."""
    return logged("get_playhead", {}, lambda rpc: rpc("playback.position"))


# ---------------------------------------------------------------- silences

SILENCE_FRAME = 1 / 30  # sub-frame slivers are uncuttable; drop them


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
        entry = {"file": path, "clips": len(fclips), "hits": mapped}
        if for_cut and guarded_hits:
            entry["word_guarded"] = guarded_hits
        per_file.append(entry)
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
    threshold_db: float = -34.0, min_duration_s: float = 0.5, pad_s: float = 0.2
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
) -> dict:
    """Cut every silence (pad kept each side so speech breathes).

    dry_run=True (default) returns the cut list without writing. Speech from
    the cached transcript (expanded by word_guard_s) is subtracted from each
    silence before cutting, so a hot threshold can't eat word onsets.
    For large cut lists pass max_spans=N (e.g. 20): cuts the rightmost N,
    returns `pending` — re-call with the same args (file-anchored spans
    re-resolve, so chunking is safe) until `remaining` is 0. Sum each call's
    `undo_steps` for a full revert. Each chunk is logged: `make logs` shows
    progress live.
    """

    args = {"threshold_db": threshold_db, "min_duration_s": min_duration_s,
            "pad_s": pad_s, "dry_run": dry_run, "max_spans": max_spans,
            "word_guard_s": word_guard_s}

    def run(rpc):
        try:
            spans, per_file = _silence_scan(rpc, threshold_db, min_duration_s, True,
                                            pad_s, word_guard_s)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        warn = _silence_warning(spans, rpc)
        if dry_run or not spans:
            return {"dry_run": dry_run, "files": per_file, "spans": spans, **warn}
        try:
            report = _cut_spans(rpc, spans, "remove_silences", max_spans)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        report["files"] = per_file
        if warn["warning"]:
            report["warning"] = warn["warning"]
        return report

    return logged("remove_silences", args, run)


@mcp.tool()
def cut_spans(spans: list, dry_run: bool = True, max_spans: int | None = None) -> dict:
    """Cut explicit timeline-second spans [[a, b], ...], right-to-left.

    The chunk executor: validate whole, cut at most `max_spans` (rightmost
    first), report `pending` for the next call. Loop until `remaining` is 0,
    then sum each call's `undo_steps` for a full revert. Every call is
    logged, so `make logs` shows progress live.
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
def apply_cut_list(keep_ranges: list, dry_run: bool = True,
                   max_spans: int | None = None) -> dict:
    """Rough cut in one verb: keep [[start_s, end_s]...], drop the rest, close gaps.

    Ranges validated whole (sorted, non-overlapping, inside the timeline) before
    any write. Cuts run right-to-left so offsets hold; duration verified after.
    For large drops pass max_spans=N (e.g. 20): cuts the rightmost N drops,
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
            out.append({
                "id": f"L{cur[0]['i']:04d}",
                "start_word": cur[0]["i"],
                "end_word": cur[-1]["i"],
                "text": " ".join(w["w"] for w in cur),
                "f_start": float(cur[0]["f_start"]),
                "f_end": float(cur[-1]["f_end"]),
            })
            cur.clear()

    for w in cache_words:
        cur.append(w)
        if re.search(r"[.?!…]['\"]?$", w.get("w", "")) or len(cur) >= max_words:
            flush()
    flush()
    return out


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", t.lower())).strip()


def _take_groups(frags: list, window: int = 10, ratio: float = 0.6) -> list:
    """Group near-duplicate fragments (retakes) within a sliding window.

    Union-find over normalized SequenceMatcher ratio. Group id derives
    from the smallest member id, so it is stable across reads.
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

    norms = [_norm_text(f["text"]) for f in frags]
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
            groups.append({"group": f"G{min(ids)}", "members": ids,
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
    missing = 0
    for f in frags:
        hits = _resolve_file_interval(clips, media, f["f_start"], f["f_end"])
        if not hits:
            missing += 1
            f["t_start"], f["t_end"], f["removed"] = -1, -1, True
        else:
            f["t_start"], f["t_end"] = hits[0][0], hits[-1][1]
    for g in _take_groups([f for f in frags if not f.get("removed")]):
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
    if media in _trim_health(_all_existing_clips(rpc)):
        return {"ok": False, "error": "trim layout degenerate — word cuts blocked until the bridge is fixed"}
    clips = _all_existing_clips(rpc)
    for f in drop_frags:
        fa, fb = _expand_to_midpoints(f, by_index, cache_words)
        for a, b in _resolve_file_interval(clips, media, fa, fb, strict=True):
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
    dur = _clips(rpc).get("duration_s", 0)
    remove_s = round(sum(b - a for a, b in spans), 3)
    return {"ok": True, "keep": keep_ids, "kept": len(keep_ids),
            "will_remove": [{"id": f["id"], "text": f["text"]} for f in drop_frags],
            "drops": len(drop_frags), "spans": spans,
            "would_remove_s": remove_s,
            "duration_before_s": round(dur, 3),
            "duration_after_s": round(dur - remove_s, 3),
            "moves": moves, "crumbs": crumbs}


def dupe_str(dupes: list) -> str:
    return ", ".join(dupes[:8])


def _move_live_span(rpc, span: tuple, dest_t: float) -> float:
    """Cut one live span and paste at dest_t (anchor re-resolved by caller)."""
    span_len = span[1] - span[0]
    _isolate_span(rpc, span[0], span[1])
    rpc("timeline.action", {"action": "cut"})
    dest_adj = dest_t - span_len if dest_t > span[1] else dest_t
    rpc("playback.seek", {"t_s": max(0, dest_adj)})
    rpc("timeline.action", {"action": "paste"})
    return dest_adj


def _load_cache(key: str) -> dict | None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(p):
        with open(p) as f:
            data = json.load(f)
        if data.get("schema") != SCHEMA_VERSION:
            return None  # stale schema: cache MISS (forces re-transcribe, never dead-ends)
        return data
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
            rows = [{k: v for k, v in w.items() if v is not None}
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
    timeline spans via live clip layout. Returns (spans, texts, skipped)."""
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
    spans, texts, skipped = [], [], []
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
        for a, b in frags:
            spans.append((a, b))
        texts.append(" ".join(w["w"] for w in sel))
    return _merge_spans(spans), texts, skipped


@mcp.tool()
def delete_words(start_index: int = 0, count: int = 0, dry_run: bool = True,
                 ranges: list | None = None, max_spans: int | None = None) -> dict:
    """Delete words [start_index, start_index+count) and ripple the video.

    Plan on sentences (get_transcript default): a sentence's start_word /
    end_word IS the range to pass here. For retake sweeps pass
    ranges=[[start, count], ...] — N file-stable ranges validated whole and
    cut right-to-left in one call (one re-resolve, one undo depth to revert).
    For large sweeps pass max_spans=N (e.g. 20): cuts the rightmost N spans,
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
        try:
            report = _cut_spans(rpc, spans, "delete_words", max_spans)
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
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


# ---------------------------------------------------------------- story tools


@mcp.tool()
def get_story(search: str | None = None, limit: int = 200, offset: int = 0,
              detail: str = "compact", include_removed: bool = False) -> dict:
    """Read the transcript as stable story lines (the model's editing UI).

    detail='compact' (default): id + text only (~10 tok/line — no times,
    no word indices). detail='full': adds word ranges, file/live times,
    take_group, removed flags. Ids (L0020) are file-anchored and never
    shift after cuts — plan with these, commit with apply_story.
    Never spawns an engine.
    """
    args = {"search": search, "limit": limit, "offset": offset,
            "detail": detail, "include_removed": include_removed}

    def run(rpc):
        t = _fresh_transcript(rpc)
        frags, missing = _story_live(rpc, t)
        groups = _take_groups([f for f in frags if not f.get("removed")])
        rows = frags if include_removed else [f for f in frags if not f.get("removed")]
        if search:
            s = search.lower()
            rows = [f for f in rows if s in f["text"].lower()]
        total = len(rows)
        rows = rows[max(0, offset): max(0, offset) + max(1, limit)]
        if detail == "full":
            lines = [{k: v for k, v in f.items() if v is not None} for f in rows]
        else:
            lines = [{"id": f["id"], "text": f["text"],
                      **({"take_group": f["take_group"]} if f.get("take_group") else {})}
                     for f in rows]
        out: dict = {"clip": t.get("clip"), "line_count": total,
                     "lines": lines, "removed_lines": missing,
                     "take_groups": groups}
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


@mcp.tool()
def apply_story(keep: list, dry_run: bool = True,
                max_spans: int | None = None) -> dict:
    """Rough cut + reorder in one verb: declare what stays and in what order.

    keep=[ids in desired order] (see get_story). Drops resolve at commit
    via midpoint-expanded file intervals (blades land in silence — no
    orphan crumbs); cuts run right-to-left; reorders run as minimal
    anchor moves after the drops. dry_run=True (default) returns
    will_remove as TEXT plus moves — verify by eye, no seconds math.
    For large drops pass max_spans=N: cuts the rightmost N drop spans,
    returns `pending` — finish with cut_spans(pending) until remaining=0.
    """
    args = {"keep": keep, "dry_run": dry_run, "max_spans": max_spans}

    def run(rpc):
        plan = _story_plan(rpc, list(keep or []))
        if not plan.get("ok"):
            return plan
        if dry_run or (not plan["spans"] and not plan["moves"]):
            plan["dry_run"] = True
            return plan
        undos = 0
        report: dict = {}
        if plan["spans"]:
            try:
                cut = _cut_spans(rpc, plan["spans"], "apply_story", max_spans)
            except BridgeError as e:
                return {"ok": False, "error": str(e)}
            undos += cut.get("undo_steps", 0)
            report.update(cut)
            if cut.get("remaining"):
                # Drops chunked: moves wait until drops finish (anchors shift).
                report["moves_pending"] = plan["moves"]
                report["will_remove"] = plan["will_remove"]
                report["note"] = ("chunked: finish drops with cut_spans(pending) "
                                  "until remaining is 0, then re-call apply_story "
                                  "with the same keep for moves")
                return report
        moves_done = []
        dur0 = _clips(rpc).get("duration_s", 0)
        for m in plan["moves"]:
            live, _ = _story_live(rpc, _fresh_transcript(rpc))
            by_id = {f["id"]: f for f in live if not f.get("removed")}
            src = by_id.get(m["id"])
            anchor_id = m.get("after") or m.get("before")
            anchor = by_id.get(anchor_id) if anchor_id else None
            if src is None or (anchor_id and anchor is None):
                moves_done.append({**m, "error": "anchor/src cut away mid-batch — re-plan"})
                continue
            span = (src["t_start"], src["t_end"])
            if m.get("after"):
                dest_t = anchor["t_end"]
            elif m.get("before"):
                dest_t = anchor["t_start"]
            else:
                dest_t = 0.0
            if span[0] <= dest_t <= span[1]:
                continue  # already in place after prior moves
            _move_live_span(rpc, span, dest_t)
            undos += 2  # cut + paste
            moves_done.append({**m, "done": True})
        after = _clips(rpc)
        dur1 = after.get("duration_s", 0)
        tol = 0.15 + 0.05 * max(1, len(moves_done))
        ok = abs(dur1 - dur0) < tol  # moves preserve duration
        report["moves"] = moves_done
        report["will_remove"] = plan["will_remove"]
        report["crumbs"] = plan["crumbs"]
        report["undo_steps"] = undos + report.get("undo_steps", 0)
        report["moved_verify"] = ("ok" if ok else
                                  f"MISMATCH duration {dur0:.3f}s -> {dur1:.3f}s — undo(steps={report['undo_steps']}) to revert")
        return report

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
        groups = _take_groups([f for f in frags if not f.get("removed")])
        g = next((x for x in groups if x["group"] == group), None)
        if g is None:
            return {"ok": False, "error": f"unknown take group {group} (re-read get_story)"}
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
        try:
            cut = _cut_spans(rpc, plan["spans"], "choose_take")
        except BridgeError as e:
            return {"ok": False, "error": str(e)}
        cut["group"], cut["kept_take"] = group, keep
        cut["will_remove"] = plan["will_remove"]
        return cut

    return logged("choose_take", args, run)


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
