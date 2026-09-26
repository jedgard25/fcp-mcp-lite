"""Guards added for the 2026-09-22 silence-cut postmortem. No FCP needed."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
import server as S
from server import BridgeError


def clip(cid, start, dur, trim=0, media="/tmp/x.mp4"):
    return {"id": cid, "timeline_start_s": start, "duration_s": dur,
            "trim_start_s": trim, "media_path": media, "name": cid}


def state(dur, clips):
    return {"duration_s": dur, "clips": clips}


class FakeRpc:
    def __init__(self, clip_responses):
        self.q, self.calls = list(clip_responses), []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        if method == "timeline.clips":
            return self.q.pop(0)
        return {"status": "ok"}


# 1) degenerate trims detected: >1 clip, same file, all trim 0
deg = [clip("a", 0, 10, 0), clip("b", 10, 10, 0)]
assert S._trim_health(deg) == ["/tmp/x.mp4"], S._trim_health(deg)
print("1 DEGENERATE detected ok")

# 2) single clip trim 0 is healthy (not blocked)
assert S._trim_health([clip("a", 0, 10, 0)]) == []
print("2 SINGLE-CLIP healthy ok")

# 3) proper trims healthy
good = [clip("a", 0, 10, 0), clip("b", 10, 10, 10)]
assert S._trim_health(good) == []
print("3 PROPER-TRIM healthy ok")

# 4) strict resolve raises on degenerate (blocks wrong cuts)
try:
    S._resolve_file_interval(deg, "/tmp/x.mp4", 5.0, 6.0, strict=True)
    raise SystemExit("4 STRICT: NO ERROR (bad)")
except BridgeError as e:
    assert "trim_start_s" in str(e), e
    print("4 STRICT blocks ok |", str(e)[:60])

# 5) non-strict resolve still reads (best effort, flagged by callers)
assert S._resolve_file_interval(deg, "/tmp/x.mp4", 5.0, 6.0) != []
print("5 LENIENT reads ok")

# 6) word ranges refuse on degenerate layout (delete_words would be fiction)
S._last_transcript = lambda: {
    "clip": "c", "media_path": "/tmp/x.mp4",
    "words": [{"i": 0, "w": "hello", "f_start": 11.0, "f_end": 11.5}],
    "timeline_duration_s": 20.0}
import os as _os
_REAL = _os.path.abspath(__file__)
deg_real = [dict(c, media_path=_REAL) for c in deg]
S._last_transcript = lambda: {
    "clip": "c", "media_path": _REAL,
    "words": [{"i": 0, "w": "hello", "f_start": 11.0, "f_end": 11.5}],
    "timeline_duration_s": 20.0}
try:
    S._word_ranges_to_spans(FakeRpc([state(20.0, deg_real)]), [(0, 1)])
    raise SystemExit("6 WORDS: NO ERROR (bad)")
except BridgeError as e:
    assert "trim_start_s" in str(e), e
    print("6 WORDS blocked ok")

# 7) word-guard subtraction: silence minus speech
assert S._subtract_intervals(5.0, 9.0, [(4.0, 6.0), (8.5, 9.5)]) == [(6.0, 8.5)]
assert S._subtract_intervals(5.0, 9.0, []) == [(5.0, 9.0)]
assert S._subtract_intervals(5.0, 9.0, [(0.0, 99.0)]) == []
assert S._subtract_intervals(5.0, 9.0, [(5.0, 6.0), (7.0, 8.0)]) == [(6.0, 7.0), (8.0, 9.0)]
print("7 SUBTRACT ok")

# 8) chunking: max_spans cuts rightmost first, reports pending
calls = []
base = [clip("c1", 0, 10), clip("c2", 10, 10), clip("c3", 20, 10)]
two = [clip("c1", 0, 10), clip("c2", 10, 10)]
one = [clip("c1", 0, 10)]
# Three whole-clip spans, two cuts in this chunk, each ripple verified.
seq = [state(30.0, base),
       state(30.0, base), state(30.0, base), state(20.0, two),
       state(20.0, two), state(20.0, two), state(10.0, one)]
rpc = FakeRpc(seq)
rep = S._cut_spans(rpc, [(0.0, 10.0), (10.0, 20.0), (20.0, 30.0)], "t", max_spans=2)
assert rep["removed"] == 2 and rep["remaining"] == 1, rep
assert rep["pending"] == [(0.0, 10.0)], rep
print("8 CHUNK ok | removed 2, pending:", rep["pending"])

# 9) stale-schema cache is a MISS, never a dead-end
# NOTE: redirect BOTH cache and state into tempdirs — _save_cache writes
# STATE_PATH (last.json) as a side effect, and pointing it at the real
# file once clobbered a live transcript pointer. Never touch real state.
import tempfile, json as _json
_tmp = tempfile.mkdtemp()
S.CACHE_DIR = _tmp
S.STATE_PATH = os.path.join(_tmp, "last.json")
S._save_cache("k1", {"words": [{"i": 0}]})
assert _json.load(open(os.path.join(S.CACHE_DIR, "k1.json")))["schema"] == S.SCHEMA_VERSION
# hand-write a legacy payload (no schema) under a known key
with open(os.path.join(S.CACHE_DIR, "old.json"), "w") as f:
    _json.dump({"words": [{"i": 0, "w": "x"}]}, f)
assert S._load_cache("old") is None
print("9 SCHEMA ok | legacy cache misses, fresh transcribe forced")

# 10) trim_candidates consensus unblocks; disagreement blocks
c1 = dict(clip("a", 0, 10, 0), trim_candidates={"unclippedRange": 0.0, "sourceRange": 0.0})
c2 = dict(clip("b", 10, 10, 0), trim_candidates={"unclippedRange": 0.0, "sourceRange": 10.0})
assert S._effective_trim(c1) == 0.0 and S._effective_trim(c2) == 10.0
assert S._trim_health([c1, c2]) == [], S._trim_health([c1, c2])
assert S._resolve_file_interval([c1, c2], "/tmp/x.mp4", 11.0, 12.0, strict=True) == [(11.0, 12.0)]
print("10 CONSENSUS ok | true trims unblock cuts")
d1 = dict(clip("a", 0, 10, 0), trim_candidates={"sourceRange": 5.0, "mediaRange": 7.0})
d2 = dict(clip("b", 10, 10, 0), trim_candidates={"sourceRange": 10.0})
assert S._effective_trim(d1) is None
assert S._trim_health([d1, d2]) == ["/tmp/x.mp4"]
try:
    S._resolve_file_interval([d1, d2], "/tmp/x.mp4", 5.0, 6.0, strict=True)
    raise SystemExit("10 DISAGREE: NO ERROR (bad)")
except BridgeError:
    print("10 DISAGREE blocks ok")

print("ALL GREEN")
