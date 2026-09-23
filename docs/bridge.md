# Bridge contract (implemented in `bridge/FCPBridge.m`)

The dylib exposes JSON-RPC 2.0 over TCP `127.0.0.1:9876`, one JSON object per
line. Seven verbs — responder-chain / `FFEditActionMgr` paths only, no runtime
introspection surface. All timeline reads/writes run on the main thread.

| Method | Params | Returns |
|---|---|---|
| `system.version` | — | `{ fcp, bridge }` |
| `timeline.clips` | — | `{ sequence_name, fps, playhead_s, duration_s, clips: [{id, index, class, name, lane, container?, parent?, timeline_start_s, duration_s, trim_start_s, trim_candidates?, selected, media_path?}] }`. `id` (`clip_N`) is stable for the session (handle-backed, strongly retained); containers (compound/storyline/multicam) are emitted addressably with `container: true` plus their members one level deep. Items anchored directly to a media clip (connected audio etc.) appear with `lane: "nested"` + `parent` id — registered IDs, selectable like any clip. `trim_candidates` (bridge ≥0.2.0) maps each sane source-space probe to its start in seconds. THE trim mechanism (found 2026-09-23 after ruling out `unclippedRange`/`sourceRange`/spine mirrors — all report full-media or timeline space): **`parentToLocalOffset` maps spine time to source time, so `trim = timeline_start + offset`** (`parentToLocalOffset-derived` candidate, per-clip bound-checked against the full media length). Validated live on 129 clips: zero overlapping file windows, ordered, tiling exactly [0, 1819.718]. Python takes the nonzero consensus and blocks on disagreement (see `mcp/server.py::_effective_trim`) |
| `timeline.debug_ranges` / `debug_refs` / `debug_spine` / `debug_methods` / `debug_class` / `debug_getters` / `debug_hops` / `debug_convert` | `{id}` (or `{class}` for debug_class) | Read-only per-clip/per-class probes used to reverse-engineer the trim API. Same membership check as `timeline.select`. `debug_getters` SWEEPS AND CALLS no-arg getters — one of them (`detachAudio`) creates a connected audio item as a side effect (3 litter items, since enumerated as `nested`). Never use getter sweeps on a precious timeline; the production path uses only `parentToLocalOffset` + `timelineRange` + `unclippedRange` |
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
`trim_start_s` (`timeline_start_s + parentToLocalOffset`, bound-checked per
clip against the full media length from `unclippedRange`; falls back to the
nonzero consensus over source-space range selectors when present).

`file_s -> timeline_s = timeline_start_s + (file_s - trim_start_s)`

## Injection (for `patch/patch_fcp.sh`)

`insert_dylib --inplace @executable_path/FCPBridge.dylib` on the copied
binary (dylib sits in `Contents/MacOS/`), then re-sign dylib + bundle with
`disable-library-validation` (ad-hoc OK). Apple's nested frameworks keep
their signatures.
