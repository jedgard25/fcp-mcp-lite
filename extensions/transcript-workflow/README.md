# transcript-workflow — literal Final Cut Pro panel (Workflow Extension)

Second-tier extension. Same cards as `transcript-applet/`, but hosted **inside
Final Cut** via Apple's Workflow Extension mechanism (`.appex` in a container
`.app`) instead of a floating `NSPanel`.

What you get in FCP: **Extensions button (left toolbar) → Transcript** (also
`Window > Extensions`). FCP provides the window chrome, resize, and workspace
position — this package only provides the view.

## Status until transcribed

The panel shows MCP/bridge status until words exist:

- `GET /api/status` → bridge reachability (service down? FCP unpatched?)
- `GET /api/editor` → `edit_error` when no transcript yet ("Transcribe in FCP first")

Edits stay disabled (`canEdit == false`) while `edit_error` is set or no
snapshot has loaded. Covered by `Tests/WorkflowTests.swift`.

## Run

```sh
make ui        # timeline service must be running (127.0.0.1:8765)
make workflow  # swift build + stage .build/TranscriptPanel.app skeleton
```

Then, once the Xcode SDK target exists:

1. Install Apple's **Workflow Extension SDK** (`developer.apple.com/download`,
   search WorkflowExtensions) — installs `ProExtension` frameworks + the
   `Final Cut Pro Workflow Extension` Xcode template.
2. Add the template target to a container app, set
   `ProExtensionPrincipalViewControllerClass = WorkflowExtensionViewController`
   (already matching `Extension/Info.plist`), add the
   `Container/TranscriptWorkflow.entitlements` network-client entitlement.
3. Copy `Sources/TranscriptWorkflow/*.swift` into the appex target (no edits —
   `WorkflowExtensionViewController` uses `canImport(ProExtension)` so the same
   files build with and without the SDK).
4. Build, copy `TranscriptPanel.app` to `/Applications`, launch once to register,
   then open FCP — the Extensions button appears.

`build-workflow-app.sh` stages the same bundle layout with a placeholder binary
so `make workflow` verifies the Swift package + plist/entitlement wiring without
the SDK. Debug a real appex via Xcode `Debug > Attach to Process by PID or Name`.

## Files

- `Sources/TranscriptWorkflow/` — `API/Model/EditorView` shared with the applet,
  `WorkflowContentView` (no panel chrome), `WorkflowExtensionViewController`
  (principal class, `viewWillDisappear` teardown per Apple template).
- `Extension/Info.plist` — `com.apple.FinalCut.WorkflowExtension`, min window 300×380.
- `Container/` — app `Info.plist` + sandbox `network.client` entitlements.
- `Tests/WorkflowTests.swift` — waiting-state gating (no transcript / bridge down).
