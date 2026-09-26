# extensions — optional second-tier extras

Core (`patch/`, `bridge/`, `mcp/`, `tools/`) stays lean and dependency-free.
Everything under `extensions/` is:

- optional (never installed by `make mcp-setup`, never imported by core),
- self-contained (own README, own entrypoint, stdlib-first),
- agent-launchable (`make ui`, background `bash`, then drive via browser).

| Extension | What | Launch |
|---|---|---|
| `transcript-ui/` | Local API service; also retains the legacy web editor | `make ui` → http://127.0.0.1:8765 |
| `transcript-applet/` | Native floating card editor: Enter to split, Delete to trim, animated reordering | `make ui` + `make applet` |
| `transcript-workflow/` | Same cards as a literal FCP panel (Workflow Extension `.appex`); status until transcribed | `make ui` + `make workflow` |
