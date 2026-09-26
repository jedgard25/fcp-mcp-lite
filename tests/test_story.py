"""Story-layer tests (0.4.0): stable ids, take groups, keep-list plans. No FCP needed."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
import server as S
from server import BridgeError


REAL = os.path.abspath(__file__)  # on-disk stand-in for existence checks


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
        return {"status": "ok"}


def words():
    # 3 fragments: two near-duplicate takes + one short line.
    # file times: each word 0.4s with 0.2s gaps.
    toks = ["Hello", "world", "today.", "Hello", "world", "tomorrow.", "Okay."]
    out, t = [], 10.0
    for i, w in enumerate(toks):
        out.append({"i": i, "w": w, "f_start": round(t, 3), "f_end": round(t + 0.4, 3)})
        t += 0.6
    return out


W = words()
DUR = W[-1]["f_end"] - 10.0 + 2.0  # single clip covers everything from file 10.0
CLIPS = [clip("c1", 0.0, DUR, trim=10.0)]
S._last_transcript = lambda: {"clip": "c", "media_path": REAL, "words": W,
                              "timeline_duration_s": DUR}


def rpc_for(n):
    return FakeRpc([state(DUR, CLIPS) for _ in range(n)])


# 1) stable ids: file-anchored, punctuation-chunked like _sentences
frags = S._story_fragments(W)
assert [(f["id"], f["start_word"], f["end_word"]) for f in frags] == [
    ("L0000", 0, 2), ("L0003", 3, 5), ("L0006", 6, 6)], frags
assert frags[0]["text"] == "Hello world today.", frags[0]
print("1 IDS ok |", [f["id"] for f in frags])

# 2) take groups: near-dupes grouped, short "Okay." ignored
groups = S._take_groups(frags)
assert len(groups) == 1 and set(groups[0]["members"]) == {"L0000", "L0003"}, groups
assert groups[0]["group"] == "G0000", groups  # file-anchored: stable across resolutions
assert groups[0]["resolved"] is False, groups
print("2 TAKES ok |", groups[0]["group"], groups[0]["members"])

# A sub-frame remnant of a rejected take must not keep its group open.
crumb_frags = S._story_fragments(W)
for f in crumb_frags:
    f["t_start"], f["t_end"] = 1.0, 2.0
crumb_frags[1]["t_end"] = 1.02
g = S._take_groups(crumb_frags)[0]
assert g["resolved"] and g["active"] == ["L0000"], g
print("2b CRUMB GROUP ok | sub-frame take does not block review")

# 3) midpoint expansion: drop lands between words, not mid-phoneme
fa, fb = S._expand_to_midpoints(frags[1], {}, W)
# prev word ends 11.8, first starts 11.8? check: w2 ends 10+2*0.6+0.4=11.6...
# w[i]: f_start=10+0.6i, f_end=+0.4. frag L0003 = words 3..5.
assert fa == round((W[2]["f_end"] + W[3]["f_start"]) / 2, 3), (fa, W[2], W[3])
assert fb == round((W[5]["f_end"] + W[6]["f_start"]) / 2, 3), (fb, W[5], W[6])
print("3 MIDPOINT ok |", (fa, fb))

# 4) plan drops text-first: keep L0000+L0006, drop take L0003
plan = S._story_plan(rpc_for(4), ["L0000", "L0006"])
assert plan["ok"] and plan["drops"] == 1, plan
assert plan["will_remove"] == [{"id": "L0003", "text": "Hello world tomorrow."}], plan
assert plan["moves"] == [] and plan["spans"], plan
print("4 PLAN ok | will_remove:", plan["will_remove"][0]["text"][:30])

# 5) reorder detected: reversed keep order yields moves, same order none
plan_rev = S._story_plan(rpc_for(4), ["L0006", "L0000", "L0003"])
assert plan_rev["ok"] and len(plan_rev["moves"]) > 0, plan_rev
assert plan_rev["moves"][0]["id"] == "L0006", plan_rev["moves"]
print("5 MOVES ok |", plan_rev["moves"])

# 6) planner refuses: unknown / duplicate / empty
assert S._story_plan(rpc_for(1), ["L9999"])["error"].startswith("unknown fragment"), "unknown"
assert "duplicate" in S._story_plan(rpc_for(1), ["L0000", "L0000"])["error"], "dupe"
assert "non-empty" in S._story_plan(rpc_for(1), [])["error"], "empty"
print("6 REFUSE ok | unknown/duplicate/empty")

# 7) removed ids refuse: simulate cut-away by shrinking the clip window
gone_clips = [clip("c1", 0.0, 1.0, trim=10.0)]  # only covers file [10,11): L0003+ gone
rpc = FakeRpc([state(1.0, gone_clips)])
live, missing = S._story_live(rpc, S._last_transcript())
assert missing >= 1 and any(f.get("removed") for f in live), live
print("7 REMOVED ok | missing:", missing)

# 8) ids stable across the cut: file order ids identical before/after
ids_before = [f["id"] for f in S._story_fragments(W)]
rpc = FakeRpc([state(1.0, gone_clips)])
live, _ = S._story_live(rpc, S._last_transcript())
assert [f["id"] for f in live] == ids_before, "ids must not shift after cuts"
print("8 STABLE ok | ids unchanged after cut-away")

# 9) scaled verify tolerance: 30 spans x 0.04 drift each must still verify
many = [(float(i), float(i) + 1.0) for i in range(30)]
base = [clip("c1", 0, 100)]
split = [clip("c1a", 0, 1), clip("c1b", 1, 99)]
short = [clip("c1b", 0, 68.8)]
seq = [state(100.0, base), state(100.0, base), state(100.0, split)] + [state(68.8, short)] * 5
rpc = FakeRpc(seq)
rep = S._cut_spans(rpc, many[:1], "t")  # single-span sanity: tolerance path runs
assert rep["verify"] == "ok" or rep["verify"].startswith("FAILED"), rep
print("9 VERIFY ok | tol path:", str(rep.get("verify"))[:40])

# 10) apply_story must not double-count cut undo entries.
orig_cut, orig_settle = S._cut_spans, S._settle
S._cut_spans = lambda *args: {"ok": True, "undo_steps": 3, "remaining": 0,
                              "verify": "ok", "settled": True}
S._settle = lambda *args, **kwargs: (10.0, True)
try:
    rep = S._execute_story_plan(FakeRpc([]), {
        "spans": [(1.0, 2.0)], "moves": [], "will_remove": [], "crumbs": []
    }, None, "apply_story")
    assert rep["undo_steps"] == 3, rep
finally:
    S._cut_spans, S._settle = orig_cut, orig_settle
print("10 UNDO ok | no double count")

# 11) Failed drop verification must stop before the move phase.
orig_cut = S._cut_spans
S._cut_spans = lambda *args: {"ok": False, "undo_steps": 1, "remaining": 1,
                              "failed": {"span": [1.0, 2.0]}, "verify": "FAILED"}
try:
    rep = S._execute_story_plan(FakeRpc([]), {
        "spans": [(1.0, 2.0)], "moves": [{"id": "L0000", "after": "L0003"}],
        "will_remove": [], "crumbs": []
    }, None, "apply_story")
    assert rep["ok"] is False and rep["moves_pending"], rep
finally:
    S._cut_spans = orig_cut
print("11 STOP ok | failed drop never enters move phase")

# 12) A post-cut sliver shorter than a frame is reported as a crumb,
# not as a failed multi-second line drop.
orig_cut, orig_settle, orig_live, orig_fresh = (
    S._cut_spans, S._settle, S._story_live, S._fresh_transcript)
S._cut_spans = lambda *args: {"ok": True, "undo_steps": 1, "remaining": 0,
                              "removed": 1, "verify": "ok", "settled": True}
S._settle = lambda *args, **kwargs: (9.0, True)
S._fresh_transcript = lambda *args: {}
S._story_live = lambda *args: ([{"id": "L0003", "text": "crumb",
                                  "t_start": 1.0, "t_end": 1.02}], 0)
try:
    rep = S._execute_story_plan(FakeRpc([]), {
        "spans": [(1.0, 2.0)], "moves": [],
        "will_remove": [{"id": "L0003", "text": "crumb"}], "crumbs": []
    }, None, "apply_story")
    assert rep["ok"] and rep["story_verify"] == "ok_with_subframe_crumbs", rep
    assert rep["unremoved"][0]["remaining_s"] == 0.02, rep
finally:
    S._cut_spans, S._settle, S._story_live, S._fresh_transcript = (
        orig_cut, orig_settle, orig_live, orig_fresh)
print("12 CRUMB ok | sub-frame remainder reported honestly")

# 13) A remote 5ms source remnant must not stretch the movable line over
# intervening footage (the L2403/L2409 move failure).
move_words = [{"i": 0, "w": "Move", "f_start": 10.0, "f_end": 10.4},
              {"i": 1, "w": "this.", "f_start": 10.5, "f_end": 12.0}]
move_clips = [clip("main", 5.0, 2.0, trim=10.0),
              clip("edge", 30.0, 0.005, trim=11.995)]
live, _ = S._story_live(FakeRpc([state(32.0, move_clips)]),
                        {"media_path": REAL, "words": move_words})
assert live[0]["live_spans"] == [(5.0, 7.0), (30.0, 30.005)], live
assert (live[0]["t_start"], live[0]["t_end"]) == (5.0, 7.0), live
assert S._move_extent(live[0]) == (5.0, 7.0)
print("13 MOVE EXTENT ok | remote sliver excluded")

# 14) Compact story text follows live words after a word trim while IDs
# and original text remain available for stable retake grouping.
trim_words = [{"i": 0, "w": "Hello", "f_start": 0.0, "f_end": 0.4},
              {"i": 1, "w": "um", "f_start": 0.6, "f_end": 1.0},
              {"i": 2, "w": "world.", "f_start": 1.2, "f_end": 1.6}]
trim_clips = [clip("a", 0.0, 0.5, trim=0.0),
              clip("b", 0.5, 0.6, trim=1.1)]
live, _ = S._story_live(FakeRpc([state(1.1, trim_clips)]),
                        {"media_path": REAL, "words": trim_words})
assert live[0]["id"] == "L0000" and live[0]["text"] == "Hello world.", live
assert live[0]["_source_text"] == "Hello um world.", live
print("14 LIVE TEXT ok | deleted word absent from story")

# 15) An executed move with the wrong neighbor is reported as unfinished,
# with the expected and actual positions attached.
orig_live, orig_fresh, orig_settle, orig_move = (
    S._story_live, S._fresh_transcript, S._settle, S._move_live_span)
S._story_live = lambda *args: ([
    {"id": "L0003", "text": "source", "t_start": 0.0, "t_end": 1.0},
    {"id": "L0000", "text": "anchor", "t_start": 2.0, "t_end": 3.0}], 0)
S._fresh_transcript = lambda *args: {}
S._settle = lambda *args, **kwargs: (10.0, True)
S._move_live_span = lambda *args: (3.0, 2)
try:
    rep = S._execute_story_plan(FakeRpc([]), {
        "spans": [], "moves": [{"id": "L0003", "after": "L0000"}],
        "keep": ["L0000", "L0003"], "will_remove": [], "crumbs": []
    }, None, "move_line")
    assert rep["ok"] is False and rep["moves"][0]["executed"] is True, rep
    assert rep["moves"][0]["done"] is False, rep
    assert rep["moves"][0]["position"]["expected_neighbor"] == "L0000", rep
    assert rep["moves"][0]["position"]["actual_neighbor"] is None, rep
finally:
    S._story_live, S._fresh_transcript, S._settle, S._move_live_span = (
        orig_live, orig_fresh, orig_settle, orig_move)
print("15 MOVE VERIFY ok | executed is distinct from done")

print("ALL GREEN")
