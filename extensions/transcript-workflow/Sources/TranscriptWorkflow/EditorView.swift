import AppKit
import SwiftUI

/// Native text selection and keyboard commands, with edits mapped to recorded
/// words. Shared verbatim with the floating applet: Delete trims, Enter splits,
/// any other keystroke is refused (no synthesized speech).
struct CardTextView: NSViewRepresentable {
    let text: String
    @Binding var selection: NSRange
    @Binding var focused: Bool
    let enabled: Bool
    let onSplit: () -> Void
    let onDelete: (NSRange) -> Void
    let onUnsupportedEdit: () -> Void

    static var paragraph: NSParagraphStyle {
        let style = NSMutableParagraphStyle()
        style.lineSpacing = 4
        return style
    }
    static var attributes: [NSAttributedString.Key: Any] {
        [.font: NSFont.systemFont(ofSize: 15), .paragraphStyle: paragraph, .foregroundColor: NSColor.labelColor]
    }

    func makeNSView(context: Context) -> CardText {
        let view = CardText()
        view.focusRingType = .none
        view.isEditable = true
        view.isSelectable = true
        view.isRichText = false
        view.allowsUndo = false
        view.isAutomaticQuoteSubstitutionEnabled = false
        view.isAutomaticDashSubstitutionEnabled = false
        view.isAutomaticSpellingCorrectionEnabled = false
        view.drawsBackground = false
        view.textContainerInset = NSSize(width: 0, height: 3)
        view.textContainer?.lineFragmentPadding = 0
        view.textContainer?.widthTracksTextView = true
        view.isVerticallyResizable = false
        view.isHorizontallyResizable = false
        view.delegate = context.coordinator
        view.focusChanged = { context.coordinator.parent.focused = $0 }
        view.setAccessibilityLabel("Transcript card. Enter to split; select words and Delete to trim.")
        view.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: Self.attributes))
        return view
    }

    func updateNSView(_ view: CardText, context: Context) {
        context.coordinator.parent = self
        context.coordinator.updating = true
        defer { context.coordinator.updating = false }
        if view.string != text {
            let old = view.selectedRange()
            view.textStorage?.setAttributedString(NSAttributedString(string: text, attributes: Self.attributes))
            let count = (text as NSString).length
            view.setSelectedRange(NSRange(location: min(old.location, count), length: 0))
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, nsView: CardText, context: Context) -> CGSize? {
        guard let width = proposal.width, width > 0 else { return nil }
        return nsView.measuredSize(text: text, width: width)
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: CardTextView
        var updating = false
        init(_ parent: CardTextView) { self.parent = parent }
        func textViewDidChangeSelection(_ notification: Notification) {
            guard !updating, let view = notification.object as? NSTextView else { return }
            parent.selection = view.selectedRange()
        }
        func textView(_ textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            if selector == #selector(NSStandardKeyBindingResponding.insertNewline(_:)) {
                if parent.enabled { parent.selection = textView.selectedRange(); parent.onSplit() }
                return true
            }
            return false
        }
        func textView(_ textView: NSTextView, shouldChangeTextIn affectedCharRange: NSRange, replacementString: String?) -> Bool {
            guard parent.enabled else { return false }
            if replacementString == "", affectedCharRange.length > 0 {
                var range = affectedCharRange
                let string = textView.string as NSString
                if range.length == 1, range.location > 0, NSMaxRange(range) <= string.length,
                   string.substring(with: range) == " " {
                    range.location -= 1
                }
                parent.onDelete(range)
            } else {
                parent.onUnsupportedEdit()
                NSSound.beep()
            }
            return false
        }
    }
}

final class CardText: NSTextView {
    var focusChanged: ((Bool) -> Void)?
    private var measurement: (text: String, width: CGFloat, size: CGSize)?
    override var mouseDownCanMoveWindow: Bool { false }
    func measuredSize(text: String, width: CGFloat) -> CGSize {
        if let measurement, measurement.text == text, measurement.width == width { return measurement.size }
        let bounds = (text as NSString).boundingRect(with: NSSize(width: width, height: .greatestFiniteMagnitude),
            options: [.usesLineFragmentOrigin, .usesFontLeading], attributes: CardTextView.attributes)
        let size = CGSize(width: width, height: max(26, ceil(bounds.height) + 8))
        measurement = (text, width, size)
        return size
    }
    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { focusChanged?(true) }
        return accepted
    }
    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted { focusChanged?(false) }
        return accepted
    }
}
