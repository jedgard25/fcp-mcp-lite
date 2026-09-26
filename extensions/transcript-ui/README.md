# transcript-ui — the transcript editor (single clean path)

FCP-inspector-styled slice editor. Each card is a sentence intersected with
one physical clip (`S…` slice ids, file-anchored word indices — never shift
after cuts). Every action commits immediately through `mcp/server.py` (same
validation + JSONL log as the agent verbs), so the change lands live in FCP.

The in-process FCP panel (**Window > Transcript**, ⌘0) loads this page. It
shows bridge/MCP status until a transcript exists; edits stay disabled while
the snapshot carries `edit_error`.

## Run

```sh
make ui            # http://127.0.0.1:8765 — leave running; the FCP panel reads it
make ui PORT=8770  # custom port (panel honors TRANSCRIPT_UI_PORT)
```

Open http://127.0.0.1:8765 in a browser to drive the same editor outside FCP.

## What each control does

| Control | Backend intent (`POST /api/editor/edit`) | Notes |
|---|---|---|
| Select words + **Delete** (or Backspace with no selection trims the preceding word) | `trim` with those word ids | cuts those words: jump cut, splice continuity broken (reported, not an error) |
| **Enter** with caret in a card | `split` after the word under the caret | needs a word each side; FCP shows two clips |
| 🗑 delete | `delete` (whole slice) | sub-frame crumbs stay, reported |
| Drag ⋮⋮ grip | `move` (`before_id`, null = end) | disabled while searching; optimistic order until the verified snapshot arrives |
| Type / paste new words | refused + reverted | speech can only be cut, never synthesized |

`GET /api/editor` returns the snapshot (`revision, timeline, title, duration,
slices, edit_error`). Every edit carries `revision` + word-identity anchors;
a card that changed in FCP is refused as `stale` and the fresh timeline is
shown. A failed write locks editing until manual refresh — inspect FCP first.
Background sync re-reads every 2s while idle.
