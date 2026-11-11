# Direction: FCP as the editor, an agent-native MCP as the brain

**Goal.** A blind Claude session, given footage, a script and this MCP, builds an
edit of the *best-pokemon-artists* class in **20–30 minutes**, inside Final Cut
Pro. That means a transcript rough cut, dual-system A-roll, multi-lane animated
B-roll, backdrops, VO, fades and roles, and the agent checks its own work by
looking at it.

**Decision.** This replaces WeftCut as the editing surface. WeftCut's ideas
(the edit model, the verbs, the agent loop, the vision tools) carry over. Its
engine (decode, composite, preview cache, export) does not: FCP does that.
There is one direction, and this document is it. Evidence lives in
[`weftcut-parity-audit.md`](weftcut-parity-audit.md). The wire contract lives in
[`bridge.md`](bridge.md).

## Why this is credible (measured, 2026-11-11)

| Claim | Evidence |
|---|---|
| FCP can hold a WeftCut edit 1:1 | reference edit converted: 120/120 project clips on the right lane/frame; 74/74 animated clips within **0.36 px** on every frame (47,795 sampled in FCP's live channels) |
| Edits apply **in place** on the open project | FCPXML fragment → pasteboard → `pasteAnchored:`: 75 ms/clip, crop, keyframes and audio-off intact |
| Batches are atomic and undoable | `FFUndoManager` grouping: 3 pastes in 140 ms = one named undo step |
| Exact motion, beyond what FCPXML can express | `FFChannelChangeController` + `CHChannel` tangents reproduce WeftCut's cubic-beziers (max 0.005 units) with a real undo |
| The agent can see | composite frames from FCP's own renderer (~330 ms, no screen permission), timeline/UI screenshots in-process, per-cut contact sheets with waveforms from sources |
| Units match visually | FCP composite vs WeftCut export at the same timestamps: host framing and backdrop panel geometry match |

## Architecture: a full MCP, not a file loop

It is **not** "export FCPXML, edit the file, re-import a project". Whole-project
import survives only for migration and recovery. The loop is:

```
           ┌────────────── read: export_xml(open project) ~100 ms ──────────────┐
           ▼                                                                     │
  Edit model (Python)  ── plan / dry_run / diff ──►  writes, grouped as ONE undo │
  slices + lanes + IDs                               ├─ structure: FCPXML fragment → paste (connect/insert/overwrite)
  (FCPXML is the wire                                ├─ params:    channel writes (transform, opacity, crop, volume, curves)
   format, both ways)                                └─ edits:     action* methods (trim, blade, move, anchor, lane, role)
           ▲                                                                     │
           └──── eyes: composite frames · cut sheets · timeline screenshot ◄─────┘
```

Layers, bottom up:

1. **Bridge** (`bridge/FCPBridge.m`, ObjC, in-process). These are dumb, typed,
   main-thread verbs: `export_xml`, `paste_xml{xml, at, mode, lane}`,
   `batch_begin/end{name}`, `set_param{id, param, keys|tangents}`,
   `edit{op, ids, ...}`, `open_project`, `frames{times, width}`,
   `screenshot{region}`, `undo/redo`. Every verb carries a modal watchdog and
   returns before/after timings. `dev.probe` stays for discovery.
2. **Edit model** (`mcp/model/`, Python). Parses FCPXML into **slices**
   (anything placed on the timeline) that each have a **stable ID**, a lane and
   a role. It emits FCPXML fragments. It holds the WeftCut-side semantics
   (absolute time, z-order, layer-local keyframes, WeftCut easing) and the
   converter's mapping rules. FCP is never the planner.
3. **Verbs** (`mcp/server.py`). These are WeftCut-shaped and agent-facing:
   `place_broll`, `animate` (presets plus exact curves), `fade`, `trim`,
   `move`, `restack`, `set_role_gain`, `apply_cut_list`/story verbs (existing),
   `checkpoint` (FCP snapshot), `frames`, `cut_sheet`, `look_at_timeline`.
   Every write verb defaults to `dry_run` and verifies after it writes.
4. **Skill** (`skills/fcp-edit/`). The agent's playbook: the order of
   operations for a talking-head explainer, how to read a cut sheet, when to
   look, house style (motion presets, fades, lane/role conventions).

### Identity: every lane and slice is addressable

- **Slice ID**: `com.fcpmcp.id` in each clip's FCPXML `<metadata>`, minted by the
  model and re-read on every `export_xml`. (To verify: does FCP keep custom
  metadata keys through paste and through a user's edits? The fallback is a
  tag in the clip note. The spike used a name suffix, which worked but is
  visible.)
- **Section colors through an injected group hook, not roles**:
  `bridge/hooks/group_colors.m` replaces `+[FFColorForRole colorSchemeForRolesOfObject:]`
  so a clip (and its connected audio, compounds included) tagged
  `fcpmcp:group=<Name>:<palette>` in its note draws in that palette color. Untagged
  clips keep role colors, so it degrades harmlessly if the hook isn't loaded.
  Chapter markers mark the section starts. See `docs/groups-demo.png`. (Spike: loaded through
  `dev.probe`; production: install from the bridge at launch.)
- **Lane semantics through roles**: FCP lanes are just integers, but video
  roles/subroles (`B-roll.lane-1`, `Backdrop`, `Host`) are first-class: they
  are shown in the timeline index, can be soloed, and survive magnetic
  reshuffles. The model treats role + lane as the address of a "track".
- **Magnetic timeline as a feature**: B-roll anchored to the compound A-roll
  moves with it when the rough cut changes, so ripple edits don't orphan
  overlays. The model works out anchor and offset when it writes; the agent
  never thinks about anchors.

### Vision: three instruments

| Instrument | Answers | How | Cost |
|---|---|---|---|
| **Composite frames** | "what does the viewer show at t?" | seek → `FFDestVideoDisplay _copyPlayerFrameForTime:` → `FFPlayerFrame.image1` (`FFImage`) → `newCGImageRef` | ~330 ms/frame, viewer quality (1080p at "better performance") |
| **Cut sheet** | "is this cut clean, is this the right take?" | from `export_xml`: source path + in/out per slice → ffmpeg frames at head/tail + `showwavespic` with handles and edit lines (`tools/cut_sheet.py`) | ~0.7 s/cut, never touches FCP |
| **Story check** | "does any line play twice, did a retake word survive a cut?" | kept transcript in timeline order from each clip's connected-audio range → near-duplicate sentences + same-word-across-cut (`tools/story_check.py`) | instant; caught 3 real defects in the first blind run that the visual audit missed |
| **Timeline screenshot** | "what's placed where, what's disabled, what did I forget?" | in-process `cacheDisplayInRect:` of FCP's own window/timeline view: no TCC, because it is FCP drawing itself | ~100 ms |

Planned: contact sheets of composite frames (N frames per slice, with lane
labels), a "diff look" (before/after frames for a batch), and audio meters per
role over a range.

## The benchmark is the spec

`best-pokemon-artists` is the acceptance test, and it is scored automatically:

1. **Structure**: `bench/rtdiff.py` (slices matched by lane + frame; roles, crops, fades, enabled).
2. **Motion**: `bench/motion_audit.py` (per-frame position error from FCP's live channels).
3. **Look**: composite frames vs `weftcut-export.mov` at N timestamps (SSIM/ΔE).
4. **Time**: wall-clock from an empty project to the scored state, with a blind session driving it.

A nightly (or per-PR) run does this: start a fresh Claude session with only
the skill, the footage and the script. Let it build. Score it. The score
trend is the project's progress bar. The converter output is the oracle;
a blind session's result has to reach close to it, not byte-equal.

## With agents writing 2–10k LOC/day

Code volume is not the constraint. **Verification against a live FCP is.**
Shape the work so that throughput lands:

1. **Freeze contracts first** (day 1): the bridge JSON schema (`bridge.md`) and
   the edit-model types. Bridge and MCP work then proceed in parallel, with the
   MCP side against a **recorded-fixture mock bridge** (replay real
   `export_xml` and verb responses captured from FCP).
2. **One live FCP is a shared, serialized resource.** Build a test rig early: a
   launcher that opens a scratch library, an `rpc` queue with per-test
   checkpoints, and auto-recovery (modal watchdog, relaunch on crash). Agents
   write against the mock and land behind the rig.
3. **Workstreams** (each is independently shippable, so they can run in parallel):

| # | Workstream | Language | Depends on | Done when |
|---|---|---|---|---|
| W1 | Bridge verbs + watchdog + timings | ObjC | contract | all verbs pass the rig; probes retired |
| W2 | Edit model: FCPXML ⇄ slices, IDs, fragments, WeftCut mapping | Python | contract | round-trips the reference edit losslessly |
| W3 | Eyes: frames, cut sheets, timeline/inspector screenshots, contact sheets | ObjC + Python | W1 `frames` | agent can answer "what's on screen" in one call |
| W4 | Agent verbs (place/animate/fade/trim/move/restack/roles/checkpoint) | Python | W2 | each verb dry-runs, writes as one undo, and verifies |
| W5 | Benchmark harness + nightly blind run + scoring | Python | W2, W3 | score posted per run |
| W6 | Skill (playbook, presets, conventions), refined from benchmark transcripts | Markdown | W4, W5 | 20–30 min target met |
| W7 | Ingest: import media headlessly, proxies, transcribe on import | ObjC + Python | W1 | footage folder → event, transcribed |

4. **Repo layout**: `bridge/` (ObjC verbs), `mcp/` (server, model, verbs),
   `scripts/weftcut_to_fcpxml.py` (converter/oracle), `bench/` (scorers),
   `tools/probe/` (discovery harness + `recipes/`, the working reference for
   each private call until it becomes a bridge verb; delete a recipe once its
   verb lands), `tests/fixtures/fcpxml/` (encoding fixtures).
5. **Discovery is cheap; use it.** New private behavior is found with
   `dev.probe` (hot-loaded dylibs, no relaunch) in minutes. Every discovery
   becomes a verb plus a rig test the same day, or it gets thrown away.

## Constraints and known sharp edges

These are kept from the earlier case studies and the spike.

- **Patching**: the App Store build is copied to `~/Applications/FCPBridge`,
  dylib injected, re-signed (`disable-library-validation`, `get-task-allow`).
  Every FCP update needs `make patch` plus a selector smoke test (list every
  selector we call; fail loudly if one disappears). Swift-only internals
  (`Interchange.framework`) can't be swizzled; everything we use is ObjC.
- **Introspect, don't decompile**: the ObjC runtime (`class_copyMethodList`,
  type encodings) exposes the surface. `strings` on Flexo finds selectors.
- **Main thread only**: all model access goes through the main queue. An
  off-main Swift trap kills FCP, and any modal blocks the bridge (so every
  verb has a watchdog).
- **Libraries**: create them as `PEDocument`, and never call `saveDocument:`
  (it raises the "file changed by another application" modal). FCP saves
  through its catalog.
- **FCPXML import quirks**: `srcEnable` is dropped (use
  `audio-channel-source enabled="0"`); keyframe `interp` on transform params is
  dropped (use channel tangents, or bake); a `timeMap` must cover the clip's
  `start` or the clip is silently dropped; pasteboard XML needs **top-level**
  clips (not inside an `<event>`).
- **Compare times as rationals**: FCP renormalises every time to the sequence
  timescale on import (`10503000/90000s` → `700200/6000s`). Never diff FCPXML
  as text.
- **Return values aren't contracts**: verify by re-reading state. (An import
  once returned `NO` and had in fact succeeded.)
- **Retime anatomy**: `timept@time` is retimed clip time and `timept@value` is
  source time; `smooth2` + `inTime`/`outTime` are the ease handles. The clip
  `start` is in retimed time.
- **Don't bake dense keyframes into FCP**: 32k baked keys made the project's
  `export_xml` take 20.7 s (against ~100 ms otherwise), and import 18–27 s.
  Write authored keys and set exact tangents live instead.
- **Chapter markers without `posterOffset` crash FCP** when drawn or clicked
  (invalid time → NaN layer position → uncaught exception). The validator rejects them.
- **Probe code: never send a void-returning selector through an id-returning
  call.** ARC retains the garbage "result" and FCP segfaults. Use `CALLV*`.
- **Paste side effect**: a paste also adds a browser clip to the target event.
  Clean it up, or paste through a hidden scratch event.

## Out of scope (for now)

- **Motifs** (animated overlays): build them in a separate project (e.g.
  Remotion), render ProRes 4444 with alpha, import as a clip. No FCP-side
  work beyond import.
- **Tacks** (review annotations): would map neatly to FCP to-do markers. Nice,
  not critical.
- **WeftCut engine features** (its compositor, frame interpolation, its export
  path): FCP owns these.
