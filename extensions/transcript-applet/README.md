# Transcript — native Final Cut transcript editor

A narrow floating NSPanel with native Liquid Glass (macOS 26+), SwiftUI cards,
and AppKit text selection. Earlier systems use an NSVisualEffectView blur.
Cards wrap their full text and resize with the window. The panel stays above
Final Cut without disappearing when it loses focus.
No web view. Requires macOS 14+; system controls adopt the installed macOS look.

```sh
make ui       # local Python timeline service, leave running
make applet   # build, sign locally, and open Transcript.app
```

The app bundle is `.build/Transcript.app`. Reopen it while the service is running.
Use `UI_PORT=...` and `APPLET_PORT=...` when choosing another port.

## Editing

- Click directly in a card. There is no separate detail view.
- Drag a card's header to reorder. A lifted card follows the pointer while
  neighboring cards make room with spring animation. Only the drop sends an
  edit; hovering never does. Rows retain identity on acknowledgment.
- Select words and press **Delete** to trim. Partial word selections include
  the whole word; with no selection, Backspace trims the preceding word.
- Put the cursor between words and press **Enter** to split after the preceding
  word. Both halves appear as separate cards. A selection splits after its end.
- Each card has a trash button to delete the entire slice.
- The always-visible search field filters cards. Reordering is disabled while filtered.
- The timeline syncs every two seconds while the panel is visible and idle.
  **Refresh** / Command-R also rereads it immediately.

Edits apply immediately to FCP. Typing replacement words is refused because the
editor cuts recorded speech; it does not synthesize replacement audio. Changing
focus never commits an edit. Undo is available in FCP, the panel then syncs the result automatically.
A single GUI action may create multiple FCP undo entries.

## State and safety

`editor_service.py` projects original transcript words through the current
primary clips, in timeline order. Each slice is a sentence intersected with one
physical clip. Split boundaries therefore survive app restarts and FCP undo
without a second saved document. Sub-frame remnants are not editable rows.

The native app uses `/api/editor` and `/api/editor/edit`; legacy web endpoints
remain separate. Edits carry a structural revision, excluding playhead, selection, and volatile
bridge handles. HTTP requests are serialized; successful edits return the live
snapshot. When an external edit changed the revision, an intent can be resolved
against current positions only if its original source and anchor word identities
still match in the same named timeline. Changed cards are refused before writing
and the refreshed snapshot is shown immediately, without locking the editor. On drop, the UI locks editing immediately and keeps the proposed order visible
until the verified snapshot arrives. A matching acknowledgment does not rebuild
the cards or reset text selection. Timed-out writes are never retried.
Failed or uncertain writes still disable edits until a refresh, and any returned
actual state remains visible. Avoid editing in FCP during an in-flight action:
the bridge cannot make a multi-RPC edit atomic against manual FCP edits.

Repeated source footage disables editing because transcript word identities
would be ambiguous. Only the cached transcript's source is shown. The service
must be restarted after updating its Python code.

## Verification

```sh
make test
swift test --package-path extensions/transcript-applet
```

Python regressions cover timeline order, split projection, trimmed words, structural
revisions, safe intent rebasing, move verification, partial failure, and duplicate source footage.
Swift regressions cover UTF-16 selection, overlapping requests, drag
acknowledgments, background sync, failed-write reconciliation, and Enter/Delete interception. These tests use synthetic data, without modifying FCP projects.
