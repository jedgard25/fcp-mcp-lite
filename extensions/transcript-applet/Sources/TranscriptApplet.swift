import AppKit
import SwiftUI

@main
struct TranscriptApplet: App {
    @NSApplicationDelegateAdaptor(PanelController.self) private var delegate
    var body: some Scene {
        Settings { EmptyView() }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Show Transcript") { delegate.showPanel() }.keyboardShortcut("0", modifiers: .command)
            }
        }
    }
}

@MainActor final class PanelController: NSObject, NSApplicationDelegate {
    private var panel: TranscriptPanel?

    func applicationDidFinishLaunching(_ notification: Notification) { showPanel() }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }

    func showPanel() {
        if let panel { panel.makeKeyAndOrderFront(nil); return }
        let args = CommandLine.arguments
        var port = Int(ProcessInfo.processInfo.environment["TRANSCRIPT_UI_PORT"] ?? "") ?? 8765
        if let index = args.firstIndex(of: "--port"), index + 1 < args.count, let value = Int(args[index + 1]) { port = value }
        let model = StoryModel(port: port)
        let panel = TranscriptPanel(contentRect: NSRect(x: 0, y: 0, width: 400, height: 760),
                                    styleMask: [.borderless, .nonactivatingPanel],
                                    backing: .buffered, defer: false)
        panel.title = "Transcript"
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.collectionBehavior = [.fullScreenAuxiliary]
        panel.minSize = NSSize(width: 300, height: 400)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.isMovableByWindowBackground = false
        let host = NSHostingView(rootView: ContentView(close: { [weak panel] in panel?.close() })
            .environment(model).task { await model.refresh() })
        host.focusRingType = .none
        if #available(macOS 26.0, *) {
            let glass = NSGlassEffectView()
            glass.focusRingType = .none
            glass.style = .regular
            glass.cornerRadius = 24
            glass.contentView = host
            panel.contentView = glass
        } else {
            let blur = NSVisualEffectView()
            blur.material = .hudWindow
            blur.blendingMode = .behindWindow
            blur.state = .active
            blur.wantsLayer = true
            blur.layer?.cornerRadius = 24
            blur.layer?.masksToBounds = true
            host.translatesAutoresizingMaskIntoConstraints = false
            blur.addSubview(host)
            NSLayoutConstraint.activate([host.leadingAnchor.constraint(equalTo: blur.leadingAnchor),
                                         host.trailingAnchor.constraint(equalTo: blur.trailingAnchor),
                                         host.topAnchor.constraint(equalTo: blur.topAnchor),
                                         host.bottomAnchor.constraint(equalTo: blur.bottomAnchor)])
            panel.contentView = blur
        }
        panel.center()
        panel.setFrameAutosaveName("TranscriptCardPanel")
        self.panel = panel
        panel.makeKeyAndOrderFront(nil)
    }
}

final class TranscriptPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}
