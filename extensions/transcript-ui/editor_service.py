"""Native editor: live slices, revision-checked edits, one authoritative snapshot.

A slice is a sentence intersected with one physical clip. No stored UI order or
split metadata: refresh, restart and FCP undo all reconstruct the same view.
"""
import hashlib
import json
import threading

import server as core

LOCK = threading.RLock()


def snapshot(rpc):
    transcript = core._fresh_transcript(rpc)
    state = core._clips(rpc)
    clips = sorted(core._primary_clips(state), key=lambda c: c.get("timeline_start_s", 0))
    media = transcript.get("media_path")
    words = transcript.get("words", [])
    if not media or not words or "f_start" not in words[0]:
        raise core.BridgeError("A transcript with source word timings is required. Transcribe in FCP first.")
    # Playback and selection are UI state, not edits. Hash only the structure
    # needed to resolve source words; moving the playhead must not stale a card.
    structure = [{"media": c.get("media_path"), "lane": c.get("lane"),
                  "container": c.get("container", False),
                  "start": c.get("timeline_start_s"), "duration": c.get("duration_s"),
                  "source": core._clip_file_window(c) if c.get("media_path") else None}
                 for c in state.get("clips", [])]
    revision = hashlib.sha256(json.dumps([media, words, state.get("sequence_name"),
                                         state.get("fps"), state.get("duration_s"), structure],
                                        sort_keys=True).encode()).hexdigest()
    groups = core._story_fragments(words)
    group_for = {i: f["id"] for f in groups for i in range(f["start_word"], f["end_word"] + 1)}
    slices = []
    seen = set()
    problem = None
    for clip in clips:
        if clip.get("media_path") != media:
            continue
        f0, f1 = core._clip_file_window(clip)
        start = float(clip["timeline_start_s"])
        # Assign each word to the clip containing its midpoint. Tiny leftovers
        # cannot duplicate a word or turn into independently editable rows.
        live = [w for w in words if f0 <= (w["f_start"] + w["f_end"]) / 2 < f1
                and min(f1, w["f_end"]) - max(f0, w["f_start"]) >= core.SILENCE_FRAME]
        runs = []
        for w in live:
            if not runs or group_for[w["i"]] != group_for[runs[-1][-1]["i"]]:
                runs.append([])
            runs[-1].append(w)
        boundaries = [f0] + [max(f0, min(f1, (a[-1]["f_end"] + b[0]["f_start"]) / 2))
                                 for a, b in zip(runs, runs[1:])] + [f1]
        for n, run in enumerate(runs):
            key = f'S{run[0]["i"]}-{run[-1]["i"]}'
            if any(w["i"] in seen for w in run):
                problem = "Repeated source footage detected. Editing is disabled because word identities are ambiguous."
                key += f'-copy-{len(slices)}'
            seen.update(w["i"] for w in run)
            a, b = boundaries[n:n + 2]
            slices.append({"id": key, "text": " ".join(w["w"] for w in run),
                           "start": round(start + a - f0, 6), "end": round(start + b - f0, 6),
                           "words": [{"i": w["i"], "w": w["w"],
                                      "start": max(start, start + w["f_start"] - f0),
                                      "end": min(start + f1 - f0, start + w["f_end"] - f0)} for w in run]})
    if core._trim_health([c for c in clips if c.get("media_path") == media]):
        problem = "Final Cut is reporting ambiguous source trims. Repair the bridge before editing."
    return {"revision": revision, "timeline": state.get("sequence_name"), "title": transcript.get("clip") or "Timeline",
            "duration": state.get("duration_s", 0), "slices": slices, "edit_error": problem}


def read():
    with LOCK:
        return core.logged("editor_read", {}, snapshot)


def word_order(story):
    return [w["i"] for s in story["slices"] for w in s["words"]]


def edit(body):
    def run(rpc):
        before = snapshot(rpc)
        if before.get("edit_error"):
            return {"ok": False, "error": before["edit_error"], "story": before}
        rows = before["slices"]
        src = next((s for s in rows if s["id"] == body.get("id")), None)
        if body.get("revision") != before["revision"]:
            # Rebase the user's intent on current positions only when the exact
            # source/anchor word identities they saw still exist. No write has
            # happened yet, so this is validation, never replaying a mutation.
            source_matches = body.get("timeline") == before.get("timeline") and src is not None and body.get("source_words") == [w["i"] for w in src["words"]]
            anchor = next((s for s in rows if s["id"] == body.get("before_id")), None)
            anchor_matches = (body.get("before_id") is None or
                              (anchor is not None and body.get("anchor_words") == [w["i"] for w in anchor["words"]]))
            if not source_matches or (body.get("action") == "move" and not anchor_matches):
                return {"ok": False, "code": "stale", "error": "That card changed in Final Cut. The latest timeline is now shown.", "story": before}
        if src is None:
            return {"ok": False, "code": "stale", "error": "That card was removed in Final Cut. The latest timeline is now shown.", "story": before}
        action = body.get("action")
        expected = word_order(before)
        span = (src["start"], src["end"])
        if action == "move":
            rest = [s for s in rows if s["id"] != src["id"]]
            target = body.get("before_id")
            anchor = next((s for s in rest if s["id"] == target), None)
            if target is not None and anchor is None:
                raise core.BridgeError("The destination no longer exists.")
            index = rest.index(anchor) if anchor else len(rest)
            ordered = rest[:index] + [src] + rest[index:]
            expected = [w["i"] for s in ordered for w in s["words"]]
            dest = anchor["start"] if anchor else (rest[-1]["end"] if rest else src["end"])
            if expected == word_order(before):
                return {"ok": True, "story": before}
            plan = {"text": src["text"], "span": span, "destination": dest}
        elif action in ("trim", "delete", "split"):
            selected = body.get("words", [])
            indices = [w["i"] for w in src["words"]]
            if action == "delete":
                selected = indices
            if not isinstance(selected, list) or not selected or any(i not in indices for i in selected):
                raise core.BridgeError("Select words in the current slice first.")
            positions = sorted({indices.index(i) for i in selected})
            if positions != list(range(positions[0], positions[-1] + 1)):
                raise core.BridgeError("Select one continuous range of words.")
            lo, hi = positions[0], positions[-1]
            ws = src["words"]
            def boundary(k):
                return max(span[0], min(span[1], (ws[k - 1]["end"] + ws[k]["start"]) / 2))
            if action == "split":
                if hi == len(ws) - 1:
                    raise core.BridgeError("Split after a word before the end of the slice.")
                cut = boundary(hi + 1)
                plan = {"text": ws[hi]["w"], "split_at": cut}
            else:
                span = (span[0] if lo == 0 else boundary(lo), span[1] if hi == len(ws) - 1 else boundary(hi + 1))
                expected = [i for i in expected if i not in selected]
                plan = {"text": " ".join(w["w"] for w in ws[lo:hi + 1]), "span": span}
        else:
            raise core.BridgeError("Unknown editor action.")
        if body.get("dry_run", False):
            return {"ok": True, "dry_run": True, "plan": plan, "story": before}
        # Never replay mutations automatically, including after a network timeout.
        try:
            if action == "move":
                with core._WRITE_LOCK:
                    core._move_live_span(rpc, span, dest)
            elif action == "split":
                with core._WRITE_LOCK:
                    rpc("playback.seek", {"t_s": cut})
                    rpc("timeline.action", {"action": "blade"})
            else:
                report = core._cut_spans(rpc, [span], "editor_" + action)
                if report.get("ok") is False or report.get("remaining", 0) or report.get("refused"):
                    raise core.BridgeError(report.get("error") or report.get("verify") or "The cut did not complete.")
            core._settle(rpc, tries=3, pause_s=0.2)
            after = snapshot(rpc)
            verified = word_order(after) == expected
            if action in ("move", "split"):
                verified = verified and abs(after["duration"] - before["duration"]) < 0.15
            if action == "split":
                verified = verified and len(after["slices"]) == len(rows) + 1
            return {"ok": verified, "story": after,
                    "error": None if verified else "Final Cut did not confirm the requested edit. The actual timeline is shown; inspect it before another edit."}
        except Exception as exc:
            # A write may have happened: return actual state, never an invented rollback.
            try:
                after = snapshot(rpc)
            except Exception:
                after = None
            return {"ok": False, "error": str(exc), "story": after}
    with LOCK:
        return core.logged("editor_edit", body, run)
