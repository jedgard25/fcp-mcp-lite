# transcript-ui — optional autocommitting transcript editor

Second-tier extension. Not installed with core, not imported by core/tests,
zero new dependencies (stdlib HTTP + the venv you already have).

Each story line is an object (`L####`, file-anchored — never shifts after cuts).
Every action commits immediately: delete / drop-reorder / edge-trim writes
through `mcp/server.py` (same validation + JSONL log as the agent verbs),
so the change swaps live in FCP.

## Run (human or agent)

```sh
make ui            # http://127.0.0.1:8765 — venv python, background it if needed
make ui PORT=8770  # custom port
```

Agent flow: `bash` launch `make ui` in background, `webfetch`/browser the URL,
or drive `/api/*` directly. No FCP UI injection — the patched bridge stays
the only in-process code.

## What each control does

Literal text editing — every row is editable text, every keystroke-targeted
change commits on focus loss:

| Control | Backend (commit, `dry_run=False`) | Notes |
|---|---|---|
| type / delete words | `delete_words(ranges=…)` (one validated batch) | delete-only: new/edited words are refused + reverted — speech can't be synthesized. Clearing a row routes to `delete_lines` (midpoint-safe blades). |
| Backspace from row start/end | edge `delete_words` | trims the slice, like trimming the clip head/tail |
| delete mid-sentence words | mid `delete_words` | cuts those words: jump cut, splice continuity broken (reported, not an error) |
| Enter with caret mid-row | `split_words` (blade, duration-preserving, verified) | splits AFTER the word under the caret (caret at word start splits before it). Needs a word each side. FCP shows two clips; the row earns a ✂ badge. |
| 🗑 delete | `delete_lines([id])` + auto-follow `cut_spans(pending)` | refuses to delete the last line; sub-frame crumbs stay, reported |
| drag ⠿ reorder | `apply_story(keep=[ids in DOM order])`, auto-looped to `remaining=0` | drop above/below middle = before/after; order verified server-side |

`GET /api/story` returns full-detail lines (`id, text, start_word, end_word,
t_start/t_end, take_group, removed`) so trim buttons always have word indices.
`GET /api/review` is the take-group gate, not a clean-script verdict.
