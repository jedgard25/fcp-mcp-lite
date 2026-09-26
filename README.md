# fcp-mcp-lite

Minimal agent bridge for Final Cut Pro. Extracts only the load-bearing core
from [SpliceKit](https://github.com/elliotttate/SpliceKit) (MIT):

- **patch** — copy App Store FCP, inject a dylib, re-sign, launch
- **bridge** — ObjC dylib, TCP JSON-RPC on `127.0.0.1:9876`, plus an in-process
  **Window > Transcript** panel (WKWebView on the `transcript-ui` service —
  no Workflow Extension SDK / `.appex` needed because the dylib already
  runs inside FCP)
- **mcp** — stdio MCP server with story, word, silence, and timeline tools
- **tools** — vendored `silence-detector.swift` + `parakeet-transcriber`, run as
  **subprocesses** (never in-process, so a crash can't take FCP down)

Non-goals: command palette, plugins, mixer UI, captions UI, BRAW/VP9, debug
toolkit. All of that is SpliceKit upstream if you ever want it.

## Layout

```
patch/patch_fcp.sh      copy → inject → re-sign → launch
mcp/server.py           stdio MCP server → TCP bridge
mcp/requirements.txt    pinned deps
mcp/log.py              JSONL call log (every tool call, args, RPCs, timing)
tools/silence-detector.swift   (vendored from SpliceKit, MIT)
tools/parakeet-transcriber/    (vendored from SpliceKit, MIT)
scripts/mcp-doctor.sh   venv + bridge reachability checks
```

## Quick start

```sh
make mcp-setup    # venv at ~/.venvs/fcp-mcp-lite
make patch        # patch + launch FCP (prompts before touching anything)
make mcp-doctor   # verify venv + bridge on :9876
make ui           # transcript service on :8765 (leave running)
```

Then point opencode at the MCP server (see `mcp/client-config.json`).

In patched FCP: **Window > Transcript** (⌘0) opens the editor panel. It shows
bridge/MCP status until a transcript exists, then the cards.

## Design rules (why SpliceKit broke, and this won't)

1. **Identity, not time.** Clips carry session-stable IDs (`timeline.select`
   confirms membership + selection before anything acts). Timestamps are
   resolved at commit, never stored in a plan.
2. **Validate whole, then cut serially.** Destructive ops compute the full cut
   list first, split it at primary clip edges, and verify each cut. FCP does
   not provide one atomic transaction for the batch.
3. **Revert, never wrong-cut.** A select miss auto-reverts its blades; a stale
   ID refuses; a stale transcript (timeline rippled since transcribe) refuses.
4. **Verify after write.** Merged ranges are split at primary clip edges;
   each delete must ripple by its requested piece before the next runs.
   Story commits also check that planned lines disappeared.
5. **`dry_run` everywhere** (default on). Rehearse the exact op, commit nothing.
6. **Out-of-process inference.** Parakeet/silence run as CLIs over source
   files. Transcript cache is JSON on disk keyed by file+model — a read never
   spawns an engine. Clips whose media moved are skipped, not fatal.
7. **JSONL or it didn't happen.** `~/.local/share/fcp-mcp-lite/calls.jsonl`
   records every call. `tail -f` it via `make logs`.

Known v0 limits: a batch is N undo entries, not one (`undo_steps` counts
issued blade/delete actions, but FCP's undo manager can differ after a
failed action; inspect before a bulk undo). Blades at existing edit points
are skipped so boundary-aligned cuts cost 1 entry. Cuts are capped at 40
primary clip pieces per call by default and return `pending` for continuation;
single-user assumed (concurrent hand-editing skews verify counts);
cut targeting is primary-storyline-first, connected timelines often miss
(guarded, never wrong).
Word/silence ops are file-anchored: transcript words store source-file
times and re-resolve against the live timeline on every call, so chained
cuts never go stale; `delete_words(ranges=[[start, count], …])` cuts a
whole retake sweep in one validated batch; silence scans cover every
on-disk primary clip (one detector run per unique file).
