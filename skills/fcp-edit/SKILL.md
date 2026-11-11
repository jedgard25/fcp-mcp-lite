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
- **Never write into, open, or read the user's other libraries or projects.**
  Each job gets a **brand-new library** (`make_library.m`), e.g.
  `~/Movies/<Job Name>.fcpbundle`. Unless the user names a library, build
  there. Never open other libraries to look for material: the user may keep
  reference edits there that you must not see. To change a result, import a
  new version (`… v2`) into the same library; never overwrite. Leave the
  library open when you're done so the user can review it.
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
| `open_library.m` | `{"path": "/abs/Lib.fcpbundle"}` | Reopens **your own** job library if it got closed. Never open anything else. |
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
| `tools/parakeet-transcriber/.build/release/parakeet-transcriber FILE --model v3 > words.json` | On-device ASR. Outputs a JSON list `[{word, startTime, endTime, confidence}]` in seconds of FILE. ~1.5 min per 15 min of audio. **Transcribe the processed WAV** (cleaner), then map to camera time with the offset. Run long files in the background. |
| `python3 tools/cut_sheet.py export.fcpxml sheet.jpg [--from N] [--count N] [--handle 0.5]` | Per cut: head and tail frames from the source, plus the waveform of the connected audio across the cut ±handle, with red lines at the edit points. A red line inside a waveform blob = a cut mid-word. Works on any project whose spine clips carry a lane −1 audio child. |
| `python3 tools/story_check.py export.fcpxml --words AUDIO.wav=words.json [--words …] [--text]` | Reads the cut back as **text in timeline order** (from each clip's connected-audio range, so no offsets needed) and flags **DUPLICATE** sentences (a retake left in, or a cut that ends on the line the next cut starts) and **STUTTER** (the same word on both sides of a cut, usually the first word of a retake left on a tail). Exit code 1 = something flagged. `--text` prints the whole kept transcript with cut markers. |
| `ffprobe` / `ffmpeg` | Durations, timecode (`-show_entries stream_tags=timecode`), spot frames. |
| `scripts/weftcut_to_fcpxml.py` | Reference converter. Import `tc_start_frames` from it instead of rewriting timecode math. |

## 2. Writing FCPXML that FCP accepts

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
- After importing, **export it back and check** clip count, total duration and
  a few offsets. Compare times as fractions, never as text (FCP renormalizes).

## 3. Rough-cut method (talking head, multiple takes)

1. **Inventory**: list the media; get durations and timecode; read any script.
2. **Sync**: run `align_audio.py` over every camera file × every WAV. Pair
   each camera file with its best match only if the score is clearly above the
   others. Note the offsets.
3. **Transcribe** each matched WAV. Convert word times to camera time:
   `cam_t = wav_t − offset_s`. Drop words that fall outside the camera file.
4. **Segment** into utterances: break on gaps > ~0.6 s or sentence punctuation.
5. **Pick takes.** This is the editorial core:
   - Line the utterances up against the script (fuzzy text match). Each script
     beat should appear **once** in the cut, in script order.
   - Retakes show up as near-duplicate utterances close together. **Default to
     the last complete, fluent take**, since presenters restart until it's right.
     Prefer a take with no stumble, filler or restart inside it.
   - **False starts**: an utterance that trails off or is cut short, then
     restarts the same words. Drop the fragment.
   - Drop off-script chatter ("okay", "let me do that again", counting in,
     slate claps) unless it's clearly intended content.
   - If the script and the delivery disagree, the delivery wins; a presenter
     ad-libbing a better line is content.
6. **Cut points**: start ~0.10–0.15 s before the first word and end ~0.15–0.25 s
   after the last word, inside the silence. Never cut inside a word. Merge
   consecutive kept utterances from the same take (no cut between them).
7. **Build** the FCPXML (section 2), validate, import into a new event, open the
   project.
8. **Audit** (below): story check first, then vision. Fix, re-import as `v2`,
   and audit again until `story_check.py` is clean.

## 4. Self-audit with vision. Do it every time, and look at the images

- **Timeline screenshot** (`snapshot_ui.m`): is the project open, is every cut
  on the storyline, is the WAV lane present under each cut, does anything look
  disabled (dimmed) or missing?
- **Composite frames** (`composite_frames.m`) at ~6 times spread across the
  cut, plus a few right after edit points: right person, right framing, no
  black or offline frames ("Missing File" = a bad path).
- **Cut sheet** (`cut_sheet.py` on the exported XML, in pages of ~10–12 cuts):
  red edit lines should sit in silence; the head/tail frames show jump cuts.
  Check every page for a cut of < 2 minutes, and the first few pages plus any
  suspicious cuts for longer ones.
- **Sync check**: for 2–3 cuts, compare the transcript word under an edit with
  the waveform. If speech blobs consistently start before or after the
  edit, the offset sign is wrong (`audio = camera + offset`).
- **Story check (mandatory, automated + read)**: run `tools/story_check.py` on
  the **exported** project. Fix every DUPLICATE and STUTTER, or justify keeping
  it in your report. Then read its `--text` output top to bottom against the
  script for missing beats. Frames and waveforms can't show a repeated sentence;
  in the first blind run, a duplicated intro line and two stray retake words at
  cut tails passed the visual audit and were caught only by this.

## 5. Report back

Say what you built and where (library / event / project). Give the source
pairing and offsets, the number of cuts and the total duration, which takes you
chose where there were several (and why), anything you were unsure about, and
the audit evidence (paths to the screenshots/sheets you looked at). State
plainly what you did **not** verify.

## Known sharp edges
- A modal dialog in FCP blocks every call. If a call hangs, run `visible_windows.m`
  and report; don't keep retrying.
- Don't create libraries or call save methods. FCP saves on its own.
- Large baked keyframe sets make export/import slow (tens of seconds); a
  rough cut has none, and should export in well under a second.
