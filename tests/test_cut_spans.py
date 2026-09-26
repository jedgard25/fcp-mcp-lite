"""Stubbed tests for the cut/move core. No FCP needed: FakeRpc scripts
timeline.clips snapshots; every other verb returns ok unless told otherwise."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
from server import _cut_spans, _isolate_span, _resolve_id, _fresh_transcript, BridgeError


def clip(cid, start, dur, name=None):
    return {"id": cid, "timeline_start_s": start, "duration_s": dur,
            "name": name or cid, "selected": False}


def state(dur, clips):
    return {"duration_s": dur, "clips": clips}


class FakeRpc:
    def __init__(self, clip_responses, fail_select=False):
        self.q, self.calls, self.fail_select = list(clip_responses), [], fail_select

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        if method == "timeline.clips":
            return self.q.pop(0)
        if method == "timeline.select" and self.fail_select:
            raise BridgeError("selection landed elsewhere — deselected")
        return {"status": "ok"}

    def undos(self):
        return [c for c in self.calls if c[0] == "timeline.undo"]


def seg_state():
    # after blading [25,26] out of c2 [20,30]: fresh sliver c2b + rest
    return state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 10),
                        clip("clip_3", 20, 5), clip("clip_2b", 25, 1),
                        clip("clip_4", 26, 4), clip("clip_5", 30, 10)])


# 1) HIT: select by ID, delete, duration verifies
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20),
                             clip("clip_3", 30, 10)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20),
                             clip("clip_3", 30, 10)]),
               seg_state(),
               state(39.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 15),
                             clip("clip_4", 25, 4), clip("clip_5", 29, 10)]),
               state(39.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 15),
                             clip("clip_4", 25, 4), clip("clip_5", 29, 10)])])
rep = _cut_spans(rpc, [(25.0, 26.0)], "t")
selects = [c for c in rpc.calls if c[0] == "timeline.select"]
assert rep["verify"] == "ok" and rep["removed"] == 1, rep
assert rep["settled"] is True, rep
assert rep["undo_steps"] == 3, rep  # 2 blades (mid-timeline) + 1 delete
assert selects and selects[0][1] == {"id": "clip_2b"}, selects
print("1 HIT ok | selected by ID:", selects[0][1], "| undo_steps:", rep["undo_steps"])

# 2) MISS: blades landed (1 -> 3 clips) but no segment covers mid ->
#     both blades reverted, BridgeError
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 30)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 30)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 10),
                             clip("clip_3", 30, 10)])])
rep = _cut_spans(rpc, [(25.0, 26.0)], "t")
assert rep["ok"] is False and rep["failed"], rep
assert len(rpc.undos()) == 2, rpc.calls
print("2 MISS ok | undos:", len(rpc.undos()))

# 3) select fails identity check -> issued blades reverted, nothing deleted
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               seg_state()], fail_select=True)
rep = _cut_spans(rpc, [(25.0, 26.0)], "t")
assert rep["ok"] is False and rep["failed"], rep
assert len(rpc.undos()) == 2, rpc.calls  # the 2 blades issued, nothing else
assert not [c for c in rpc.calls if c == ("timeline.action", {"action": "delete"})]
print("3 SELECT-FAIL ok | no delete issued, undos:", len(rpc.undos()))

# 4) duration mismatch -> MISMATCH names the revert, no lie
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               seg_state()] + [seg_state()] * 5)
rep = _cut_spans(rpc, [(25.0, 26.0)], "t")
assert rep["verify"].startswith("FAILED") and "expected=39.000s" in rep["verify"], rep
assert rep["ok"] is False and rep["failed"]["span"] == [25.0, 26.0], rep
assert rep["removed"] == 0 and rep["remaining"] == 1, rep
print("4 MISMATCH ok |", rep["verify"][:70])

# 5) stale ID resolution
rpc = FakeRpc([state(40.0, [clip("clip_9", 0, 10)])])
try:
    _resolve_id(rpc, "clip_404")
    raise SystemExit("5 STALE: NO ERROR (bad)")
except BridgeError as e:
    assert "stale id" in str(e), e
    print("5 STALE ok |", str(e)[:60])

# 6) transcript cache is content-stable: rippled timelines stay usable
#    (file times re-resolve live; no duration guard to dead-end chained cuts)
import server as S
S._last_transcript = lambda: {"words": [], "timeline_duration_s": 40.0,
                              "media_path": "/tmp/x.mp4"}
assert _fresh_transcript(FakeRpc([state(39.0, [])]))["timeline_duration_s"] == 40.0
print("6 FRESH ok | rippled timeline accepted (live re-resolve)")


# 7) boundary-aware blades: span exactly on edit points issues no blades
def mclip(cid, t0, dur, trim=None, media="/tmp/x.mp4"):
    d = clip(cid, t0, dur)
    d["media_path"] = media
    d["trim_start_s"] = trim if trim is not None else t0
    return d


rpc = FakeRpc([state(30.0, [mclip("c1", 0, 10), mclip("c2", 10, 10), mclip("c3", 20, 10)]),
               state(30.0, [mclip("c1", 0, 10), mclip("c2", 10, 10), mclip("c3", 20, 10)]),
               state(30.0, [mclip("c1", 0, 10), mclip("c2", 10, 10), mclip("c3", 20, 10)]),
               state(20.0, [mclip("c1", 0, 10), mclip("c3", 10, 10, trim=20)]),
               state(20.0, [mclip("c1", 0, 10), mclip("c3", 10, 10, trim=20)])])
rep = _cut_spans(rpc, [(10.0, 20.0)], "t")
blades = [c for c in rpc.calls if c == ("timeline.action", {"action": "blade"})]
assert not blades and rep["undo_steps"] == 1, (rep, blades)
assert rep["verify"] == "ok", rep
print("7 BOUNDARY ok | no blades, undo_steps:", rep["undo_steps"])

# 7b) A merged story drop crossing existing edits must become one cut per
# primary clip. One midpoint selection cannot remove both clips.
from server import _split_at_primary_edges
assert _split_at_primary_edges([(10.0, 30.0)], [
    dict(clip("c1", 0, 10), lane="primary"),
    dict(clip("c2", 10, 10), lane="primary"),
    dict(clip("c3", 20, 10), lane="primary")]) == [(20.0, 30.0), (10.0, 20.0)]
print("7b SPLIT ok | merged drop covers two clip pieces")

# A single 20s planned range crossing two clips must issue two deletes,
# and each ripple is checked before the next piece is attempted.
three = [dict(clip("c1", 0, 10), lane="primary"),
         dict(clip("c2", 10, 10), lane="primary"),
         dict(clip("c3", 20, 10), lane="primary")]
two = three[:2]
one = three[:1]
rpc = FakeRpc([state(30.0, three),
               state(30.0, three), state(30.0, three), state(20.0, two),
               state(20.0, two), state(20.0, two), state(10.0, one)])
rep = _cut_spans(rpc, [(10.0, 30.0)], "story")
assert rep["verify"] == "ok" and rep["removed"] == 2, rep
assert rep["actual_removed_s"] == rep["removed_s"] == 20.0, rep
assert len([c for c in rpc.calls if c == ("timeline.action", {"action": "delete"})]) == 2
print("7c CROSS-CLIP ok | two verified deletes")

# 8) sentence chunking: punctuation split + word-range mapping
from server import _sentences
_w = [{"i": i, "w": w, "t_start": float(i), "t_end": float(i) + 0.5}
      for i, w in enumerate("Hello world. How are you doing today? Fine".split())]
_ss = _sentences(_w)
assert [(s["s"], s["start_word"], s["end_word"]) for s in _ss] == [(0, 0, 1), (1, 2, 6), (2, 7, 7)], _ss
assert _ss[1]["text"] == "How are you doing today?", _ss[1]["text"]
print("8 SENTENCES ok |", len(_ss), "chunks")

# 9) file->timeline resolve survives a cut (Bug 1 chaining):
#    one 30s source split [0,10]+[10,10]+[20,10]; word at file 21.0 is at
#    timeline 21.0. After cutting file [10,20) away the layout compacts to
#    [0,10]+[10,10](trim 20) and the same word re-resolves to timeline 11.0.
from server import _resolve_file_interval, _words_with_live_times, _merge_spans
_before = [mclip("c1", 0, 10, trim=0), mclip("c2", 10, 10, trim=10), mclip("c3", 20, 10, trim=20)]
_after = [mclip("c1", 0, 10, trim=0), mclip("c3", 10, 10, trim=20)]
assert _resolve_file_interval(_before, "/tmp/x.mp4", 21.0, 22.0) == [(21.0, 22.0)]
assert _resolve_file_interval(_after, "/tmp/x.mp4", 21.0, 22.0) == [(11.0, 12.0)]
assert _resolve_file_interval(_after, "/tmp/x.mp4", 12.0, 14.0) == [], "cut-away file time must map to nothing"
print("9 RESOLVE ok | file times track the compacted layout")

# 10) batch word ranges resolve to merged spans; removed words skip, not fail
import os as _os
_REAL_MEDIA = _os.path.abspath(__file__)  # on-disk stand-in for existence checks
S._last_transcript = lambda: {
    "clip": "c", "media_path": _REAL_MEDIA,
    "words": [{"i": 0, "w": "hello", "f_start": 1.0, "f_end": 1.5},
              {"i": 1, "w": "world", "f_start": 12.0, "f_end": 12.5},   # cut away
              {"i": 2, "w": "again", "f_start": 21.0, "f_end": 21.5}],
    "timeline_duration_s": 30.0}
from server import _word_ranges_to_spans
_after_real = [dict(c, media_path=_REAL_MEDIA) for c in _after]
rpc = FakeRpc([state(20.0, _after_real)])
spans, texts, skipped, refused = _word_ranges_to_spans(rpc, [(0, 1), (1, 1), (2, 1)])
assert spans == [(1.0, 1.5), (11.0, 11.5)], spans
assert not refused, refused
assert skipped and skipped[0]["start_index"] == 1, skipped
print("10 BATCH ok | spans:", spans, "| skipped:", len(skipped))

# 11) silence mapping spans clips + merges across the split
spans = _merge_spans([(1.0, 2.0), (2.01, 3.0), (9.0, 9.5)])
assert spans == [(1.0, 3.0), (9.0, 9.5)], spans
from server import _silence_warning
w = _silence_warning([(0, 100.0)], duration_s=400.0)
assert w["warning"] and w["fraction"] == 0.25, w
assert _silence_warning([(0, 10.0)], duration_s=400.0)["warning"] is None
print("11 SILENCE ok | merge + aggressive-warning")
print("ALL GREEN")
