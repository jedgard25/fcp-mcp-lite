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
                             clip("clip_4", 25, 4), clip("clip_5", 29, 10)])])
rep = _cut_spans(rpc, [(25.0, 26.0)], "t")
selects = [c for c in rpc.calls if c[0] == "timeline.select"]
assert rep["verify"] == "ok" and rep["removed"] == 1, rep
assert selects and selects[0][1] == {"id": "clip_2b"}, selects
print("1 HIT ok | selected by ID:", selects[0][1])

# 2) MISS: blades landed (1 -> 3 clips) but no segment covers mid ->
#     both blades reverted, BridgeError
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10)]),
               state(40.0, [clip("clip_1", 0, 10)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 10),
                             clip("clip_3", 30, 10)])])
try:
    _cut_spans(rpc, [(25.0, 26.0)], "t")
    raise SystemExit("2 MISS: NO ERROR (bad)")
except BridgeError as e:
    assert len(rpc.undos()) == 2, rpc.calls
    print("2 MISS ok | undos:", len(rpc.undos()))

# 3) select fails identity check -> blades reverted, nothing deleted
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               seg_state()], fail_select=True)
try:
    _cut_spans(rpc, [(25.0, 26.0)], "t")
    raise SystemExit("3 SELECT-FAIL: NO ERROR (bad)")
except BridgeError as e:
    assert len(rpc.undos()) == 4, rpc.calls  # 6 fresh - 2 pre-blade
    assert not [c for c in rpc.calls if c == ("timeline.action", {"action": "delete"})]
    print("3 SELECT-FAIL ok | no delete issued, undos:", len(rpc.undos()))

# 4) duration mismatch -> MISMATCH names the revert, no lie
rpc = FakeRpc([state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               state(40.0, [clip("clip_1", 0, 10), clip("clip_2", 10, 20)]),
               seg_state(), seg_state()])
rep = _cut_spans(rpc, [(25.0, 26.0)], "t")
assert rep["verify"].startswith("MISMATCH"), rep
print("4 MISMATCH ok |", rep["verify"][:70])

# 5) stale ID resolution
rpc = FakeRpc([state(40.0, [clip("clip_9", 0, 10)])])
try:
    _resolve_id(rpc, "clip_404")
    raise SystemExit("5 STALE: NO ERROR (bad)")
except BridgeError as e:
    assert "stale id" in str(e), e
    print("5 STALE ok |", str(e)[:60])

# 6) transcript freshness: rippled timeline refuses word cuts
import server as S
S._last_transcript = lambda: {"words": [], "timeline_duration_s": 40.0}
rpc = FakeRpc([state(39.0, [])])
try:
    _fresh_transcript(rpc)
    raise SystemExit("6 FRESH: NO ERROR (bad)")
except BridgeError as e:
    assert "re-run transcribe" in str(e), e
    print("6 FRESH ok | stale cache refused")
rpc = FakeRpc([state(40.0, [])])
assert _fresh_transcript(rpc) == {"words": [], "timeline_duration_s": 40.0}
print("7 FRESH ok | matching duration accepted")

print("ALL GREEN")
