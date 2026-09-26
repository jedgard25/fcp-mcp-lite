---
name: fcp-edit
description: Rough-cut talking-head video in Final Cut Pro via fcp-mcp-lite tools. Load before any FCP editing task — silences, retakes, reorder, review gate.
---

# FCP rough-cut workflow (fcp-mcp-lite MCP)

## Server choice

Two video MCP servers exist. FCP timeline work uses `fcp-mcp-lite_*`
tools ONLY. Never mix WeftCut and FCP tools in one edit.

## The loop (in order, no shortcuts)

1. `bridge_status` — confirm `mcp` 0.5.2+. If older, stop and tell the user.
2. `transcribe` once (cached after) — then use `get_story`, never page
   `get_transcript` (one read was 396KB).
3. `get_story` compact — page until `next_offset` is null. Lines (`L####`)
   and take groups (`G####`) are file-anchored and never shift. A line
   marked `crumb` has less than one frame left and is not cuttable.
4. `remove_silences` dry_run first, then commit in default chunks of 40
   primary clip pieces (follow `remaining` to 0).
5. `choose_takes` ONE batch `{group: keep}` for ALL retake groups
   (dry_run first). Never one group per call.
6. `delete_lines` / `move_line` for targeted fixes (dry_run first,
   eye-check TEXT not seconds). After `delete_words`, re-read the affected
   lines with `get_story`: compact text now reflects surviving words.
7. Semantic sweep BY HAND (mandatory, no shortcuts): re-read the full
   compact story end to end and cut paraphrase duplicates that take
   detection misses. Take groups are fuzzy string matches on
   near-identical wording only — never exhaustive. Adjacent lines
   restating one idea in different words are retakes: keep the most
   complete take, delete the rest with `delete_lines` (dry_run first).
   Run-on signature: one line over ~25 words repeating its opener 2-4x
   ("but…but…but…", "and…and…", "so…so…") is a false-start bundle, not
   a sentence — drop to per-word timings and cut the dead restarts with
   `delete_words` (dry_run first, eye-check `edge_warnings`).
   Resolution rule: the goal is sensical slices, not short slices. A
   long sentence with tight gaps stays one slice; split only where word
   timings show room (~0.5s+ silence). Default keeper is the last
   restart (the delivery they settled on) — but verify, since the first
   take sometimes has the energy. Skipping this step is how audible
   repeats ship.
8. `review` — the take-group gate, NOT a clean-script verdict. Do NOT
   finish while `open_items` is non-empty. `ready=true` means groups are
   resolved; only step 7 makes the script clean. Re-review after each
   fix round.

## Verb selection (intent → tool)

- Delete lines → `delete_lines([ids])`
- Move a line → `move_line` (never `move_words`)
- Retakes → `choose_takes` batch (never `choose_take` one-by-one)
- Complete ordered keep list already prepared → `apply_story`. For a few
  deletions or moves, use `delete_lines` and `move_line`; they use the same
  underlying planner, so `apply_story` does not make moves safer.
- `delete_words` / `move_words` / `cut_spans` are LOW-LEVEL primitives
  for sub-line surgery only. If you are hand-computing word indices
  for whole lines, stop — use `delete_lines`.
- `get_timeline(start_s=…, end_s=…, detail="full")` for a local clip
  window, or `clip_id=…` for one clip. The default is a compact 50-clip
  page; follow `next_offset` to page. `verify_action` gives a summary.

## Hard rules

- `dry_run=true` first on EVERY mutation. Commit only after
  eye-checking `will_remove`/`deleted` as text.
- Mutations strictly SERIAL, one call per block. Parallel mutation
  calls plan on the same snapshot and race (server serializes, but
  your later calls then act on stale plans — re-read instead).
- Sub-frame refusals (`refused`, `unremoved`) cannot be cut safely; leave
  them. `review.crumbs` also lists fragments up to 0.25s, which may be
  audible. The gate reports story decisions, not an audio quality check.
- Take groups are a fuzzy-match heuristic, not ground truth: they miss
  paraphrase retakes and can false-positive on stock phrases. `review`
  passing never excuses skipping the hand semantic sweep.
- `ok=false` or `verify=FAILED` → stop writes. Inspect `failed`,
  `actual_removed_s`, `still_present`, `moves[].position`, and the live
  timeline. `done=true` now means a move's neighbor verified. Do not
  blindly retry `pending` or blindly undo: FCP may have accepted an
  action without creating the expected undo entry.
- `unknown group` → re-read `get_story` (resolved groups hide by
  default; `show_resolved=true` to see them). Group ids are stable;
  a rename means you are on mcp < 0.5.0.
- `apply_story` chunks: re-call with the same keep list until
  `remaining=0`; each call re-plans from live clips. Other story verbs
  can finish their returned `pending` through `cut_spans`.
