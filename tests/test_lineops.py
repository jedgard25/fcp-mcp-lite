"""0.5.0 line-verb tests: crumb refusal, stable groups, batch validation,
slim verify, settle, review gate. No FCP needed."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
import server as S
from server import BridgeError


REAL = os.path.abspath(__file__)


def clip(cid, start, dur, trim=0, media=None):
    return {"id": cid, "timeline_start_s": start, "duration_s": dur,
            "trim_start_s": trim, "media_path": media or REAL, "name": cid}


def state(dur, clips):
    return {"duration_s": dur, "clips": clips}


class FakeRpc:
    def __init__(self, clip_responses):
        self.q, self.calls = list(clip_responses), []

    def __call__(self, method, params=None):
        self.calls.append((method, params))
        if method == "timeline.clips":
            return self.q.pop(0)
        if method == "playback.position":
            return {"t_s": 1.0, "fps": 30.0, "duration_s": 99.0}
        return {"status": "ok"}

    def actions(self):
        return [c for c in self.calls if c[0] == "timeline.action"]


def words():
    toks = ["Hello", "world", "today.", "Hello", "world", "tomorrow.", "Okay."]
    out, t = [], 10.0
    for i, w in enumerate(toks):
        out.append({"i": i, "w": w, "f_start": round(t, 3), "f_end": round(t + 0.4, 3)})
        t += 0.6
    return out


W = words()
DUR = W[-1]["f_end"] - 10.0 + 2.0
CLIPS = [clip("c1", 0.0, DUR, trim=10.0)]
S._last_transcript = lambda: {"clip": "c", "media_path": REAL, "words": W,
                              "timeline_duration_s": DUR}


def rpc_for(n, dur=DUR, clips=None):
    return FakeRpc([state(dur, clips or CLIPS) for _ in range(n)])


# 1) OVERCUT FIX: a 1ms crumb range is refused, never executed
crumb_words = [dict(w) for w in W] + [
    {"i": 7, "w": "Peace.", "f_start": 14.200, "f_end": 14.201}]  # 1ms crumb
S._last_transcript = lambda: {"clip": "c", "media_path": REAL, "words": crumb_words,
                              "timeline_duration_s": DUR}
rpc = rpc_for(2)
spans, texts, skipped, refused = S._word_ranges_to_spans(rpc, [(7, 1)])
assert spans == [] and len(refused) == 1 and refused[0]["text"] == "Peace.", (spans, refused)
assert rpc.actions() == [], rpc.calls  # resolve is reads-only
print("1 CRUMB-REFUSED ok |", refused[0]["reason"][:50])

# 2) _cut_spans fails closed on sub-frame spans (no writes at all)
rpc = rpc_for(2)
try:
    S._cut_spans(rpc, [(5.0, 5.001)], "t")
    raise SystemExit("2 SUBFRAME: NO ERROR (bad)")
except BridgeError as e:
    assert "sub-frame" in str(e) and "overcut" in str(e), e
    assert rpc.actions() == [], rpc.calls
    print("2 FAIL-CLOSED ok |", str(e)[:60])

# restore full-word transcript for the rest
S._last_transcript = lambda: {"clip": "c", "media_path": REAL, "words": W,
                              "timeline_duration_s": DUR}

# 3) STABLE GROUP IDS: same id before/after resolving a member
frags = S._story_fragments(W)
g0 = S._take_groups(frags)
assert len(g0) == 1 and g0[0]["group"] == "G0000", g0
assert g0[0]["resolved"] is False and set(g0[0]["active"]) == {"L0000", "L0003"}, g0
# simulate L0003 cut away: clip window covers only file [10, 11.8)
gone = [clip("c1", 0.0, 1.8, trim=10.0)]
live, _ = S._story_live(rpc_for(1, dur=1.8, clips=gone),
                        {"clip": "c", "media_path": REAL, "words": W, "timeline_duration_s": 1.8})
g1 = S._take_groups(live)
assert len(g1) == 1 and g1[0]["group"] == "G0000", g1  # id did NOT rename
assert g1[0]["resolved"] is True and g1[0]["kept"] == "L0000", g1
print("3 STABLE-GROUP ok | G0000 survives resolution, kept=L0000")

# 4) batch validation: one bad entry refuses everything, zero writes
live_full, _ = S._story_live(rpc_for(1), S._last_transcript())
try:
    S._validate_takes_choices(live_full, [("G0000", "L0000"), ("G9999", "L0000")])
    raise SystemExit("4 BATCH: NO ERROR (bad)")
except BridgeError as e:
    assert "Nothing executed" in str(e), e
    print("4 BATCH-REFUSE ok |", str(e)[:60])
keep_ids, per_group, noop = S._validate_takes_choices(live_full, [("G0000", "L0000")])
assert keep_ids == ["L0000", "L0006"] and per_group["G0000"]["keep"] == "L0000", (keep_ids, per_group)
assert noop == []
print("4b BATCH-PLAN ok | keep:", keep_ids)

# 5) resolved-group choice is a no-op (not an error, not a 230-id dump)
keep_ids2, per_group2, noop2 = S._validate_takes_choices(live, [("G0000", "L0000")])
assert noop2 == ["G0000"] and per_group2["G0000"]["noop"] is True, (noop2, per_group2)
assert keep_ids2 == ["L0000"], keep_ids2  # survivor only
print("5 RESOLVED-NOOP ok")

# 6) _normalize_choices shapes
assert S._normalize_choices({"G0000": "L0000"}) == [("G0000", "L0000")]
assert S._normalize_choices([["G0000", "L0000"]]) == [("G0000", "L0000")]
assert S._normalize_choices([{"group": "G0000", "keep": "L0000"}]) == [("G0000", "L0000")]
for bad in ({}, [], [{"group": "G0000"}], [("G0000", "L0000"), ("G0000", "L0003")]):
    try:
        S._normalize_choices(bad)
        raise SystemExit(f"6 NORMALIZE: NO ERROR for {bad}")
    except BridgeError:
        pass
print("6 NORMALIZE ok")

# 7) line-id checker: unknown / dup / removed / empty
assert S._check_line_ids(live_full, ["L9999"]).startswith("unknown"), "unknown"
assert "duplicate" in S._check_line_ids(live_full, ["L0000", "L0000"]), "dupe"
assert "already-removed" in S._check_line_ids(live, ["L0003"]), "gone"
assert "non-empty" in S._check_line_ids(live_full, []), "empty"
assert S._check_line_ids(live_full, ["L0000"]) is None
print("7 CHECK-IDS ok")

# 8) delete_lines planning path: keep minus ids -> text-first will_remove
plan = S._story_plan(rpc_for(4), ["L0000", "L0006"])
assert plan["ok"] and plan["will_remove"] == [{"id": "L0003", "text": "Hello world tomorrow."}], plan
assert plan.get("unremoved", []) == []
print("8 DELETE-PLAN ok |", plan["will_remove"][0]["text"][:30])

# 9) slim verify: durations + counts, no clip dump
summ = S._verify_summary(rpc_for(1))
assert set(summ) == {"duration_s", "clip_count", "playhead_s", "fps"}, summ
assert summ["clip_count"] == 1 and summ["duration_s"] == round(DUR, 3), summ
print("9 SLIM-VERIFY ok |", summ)

# 10) settle: stable -> True in 2 reads; drifting -> False, bounded
d, ok = S._settle(FakeRpc([state(10.0, []), state(10.0, [])]), tries=3, pause_s=0)
assert (d, ok) == (10.0, True)
rpc = FakeRpc([state(10.0, []), state(11.0, []), state(12.0, [])])
d, ok = S._settle(rpc, tries=3, pause_s=0)
assert (d, ok) == (12.0, False) and len(rpc.calls) == 3, (d, ok, rpc.calls)
print("10 SETTLE ok")

# 11) review gate: open takes block, resolved takes pass
rev = S._review_state(rpc_for(3))
assert rev["ready"] is False and rev["open_take_count"] == 1, rev
assert rev["gate"].startswith("NOT DONE") and any("choose_takes" in i for i in rev["open_items"]), rev
assert rev["open_takes"][0]["group"] == "G0000" and len(rev["open_takes"][0]["members"]) == 2
rev2 = S._review_state(FakeRpc([state(1.8, gone), state(1.8, gone), state(1.8, gone)]))
assert rev2["ready"] is True and rev2["open_take_count"] == 0, rev2
assert rev2["resolved_take_count"] == 1 and rev2["gate"].startswith("DONE"), rev2
print("11 REVIEW ok | gate:", rev["gate"][:40], "->", rev2["gate"])

# 12) silence warning no longer recommends the already-set param
w = S._silence_warning([(0, 200.0)], duration_s=400.0, min_duration_s=1.0)
assert w["warning"] and "min_duration_s=1.0" not in w["warning"] and "threshold_db" in w["warning"], w
w2 = S._silence_warning([(0, 200.0)], duration_s=400.0, min_duration_s=0.5)
assert "min_duration_s=1.0" in w2["warning"], w2
print("12 WARNING ok")

print("ALL GREEN")
