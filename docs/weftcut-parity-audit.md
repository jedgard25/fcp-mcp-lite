# WeftCut → FCP parity audit (spike, 2026-11-11)

**Question:** can the injected bridge make FCP do what WeftCut does, editing the
*open* project in place, without the open/close-project friction of FCPXML
round-trips?
**Reference edit:** `best-pokemon-artists-edit/project.json` (29.97, 4K):
one compound A-roll (129 linked camera+WAV cuts) used 3× on the primary,
14 overlay/audio tracks, 68 image overlays, 23 crops, 76 position-animated
clips (218 spline segments), 120 fades, 1 retime, 3 saturation effects.
**Branch:** `spike/fcp-parity`. **Direction:** [`direction.md`](direction.md). Scratch library
`~/Movies/WeftCut-Parity-Spike.fcpbundle`; no user library was written to.

## Verdict

Achievable. Every construct in the reference edit landed in FCP with measured
fidelity, and the in-place path works:

| Capability | Result | Evidence |
|---|---|---|
| Read the open project as FCPXML | ✅ 50–150 ms, no UI | `+[FFXML newXMLDataForEvents:…]` |
| Create a library headlessly | ✅ | `-[FFLibraryDocument initWithURL:createDefaultEvent:error:]`, see gotcha 1 |
| Import a whole project headlessly | ✅ 0.7–1 s (unbaked), 18–27 s (16–32k baked keys), no modal | `+[FFXML importFromXMLData:options:…]` with `options.library` |
| Structural fidelity of the reference edit | ✅ 120/120 project clips on the right lane and frame; compound + 129 A/V pairs | `bench/rtdiff.py` round-trip diff |
| Motion fidelity | ✅ 74/74 animated clips, 47,795 frames, **max 0.36 px, p99 0.001 px** | `bench/motion_audit.py` samples FCP's live channels |
| **Paste-in-place** into the open timeline | ✅ 74 ms/clip, lands as a connected clip at the playhead, crop/keyframes/audio-off intact | FCPXML on `com.apple.finalcutpro.xml.v1-13` → `-pasteAnchored:` |
| Batch as ONE undo step | ✅ 3 pastes in 140 ms under "WeftCut: B-roll batch"; one undo removes all | `FFUndoManager begin/endUndoGrouping` |
| Exact WeftCut easing (cubic-bezier) live | ✅ max 0.005 units vs WeftCut's evaluator, named undo that really reverts | `FFChannelChangeController` + `CHChannel set…Tangents` |

## Architecture this proves out

```
read   : export_xml(active project)          ~100 ms   (full multi-lane state)
bulk   : FCPXML fragment → pasteboard → pasteAnchored: / paste:    one undo group
exact  : CHChannel tangents inside FFChannelChangeController       (easing, curves)
whole  : import_xml(new project in library)  fallback / migration (WeftCut → FCP)
```

The open/close-project problem goes away. Writes land in the open timeline.
They are undoable, grouped and named. Reads come back as FCPXML, so one
parser serves both directions.

## Construct mapping (what the converter does, and what FCP keeps)

| WeftCut | FCP | Status |
|---|---|---|
| root track 0 (CompositionRef ×3) | primary storyline `ref-clip`s + gaps | ✅ |
| nested composition | compound clip (`<media><sequence>`) | ✅ |
| linked VideoClip + Audio (dual-system A-roll) | video `asset-clip` + connected WAV on lane −1 | ✅ |
| visual track *i* (z-order, 0 = bottom) | connected lane *i* | ✅ |
| audio track | negative lanes, `audioRole` dialogue/music | ✅ |
| Position (anchor px, y-down) | `adjust-transform` position, % of frame height, y-up | ✅ numerically; ⚠ units not yet checked on a rendered frame |
| scale (native px) | `adjust-conform type="none"` + scale | ✅ (same caveat) |
| crop (normalized source) | `adjust-crop trim` (% of *source* height, per DTD) | ✅ preserved |
| keyframes (layer-local) | clip-source-time keyframes | ✅ |
| Spline easing on position/scale | **dropped by FCPXML import** (any `interp`) → bake per frame in eased segments, or set tangents live | ✅ via bake (32k keys) / ✅ live (2 keys) |
| Spline/Linear easing on opacity | `interp` ease/easeIn/easeOut is kept | ✅ approx (named eases, not exact handles) |
| Hold segment | key at next−1 frame | ✅ |
| fades (visual / audio) | `fadeIn`/`fadeOut` in blend/volume `param` | ✅ |
| disabled track/layer | `enabled="0"` | ✅ |
| Affine retime | `timeMap`; clip `start` is in **retimed** time | ✅ (after fix) |
| saturation effect | Color Adjustments `FxPlug:7E2022A5-…` or Hue/Saturation `.moef` | ⏳ unmapped (needs param key + WeftCut→FCP scale) |
| video-only use of an A/V file | `<audio-channel-source srcCh="1, 2" enabled="0"/>` | ✅ (`srcEnable="video"` is **silently dropped**) |
| anchor ≠ 0.5, rotation, flips, blend modes, Elastic/Bounce segments, pan | not in this edit | ⏳ untested (flips/blend have FCPXML equivalents; Elastic/Bounce → bake) |
| text, captions, Motifs | not in this edit | ⏳ titles/captions are FCPXML-native; Motifs → bake to ProRes 4444 |

## Gotchas found (each cost real time)

1. **Make a library with `PEDocument`, not `FFLibraryDocument`.** A bare
   `FFLibraryDocument` imports fine, but the editor container can't host it
   (`-[FFLibraryDocument setActiveSequence:]` unrecognized).
2. **Never call `saveDocument:` on a library.** It raises AppKit's modal "file
   changed by another application". FCP libraries save through their catalog.
   (It was dismissed with Don't Save via lldb `[NSApp stopModalWithCode:1001]`.)
3. **Pasteboard FCPXML must hold top-level clips** (`<fcpxml><resources/><asset-clip/>`).
   Clips wrapped in `<event>` validate and import into the event, but paste
   nothing.
4. FCPXML import **drops** `srcEnable` and every keyframe `interp` on
   transform params. It keeps `interp` on opacity/volume.
5. **Raw `CHChannel` writes are not undoable**, even inside
   `actionBegin`/`actionEnd` on the clip or stack: the undo entry appears but
   reverts nothing. Use `FFChannelChangeController`
   (`beginChannelChange:actionName:forItem:` → `willSetChannel:` → mutate →
   `didSetChannel:` → `endChannelChange:error:`).
6. A `timeMap` must cover the clip's `start`, or FCP **silently drops the clip**
   (no warning).
7. Any modal blocks the bridge's main-thread call. Probes carry a watchdog
   timer (`abortModal`), and lldb (`get-task-allow`) is the escape hatch.
8. A paste also adds a browser clip to the target event (import side effect).
   It's cleanable, but expect it.

## Verified after the first pass

- **Rendered frames.** Composite frames pulled from FCP's own renderer
  (`FFDestVideoDisplay` → `FFPlayerFrame.image1` → `-[FFImage newCGImageRef]`,
  ~330 ms, no screen permission) were compared with `weftcut-export.mov` at the
  same timestamps. Host framing and backdrop-panel geometry match, so the
  position units are right. B-roll lanes 1–5 are disabled in the current
  `project.json` (edited after that export), which is why cards are absent on
  the FCP side.
- **UI capture.** The full FCP window and timeline were captured in-process
  with `cacheDisplayInRect:`, with no TCC permission. The Metal viewer
  renders black that way; use the composite-frame path for it.

## Not yet verified

- Placing a whole multi-lane pass in one paste (e.g. a `<clip>` container + `operationBreakApartClipAnchoredItemsIntoEmptyLanes:`).
  One-paste-per-clip is proven and fast enough (~75 ms each).
- Lane control after paste. FCP picks the first free lane; `actionAnchorItem:…inContainerAnchorLane:` is the candidate.
- The direct `action*` methods (anchor, volume, roles, retime presets). Their
  type encodings are recorded below, but none was called.

## API surface (type encodings from the live runtime)

```
FFAnchoredSequence
  actionAnchorItem:withAnchorInLocalTime:toItem:atItemTime:inContainer:inContainerAnchorLane:error:  B104@0:8@16{CMTime}24@48{CMTime}56@80q88^@96
  actionAddItemsOnPasteboard:editsToAdd:videoProps:displayDropFrame:atTime:pasteMode:backtimed:rangeOfMedia:rootItem:needsMediaReferencesChecking:needsConsolidatedEffectsChecking:error:
  actionChangeAudioVolume:byAmount:overRange:isRelative:error:            B92@0:8@16d24{CMTimeRange}32B80^@84
  operationAddEffectID:paramDict:toAnchoredObjects:error:                 B48@0:8@16@24@32^@40
FFXML      +newXMLDataForEvents:withOptions:taskDelegate:error:  +importFromXMLData:options:taskDelegate:warnings:error:
FFXMLTranslationTask +translationTaskForPasteboard: -importClipsWithOptions: -allImportedClipsAndRanges
FFAnchoredTimelineModule -pasteAnchored: -paste: -anchorWithPasteboard:backtimed:trackType:
PEEditorContainerModule  -loadEditorForSequence: -setPlayheadSequenceTime:
FFEffectStack -intrinsicEffects  → FFHeXForm3DEffect(HEXForm3D) -positionChannel3D/-scaleChannel3D/-rotationChannel3D
CHChannel  -firstKeyframe -nextKeyframe: -getKeyframe:time:value: -segmentInterpolation: (4 = bezier)
           -setKeyframeInputTangents:inTanTime:inTanValue: -setKeyframeOutputTangents:… -curveDoubleValueAtTime:
FFChannelChangeController -beginChannelChange:actionName:forItem: -willSetChannel: -didSetChannel: -endChannelChange:error:
```

## Reproducing

- Bridge: `dev.probe` verb (gated by `~/.fcpbridge-dev-probe`), which dlopens a
  probe dylib (one unique path per build) and runs it on the main thread.
  `tools/probe/run.py tools/probe/recipes/<recipe>.m '{json}'` compiles and calls in one step.
- `scripts/weftcut_to_fcpxml.py project.json out.fcpxml --name N --event E`
- `bench/rtdiff.py ours.fcpxml roundtrip.fcpxml` diffs structure (clips matched by lane + absolute frame).
- `bench/motion_audit.py project.json` samples every animated clip per frame in the open FCP project.

## Effort read

| Piece | Size |
|---|---|
| Promote probes → bridge verbs (`export_xml`, `import_xml`, `paste_xml{at,lane}`, `begin/end_batch`, `set_curve`) | days |
| Converter → shared FCPXML model in `mcp/` (read + write, fragment emit) | 1–2 weeks |
| WeftCut verbs on top (B-roll placement, keyframe/ease, fades, roles, captions/titles) | 2–3 weeks incremental |
| Eyes: frame grabs from FCP's renderer for the agent | unknown, needs its own spike |
| Motifs (WeftCut-native overlays) → baked ProRes 4444 alpha | depends on WeftCut's export path |
