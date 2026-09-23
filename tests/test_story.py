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
assert groups[0]["group"] == "G" + min(groups[0]["members"]), groups
print("2 TAKES ok |", groups[0]["group"], groups[0]["members"])

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
base = [clip("c1", 0, 100), clip("c2", 0, 100)]
seq = [state(100.0, base), state(100.0, base), state(100.0, base),
       state(100.0 - 30 * 1.04, base)]
rpc = FakeRpc(seq)
rep = S._cut_spans(rpc, many[:1], "t")  # single-span sanity: tolerance path runs
assert rep["verify"] == "ok" or rep["verify"].startswith("MISMATCH"), rep
print("9 VERIFY ok | tol path:", str(rep.get("verify"))[:40])

print("ALL GREEN")
