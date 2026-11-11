---
name: fcp-edit
description: Edit video in Final Cut Pro through the patched-FCP bridge (fcp-mcp-lite). Use when the user wants footage cut, rough-cut, synced to external audio, transcribed, or reviewed in FCP — building projects headlessly via FCPXML, driving the open timeline, and checking your work with vision (composite frames, timeline screenshots, cut sheets).
---

# Editing in Final Cut Pro with fcp-mcp-lite

You drive a **patched copy of Final Cut Pro** that has a JSON-RPC bridge injected
into its process. You never click the UI. You **build** edits as FCPXML and
import them headlessly, **read** the open project back as FCPXML, and **look** at
the result through FCP's own renderer. Repo: `~/Projects/fcp-mcp-lite` (below,
`$R`). Every path below is relative to `$R` unless absolute.

Ground rules:
- **Never write into the user's existing libraries or projects** unless they ask.
  By default each job gets a **brand-new library** (`make_library.m`), e.g.
  `~/Movies/<Job Name>.fcpbundle`. To change a result, import a new version
  (`… v2`) into the same library; never overwrite. Leave the library open when
  you're done so the user can review it.
- **Reference material**: reading the user's earlier edits (open libraries,
  exported FCPXML) is fine and often useful for a similar video, e.g. matching
  pacing, lane conventions or take choices. Don't if the user says the run is
  **blind** (a test or benchmark); then don't open or read anything beyond the
  media they pointed you at.
- **Verify by re-reading and looking**, never by trusting a return value.
- **Don't guess at FCP internals.** If something below doesn't cover it, stop
  and report rather than improvising private calls.

## 0. Preflight (1 minute)

```bash
cd ~/Projects/fcp-mcp-lite
pgrep -f "FCPBridge/Final Cut Pro.app" >/dev/null && echo "FCP running"
test -f ~/.fcpbridge-dev-probe && echo "probes enabled"
printf '{"jsonrpc":"2.0","id":1,"method":"system.version","params":{}}\n' | nc -w 3 127.0.0.1 9876
```
If FCP isn't running: `open ~/Applications/FCPBridge/Final\ Cut\ Pro.app`, wait 30 s.
If the bridge doesn't answer, stop and report.

Create the job's library (it must not exist yet):
```bash
tools/probe/run.py tools/probe/recipes/make_library.m "{\"path\": \"$HOME/Movies/My Job.fcpbundle\"}"
```
Its display name (the file name without `.fcpbundle`) is the `libraryName`/`lib`
for the recipes below. Keep your working files in a folder of your own, not
next to the media.

## 1. Tool reference

### FCP calls (probe recipes)
`tools/probe/run.py tools/probe/recipes/<recipe>.m '<json args>'` compiles a small
dylib and runs it inside FCP on the main thread. It prints JSON. Set `T=<seconds>`
for long calls (default 120).

| Recipe | Args | Does |
|---|---|---|
| `make_library.m` | `{"path": "/abs/New.fcpbundle"}` | Creates and opens a new, empty library. Refuses if the path exists. |
| `open_library.m` | `{"path": "/abs/Lib.fcpbundle"}` | Opens an existing library (same as File › Open Library), e.g. a reference edit the user points you to. |
| `close_library.m` | `{"name": "Display Name"}` | Closes a library the FCP way (FCP saves it). Only for your own job library, and only if asked. |
| `import_xml.m` | `{"xml": "/abs/file.fcpxml", "libraryName": "<your job library>", "watchdog": 15}` | Imports an FCPXML file (its `<event>` and `<project>`) into that library. No dialogs. Empty `warnings` = clean. `Invalid argument` almost always means the library isn't open. |
| `open_project.m` | `{"lib": "<your job library>", "event": "<event>", "name": "<project>"}` | Opens that project in the timeline (needed before frames/screenshots). |
| `export_project.m` | `{"lib": "...", "event": "<event>", "out": "/abs/out.fcpxml"}` | Exports an event as FCPXML: the ground truth of what FCP holds. |
| `composite_frames.m` | `{"dir": "/abs/dir", "times": [12.5, 40], "width": 960}` | Renders what the viewer shows at each timeline second → `comp_<t>.jpg` (~0.35 s each). The **open** project only. |
| `snapshot_ui.m` | `{"dir": "/abs/dir"}` | Screenshots FCP's whole window → `fcp_window.png` (timeline, clip names, disabled clips, inspector). Downscale it before viewing: `sips -Z 1400 fcp_window.png --out small.png`. |
| `find_clip.m` | `{"name": "<clip name>"}` | Shows the open project's clip tree (names, nesting). |
| `visible_windows.m` | `{}` | Lists windows/modals. Use it if a call hangs or something looks stuck. |

Bridge verbs (raw JSON-RPC on `127.0.0.1:9876`, one line per request):
`timeline.undo`, `timeline.redo`, `playback.seek {"t_s": 12.0}`, `playback.position`.

### Media and analysis tools (run outside FCP, so they can't crash it)
| Tool | Use |
|---|---|
| `python3 tools/align_audio.py CAM.MOV [CAM2 …] --audio A.wav [B.wav …]` | Pairs camera files with external/processed audio and finds the offset. Output `best[]`: `{camera, audio, offset_s, score}`, with **`audio_time = camera_time + offset_s`**. A score ≳ 0.5 means a confident match; ≈ 0.25 means not the same recording. ~3 s for 30 min of media. |
| `python3 tools/transcribe.py WAV -o words.json` | On-device ASR (Parakeet): splits at silences and runs **3 passes with different chunk seams**, then takes the union, so false starts aren't dropped (a single pass drops them). Output is `[{word, startTime, endTime, confidence}]` in WAV seconds. ~11 s for 17 min of audio. Cached. **Transcribe the processed WAV**, not camera audio. Never call the raw `parakeet-transcriber` on a whole long file. |
| `python3 tools/takes.py --source /abs/CAM.MOV=words.json:OFFSET [--source …] --script script.md -o takes.json --text` | The **take sheet**: sentences in camera time, matched to script lines, retakes grouped, each marked `take` / `fragment` (false start) / `offscript`, with flags `filler` / `repeat` / `no_end`, plus a suggested pick per line (`»`). Lists `UNMATCHED SCRIPT LINES` (content nobody delivered or the matcher missed) and off-script chatter. Read the `--text` view instead of raw transcript. |
| `python3 tools/build_roughcut.py --takes takes.json --source /abs/CAM.MOV=/abs/WAV:OFFSET [--source …] --project P --event E -o cut.fcpxml [--drop S012,S044] [--add S013]` | Writes the rough-cut FCPXML (timecode, sample-accurate WAV sync, muted camera audio, padding into silence without clipping neighbours, contiguous picks merged, clip IDs and section groups in notes, chapter markers per section) and **validates it**. Change picks with `--drop`/`--add` sentence ids. **Don't hand-write FCPXML for a rough cut.** |
| `python3 tools/cut_sheet.py export.fcpxml sheet.jpg [--only 3,11,44] [--from N --count N] [--width 900]` | Per cut: head and tail frames, plus the waveform of the connected audio ±handle, with red lines at the edit points. A red line inside a waveform blob = a cut mid-word. Use `--only` with the cut indices story_check flagged; full pages are expensive to look at. |
| `python3 tools/story_check.py export.fcpxml --words AUDIO.wav=words.json [--words …] [--text]` | Reads the cut back as **text in timeline order** (from each clip's connected-audio range, so no offsets needed) and flags **DUPLICATE** sentences (a retake left in, or a cut that ends on the line the next cut starts) and **STUTTER** (the same word on both sides of a cut, usually the first word of a retake left on a tail). Exit code 1 = something flagged. `--text` prints the whole kept transcript with cut markers. |
| `ffprobe` / `ffmpeg` | Durations, timecode (`-show_entries stream_tags=timecode`), spot frames. |
| `python3 tools/reconcile.py before.fcpxml after.fcpxml` | After the user edits by hand: diffs two exports by clip ID (from notes) and lists SPLIT / DELETED / TRIMMED / MOVED / RELANED / DISABLED, plus ADDED (no ID = the user's own clip). Run it before touching a project the user has edited, and treat their changes as decisions. |
| `scripts/weftcut_to_fcpxml.py` | Reference converter (WeftCut project.json → FCPXML), and `tc_start_frames` for timecode. |

## 2. Writing FCPXML that FCP accepts

For rough cuts use `build_roughcut.py`; it applies every rule below. Read this
section when you need FCPXML it doesn't cover (B-roll, titles, markers).
Template for an A-roll rough cut: camera video on the primary storyline, its
scratch audio muted, the synced processed WAV connected underneath:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.13">
<resources>
  <format id="r1" name="FFVideoFormat3840x2160p2997" frameDuration="1001/30000s" width="3840" height="2160" colorSpace="1-1-1 (Rec. 709)"/>
  <asset id="v1" name="CAM" start="{CAM_TC}" duration="{CAM_DUR}" hasVideo="1" hasAudio="1" format="r1"
         videoSources="1" audioSources="1" audioChannels="2" audioRate="48000">
    <media-rep kind="original-media" src="file:///abs/path%20encoded/CAM.MOV"/></asset>
  <asset id="a1" name="WAV" start="0s" duration="{WAV_DUR}" hasAudio="1" audioSources="1" audioChannels="2" audioRate="48000">
    <media-rep kind="original-media" src="file:///abs/path/WAV.wav"/></asset>
</resources>
<event name="{JOB EVENT}"><project name="{PROJECT}">
 <sequence format="r1" duration="{TOTAL}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k"><spine>
  <asset-clip ref="v1" name="{label}" offset="{TIMELINE_POS}" start="{CAM_TC + cam_in}" duration="{D}">
    <asset-clip ref="a1" lane="-1" offset="{CAM_TC + cam_in}" start="{cam_in + offset_s}" duration="{D}" audioRole="dialogue"/>
    <audio-channel-source srcCh="1, 2" enabled="0"/>
  </asset-clip>
  … one asset-clip per kept range, offsets back to back …
 </spine></sequence></project></event></fcpxml>
```

Rules. Each one was learned by FCP silently doing the wrong thing:
- **Timecode**: a camera file with embedded timecode has its asset `start` =
  that timecode (e.g. `00:22:19;25` drop-frame at 29.97 → `40195155/30000s`).
  Source times in a clip's `start` are **asset start + seconds into the file**.
  Get it with `tc_start_frames(path)` from `scripts/weftcut_to_fcpxml.py`
  (it returns frames: `start = frames * 1001/30000`). WAVs have no timecode, so their start is `0s`.
- **Time values are rationals**: `"N/30000s"`. Quantize video times to whole
  frames (`n*1001/30000s`). Audio-only clips may be sample-accurate (`N/48000s`),
  and FCP keeps those; use that for the WAV `start` so sync is exact.
- **A connected child's `offset` is in the parent's source time** (= the
  parent's `start` when they begin together).
- **Muting a clip's own audio**: `<audio-channel-source srcCh="1, 2" enabled="0"/>`
  as the **last** child. `srcEnable="video"` is silently dropped.
- Child element order matters (DTD): adjustments, then connected clips, then
  `audio-channel-source`. Validate before importing:
  `tools/validate_fcpxml.sh file.fcpxml` (FCP's own DTD; plain `xmllint --dtdvalid` can't take its path with spaces).
- **Chapter markers need `posterOffset="11/30s"`.** Without it FCP stores an
  invalid time and **crashes** when the marker is drawn or clicked. The
  validator rejects this.
- **IDs and groups go in the clip `<note>`** (`fcpmcp:id=C001 fcpmcp:group=Intro:9`).
  Custom `<metadata>` keys are dropped. Section colors come from the group
  hook (`bridge/hooks/group_colors.m`), not roles: don't invent roles for sections.
- After importing, **export it back and check** clip count, total duration and
  a few offsets. Compare times as fractions, never as text (FCP renormalizes).

## 3. Rough-cut method (talking head, multiple takes)

1. **Inventory**: list the media and durations; find and read any script.
2. **Sync**: `align_audio.py` over every camera file × every WAV. Pair each
   camera file with its best match only if the score is clearly above the
   others (≈0.9 vs ≈0.25 in practice). Note the offsets.
3. **Transcribe** each matched WAV with `transcribe.py` (run both in parallel).
4. **Take sheet**: `takes.py … --text`. The suggestion is the last clean,
   complete take per script line in script order. **Review, don't rubber-stamp**:
   - rows where the pick has `repeat`/`filler`/`no_end`, or where an earlier take
     is complete and clean (the presenter may have restarted for delivery, not words)
   - lines with `cov` < ~0.8 (part of the line may be in a neighbouring take)
   - `UNMATCHED SCRIPT LINES`: missing content, or an ad-lib that replaced it
   - off-script sentences that are actually content (ad-libs worth keeping)
   If the script and the delivery disagree, the delivery wins.
5. **Build** with `build_roughcut.py`, adjusting with `--drop`/`--add`. It
   validates; import into a new event; open the project.
6. **Audit** (section 4), fix with `--drop`/`--add`, rebuild as `v2`, repeat
   until `story_check.py` is clean.

## 4. Self-audit. Story first, then eyes; look at every image you make

1. **Story check (mandatory)**: `story_check.py` on the **exported** project.
   Fix every DUPLICATE and STUTTER, or justify it. Then skim `--text` against the
   script for missing beats. Frames and waveforms can't show a repeated
   sentence; in the first blind run, a duplicated intro line and two stray
   retake words passed the visual audit and only this caught them.
2. **Cut sheets only where needed**: `cut_sheet.py --only <flagged cut indices>`
   plus any cut you changed. Red edit lines must sit in silence. Don't page
   through every cut; it's the most expensive thing you can look at.
3. **Timeline screenshot** (`snapshot_ui.m`): project open, every cut on the
   storyline, WAV lane under each cut, nothing dimmed or missing.
4. **Composite frames** (`composite_frames.m`, width 640) at ~6 times: right
   person, framing, no black or "Missing File" frames.
5. **Sync spot-check** on 2 cuts: speech in the waveform lines up with the
   transcript words at the edit. If it's consistently early or late, the offset
   sign is wrong.

## 5. Report back

Say what you built and where (library / event / project). Give the source
pairing and offsets, the number of cuts and the total duration, which takes you
chose where there were several (and why), anything you were unsure about, and
the audit evidence (paths to the screenshots/sheets you looked at). State
plainly what you did **not** verify.

## Known sharp edges
- A modal dialog in FCP blocks every call. If a call hangs, run `visible_windows.m`
  and report; don't keep retrying.
- Don't call save methods on libraries. FCP saves on its own.
- In probe code, never call a void-returning selector through `CALL0`/`CALL1`
  (ARC retains garbage and FCP crashes). Use `CALLV0`/`CALLV1`.
- Large baked keyframe sets make export/import slow (tens of seconds); a
  rough cut has none, and should export in well under a second.
