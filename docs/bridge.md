# Bridge contract (implemented in `bridge/FCPBridge.m`)

The dylib exposes JSON-RPC 2.0 over TCP `127.0.0.1:9876`, one JSON object per
line. Seven verbs — responder-chain / `FFEditActionMgr` paths only, no runtime
introspection surface. All timeline reads/writes run on the main thread.

| Method | Params | Returns |
|---|---|---|
| `system.version` | — | `{ fcp, bridge }` |
| `timeline.clips` | — | `{ sequence_name, fps, playhead_s, duration_s, clips: [{id, index, class, name, lane, container?, timeline_start_s, duration_s, trim_start_s, selected, media_path?}] }`. `id` (`clip_N`) is stable for the session (handle-backed, strongly retained); containers (compound/storyline/multicam) are emitted addressably with `container: true` plus their members one level deep |
| `timeline.select` | `{id}` | Membership re-verified against the live timeline (stale IDs refuse); seeks inside the clip, selects, and confirms the selection IS that object — a wrong-landing select is deselected and refused, never acted on. Failures: `unknown id`, `stale id`, `cannot resolve position`, `landed on X instead` |
| `timeline.undo` / `timeline.redo` | — | `{ action, status, actionName }` via libraryDocument undoManager |
| `timeline.release_handles` | — | `{ released: true }` — drops the ID store |
| `timeline.action` | `{action}` | allowlist: `blade bladeAll delete cut copy paste selectClipAtPlayhead selectAll deselectAll addMarker addChapterMarker nextEdit previousEdit trimToPlayhead openCompound timelineBack` → `action:` selector on the timeline module, sender=nil. Step-in recipe: `timeline.select {id of container}` then `openCompound`; step-out: `timelineBack` |
| `playback.position` | — | `{ t_s, fps, duration_s }` |
| `playback.seek` | `{t_s}` | `{ t_s, status }` via `setPlayheadTime:` (600 kHz CMTime) |

Cut planning, silence detection, transcription and verification live in
`mcp/server.py` (testable without FCP). The bridge never parses media.

## Time mapping

`timeline.clips` carries everything Python needs to map file time to timeline
time: `media_path` (via `media.originalMediaURL` →
`clipRef.assets[].originalMediaURL` → `assetMediaReference.resolvedURL`),
`timeline_start_s` (via `effectiveRangeOfObject:`, fallback `anchoredOffset`),
`trim_start_s` (via `unclippedRange.start`).

`file_s -> timeline_s = timeline_start_s + (file_s - trim_start_s)`

## Injection (for `patch/patch_fcp.sh`)

`insert_dylib --inplace @executable_path/FCPBridge.dylib` on the copied
binary (dylib sits in `Contents/MacOS/`), then re-sign dylib + bundle with
`disable-library-validation` (ad-hoc OK). Apple's nested frameworks keep
their signatures.
