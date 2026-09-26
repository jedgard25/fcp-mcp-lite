# extensions — the transcript editor lives here

Core (`patch/`, `bridge/`, `mcp/`, `tools/`) stays lean and dependency-free.
`extensions/transcript-ui/` is the single editor frontend (no separate native
apps, no legacy variants):

| Extension | What | Launch |
|---|---|---|
| `transcript-ui/` | FCP-styled slice editor over the revision-checked `/api/editor` model; the in-process FCP panel (Window > Transcript) loads this page | `make ui` → http://127.0.0.1:8765 |

The panel never opens on its own — it appears only via
**Window > Transcript** (⌘0) in patched FCP, so users who don't want it
never see it.
