# fcp-mcp-lite

![The Transcript panel editing a sequence inside Final Cut Pro](preview-image.jpg)

Agent-native MCP for Final Cut Pro. It drives a patched FCP over a local
JSON-RPC bridge, with no Workflow Extension SDK, no `.appex`, and no private
framework linking beyond the dylib the patcher injects.

**Where this is going:** [`docs/direction.md`](docs/direction.md). The goal is a
blind Claude session building a full multi-lane edit (transcript rough cut,
animated B-roll, VO, fades) in FCP in 20–30 minutes, editing the open project
in place and looking at its own work. The evidence that FCP can hold such an
edit 1:1 is in [`docs/weftcut-parity-audit.md`](docs/weftcut-parity-audit.md).
The verbs below are today's shipped surface (transcribe, cut, audit).

It is a small rewrite built on [SpliceKit](https://github.com/elliotttate/SpliceKit)
(MIT) — its copy-→-inject-→-re-sign approach and bridge patterns were the
starting point, and only the load-bearing core was kept.

## Components

```
patch/patch_fcp.sh             copy → inject dylib → re-sign → launch
bridge/FCPBridge.m             ObjC dylib: TCP JSON-RPC 2.0 on 127.0.0.1:9876
                               + in-process Window > Transcript panel
mcp/server.py                  stdio MCP server (the verbs below) → TCP bridge
extensions/transcript-ui/      slice editor + local HTTP service (the panel page)
tools/silence-detector.swift   vendored (MIT)
tools/parakeet-transcriber/    vendored (MIT), on-device ASR
```

Transcription and silence detection run as **subprocesses over source files**, so
a crash can't take FCP down. The transcript is a JSON cache on disk keyed by
file+model; a read never spawns an engine.

## Design intent

1. **Identity, not time.** Clips carry session-stable ids and word ops carry
   file-relative word indices. Timestamps are resolved against the live timeline
   at commit, never stored in a plan, so chained cuts never go stale.
2. **Validate whole, then write as one batch.** Destructive ops compute the full
   cut list first and verify after writing. Today's cut verbs still issue N
   undo entries; `FFUndoManager` grouping (proven, see the audit) makes a batch
   one undo step, and new verbs use it.
3. **Revert, never wrong-cut.** A select miss auto-reverts issued blades; a stale
   id refuses; a stale transcript refuses. Blocking beats guessing.
4. **`dry_run` everywhere** (on by default). Rehearse the exact op, write nothing.
5. **Verify after write.** Duration and clip counts are re-read and checked, not
   assumed. `verify_action` re-reads timeline + playhead on demand.
6. **Out-of-process inference.** Parakeet/silence are CLIs. Clips whose media
   moved are skipped, not fatal.
7. **JSONL or it didn't happen.** Every call, arg and RPC lands in
   `~/.local/share/fcp-mcp-lite/calls.jsonl` (`make logs`).

## Tools

**Timeline / audit**

| Tool | Does |
|---|---|
| `bridge_status` | bridge + MCP versions, reachability |
| `get_timeline` | bounded timeline window, or one clip by id |
| `get_playhead` | playhead time, fps, sequence duration |
| `verify_action` | re-read timeline + playhead after a batch |
| `select_clip` | select a clip by id (membership confirmed) |
| `undo`, `redo` | FCP undo manager, N steps |

**Transcription**

| Tool | Does |
|---|---|
| `transcribe` | on-device Parakeet over the first primary clip; cached |
| `get_transcript` | cached transcript as sentences or words |
| `get_story` | transcript as stable story lines (`L0020`) — the editing UI |

**Cutting — timeline seconds**

| Tool | Does |
|---|---|
| `detect_silences` | AVFoundation silence spans mapped to the timeline |
| `remove_silences` | cut every silence, padded for breath |
| `cut_spans` | cut explicit spans, chunked |
| `apply_cut_list` | keep ranges, drop the rest, close gaps |

**Cutting — words / story**

| Tool | Does |
|---|---|
| `delete_words`, `move_words`, `split_words` | word-range primitives |
| `delete_lines`, `move_line`, `move_clip` | story-line and clip reorder by id |
| `apply_story` | rough cut + reorder in one verb from a keep-list |
| `choose_take`, `choose_takes` | resolve retake groups, one or in batch |
| `review` | story-decision gate: open takes, fragments, duration |

## Quick start

```sh
make mcp-setup    # venv at ~/.venvs/fcp-mcp-lite
make patch        # patch + launch FCP (prompts before touching anything)
make mcp-doctor   # verify venv + bridge on :9876
make ui           # transcript service on :8765 (leave running)
make test         # stubbed tests, no FCP required
```

Point your MCP client at the server (see `mcp/client-config.json`). In patched
FCP, **Window > Transcript** (⌘0) opens the editor panel; it shows service/bridge
status until a transcript exists, then the cards. The panel loads the same page
as http://127.0.0.1:8765 and edits commit immediately through the same
validation and log as the agent verbs.

## Size & contributing

The core (`patch/` + `bridge/` + `mcp/`) is ~4,500 lines; about 1,000 more are
the vendored `silence-detector.swift` and Parakeet transcriber. It is growing
into a full editing MCP along the workstreams in
[`docs/direction.md`](docs/direction.md). Keep the bridge a set of dumb, typed,
main-thread verbs, and keep the planning in Python.

## Current limitations (each is a direction.md workstream)

- Today's shipped verbs work clip-by-clip on the primary storyline. The
  multi-lane edit model (slices, lanes, stable IDs) is W2. The FCPXML
  converter (`scripts/weftcut_to_fcpxml.py`) already holds the mapping.
- Today's cut verbs issue **N undo entries** per batch (`undo_steps` counts
  them). Grouped undo is proven, but these verbs don't use it yet.
- Cuts are capped at 40 primary-clip pieces per call by default and return
  `pending` for continuation.
- Vision (composite frames, cut sheets, timeline screenshots) is proven (`tools/probe/recipes/composite_frames.m`,
  `snapshot_ui.m`, `tools/cut_sheet.py`) and not yet exposed as verbs (W3).
- `dev.probe` (hot-loaded discovery dylibs) is gated by `~/.fcpbridge-dev-probe`
  and is for development only.
