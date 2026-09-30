"""Verbatim enrichment tests (0.5.5): filler/repeat tags, gap events, v3 migration."""
import copy
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mcp"))
import server as S


def mkword(i, w, fs, fe, **kw):
    d = {"i": i, "w": w, "f_start": fs, "f_end": fe}
    d.update(kw)
    return d


# 1) filler tagging: singles + multi-word phrases, idempotent
words = [mkword(0, "So,", 0.0, 0.2), mkword(1, "um,", 0.3, 0.6),
         mkword(2, "today", 0.9, 1.2), mkword(3, "you", 1.3, 1.4),
         mkword(4, "know", 1.4, 1.6), mkword(5, "yeah.", 1.7, 2.0)]
n = S._tag_filler_words(words)
assert n == 3, (n, words)  # um + you/know (verbatim list is narrower than FILLER_WORDS: no yeah/okay)
assert words[1]["filler"] and words[3]["filler"] and words[4]["filler"]
assert not words[0].get("filler") and not words[2].get("filler")
assert not words[5].get("filler")  # yeah stays in the island allowlist, not a verbatim filler
again = S._tag_filler_words(words)
assert again == 0, again  # idempotent
print("1 FILLER ok |", n, "tagged")

# 1b) "like" as filler, but content words untouched
words_lb = [mkword(0, "I", 0.0, 0.1), mkword(1, "like", 0.2, 0.4),
            mkword(2, "cats.", 0.5, 0.8)]
S._tag_filler_words(words_lb)
assert words_lb[1].get("filler") is True
assert not words_lb[0].get("filler") and not words_lb[2].get("filler")
print("1b LIKE ok | flagged but agent may overrule (content use)")

# 2) repeat tagging: "I I think" + phrase repeat, second take clean
words_r = [mkword(0, "I", 0.0, 0.2), mkword(1, "I", 0.25, 0.45),
           mkword(2, "think", 0.5, 0.8), mkword(3, "we", 1.0, 1.2),
           mkword(4, "should", 1.2, 1.5), mkword(5, "go.", 1.6, 1.9)]
S._tag_repeat_words(words_r)
assert words_r[0].get("repeat") is True, words_r
assert not words_r[1].get("repeat"), words_r  # second take stays clean
assert not words_r[2].get("repeat")
print("2 REPEAT ok | first take flagged:", words_r[0]["w"])

# 3) gaps: only >= 0.15s reported, pure (no I/O)
words_g = [mkword(0, "a", 0.0, 0.4), mkword(1, "b", 0.45, 0.8),
           mkword(2, "c", 1.5, 1.9)]
gaps = S._word_gaps(words_g)
assert len(gaps) == 1 and gaps[0]["kind"] == "gap", gaps
assert gaps[0]["f_start"] == 0.8 and gaps[0]["f_end"] == 1.5, gaps
print("3 GAPS ok |", gaps)

# 4) labeling: covered gap -> silence, sounding gap -> noise
labeled = S._label_gaps_with_silences(
    gaps, [{"start": 0.7, "duration": 1.0}])
assert labeled[0]["kind"] == "silence", labeled
labeled2 = S._label_gaps_with_silences(
    gaps, [{"start": 5.0, "duration": 1.0}])
assert labeled2[0]["kind"] == "noise", labeled2
print("4 LABEL ok | silence vs noise")

# 5) v3 -> v4 migration: backfilled in place, no forced re-transcribe
v3 = {"schema": 3, "clip": "c", "media_path": "/tmp/x.mp4",
      "words": [mkword(0, "um", 0.0, 0.3), mkword(1, "hi.", 0.5, 0.8)]}
import json
import tempfile
key = "migrate-test"
os.makedirs(S.CACHE_DIR, exist_ok=True)
with open(os.path.join(S.CACHE_DIR, key + ".json"), "w") as f:
    json.dump(v3, f)
loaded = S._load_cache(key)
assert loaded is not None and loaded["schema"] == 4, loaded
assert loaded["words"][0].get("filler") is True, loaded["words"]
os.remove(os.path.join(S.CACHE_DIR, key + ".json"))
print("5 MIGRATE ok | v3 upgraded, filler backfilled")

# 5b) legacy without file times still misses (forces re-transcribe)
legacy = {"schema": 3, "clip": "c",
          "words": [{"i": 0, "w": "hi"}]}
with open(os.path.join(S.CACHE_DIR, key + ".json"), "w") as f:
    json.dump(legacy, f)
assert S._load_cache(key) is None
os.remove(os.path.join(S.CACHE_DIR, key + ".json"))
print("5b LEGACY ok | pre-file-times cache still misses")

# 6) fragments carry has_filler / has_repeat (full detail only)
frag_words = [mkword(0, "So", 0.0, 0.2), mkword(1, "um", 0.3, 0.5),
              mkword(2, "go.", 0.6, 0.9)]
S._ensure_word_tags(frag_words)
frags = S._story_fragments(frag_words)
assert frags[0].get("has_filler") is True, frags
assert "has_repeat" not in frags[0], frags
print("6 FRAG ok |", frags[0]["id"], "has_filler")

# 7) island collapse prefers tagged flags over allowlist alone
# (regression: 4-tuple atom unpacking + dedupe)
W = [mkword(0, "Hello", 10.0, 10.4), mkword(1, "uh", 10.6, 10.8),
     mkword(2, "world.", 11.5, 11.9)]
S._ensure_word_tags(W)
REAL = os.path.abspath(__file__)
CLIPS = [{"id": "c1", "timeline_start_s": 0.0, "duration_s": 5.0,
          "trim_start_s": 10.0, "media_path": REAL, "name": "c1"}]
S._last_transcript = lambda: {"clip": "c", "media_path": REAL, "words": W}
S._all_existing_clips = lambda rpc: CLIPS
merged, collapsed, details = S._collapse_silence_islands(
    [(0.0, 0.5), (0.9, 2.0)], rpc=None, max_island_s=1.5)
assert collapsed == 1 and "filler" in details[0]["reason"], (collapsed, details)
print("7 ISLAND ok |", details[0]["reason"])

print("ALL GREEN")
