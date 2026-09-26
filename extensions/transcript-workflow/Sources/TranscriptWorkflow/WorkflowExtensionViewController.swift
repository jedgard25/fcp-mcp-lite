import AppKit
import SwiftUI

/// Principal view controller named in the appex Info.plist
/// (`ProExtensionPrincipalViewControllerClass`).
///
/// When the Workflow Extension SDK is present this subclasses the ProExtension
/// view controller; otherwise it is a plain NSViewController so the package
/// still builds and previews without Apple's SDK installed.
#if canImport(ProExtension)
import ProExtension
final class WorkflowExtensionViewController: ProExtensionPrincipalViewController {
    private var model: StoryModel?
    override func viewDidLoad() {
        super.viewDidLoad()
        embed()
    }
    override func viewWillDisappear() {
        super.viewWillDisappear()
        // Stop background sync when FCP closes the panel (audio/session
        // teardown hook per Apple template guidance).
        model = nil
    }
    private func embed() {
        let port = WorkflowExtensionViewController.servicePort()
        let model = StoryModel(port: port)
        self.model = model
        let host = NSHostingView(rootView: WorkflowContentView().environment(model).task { await model.refresh() })
        host.focusRingType = .none
        host.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            host.topAnchor.constraint(equalTo: view.topAnchor),
            host.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
        view.minSize = NSSize(width: 300, height: 380)
    }
    static func servicePort() -> Int {
        if let raw = ProcessInfo.processInfo.environment["TRANSCRIPT_UI_PORT"], let p = Int(raw) { return p }
        return 8765
    }
}
#else
final class WorkflowExtensionViewController: NSViewController {
    private var model: StoryModel?
    override func loadView() {
        view = NSView(frame: NSRect(x: 0, y: 0, width: 400, height: 700))
    }
    override func viewDidLoad() {
        super.viewDidLoad()
        let port: Int
        if let raw = ProcessInfo.processInfo.environment["TRANSCRIPT_UI_PORT"], let p = Int(raw) { port = p }
        else { port = 8765 }
        let model = StoryModel(port: port)
        self.model = model
        let host = NSHostingView(rootView: WorkflowContentView().environment(model).task { await model.refresh() })
        host.focusRingType = .none
        host.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            host.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            host.topAnchor.constraint(equalTo: view.topAnchor),
            host.bottomAnchor.constraint(equalTo: view.bottomAnchor),
        ])
    }
    override func viewWillDisappear() {
        super.viewWillDisappear()
        model = nil
    }
}
#endif
