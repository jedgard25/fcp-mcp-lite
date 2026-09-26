import AppKit
import SwiftUI
import XCTest
@testable import TranscriptApplet

@MainActor final class CardTextTests: XCTestCase {
    func testEnterSplitsAndDeleteRequestsTrimWithoutMutatingText() {
        var splits = 0
        var removed: NSRange?
        let editor = CardTextView(text: "One two three", selection: .constant(NSRange(location: 4, length: 0)),
                                  focused: .constant(false), enabled: true,
                                  onSplit: { splits += 1 }, onDelete: { removed = $0 }, onUnsupportedEdit: {})
        let coordinator = editor.makeCoordinator()
        let view = NSTextView()
        view.string = editor.text
        view.setSelectedRange(NSRange(location: 4, length: 0))
        XCTAssertTrue(coordinator.textView(view, doCommandBy: #selector(NSStandardKeyBindingResponding.insertNewline(_:))))
        XCTAssertEqual(splits, 1)
        XCTAssertFalse(coordinator.textView(view, shouldChangeTextIn: NSRange(location: 4, length: 3), replacementString: ""))
        XCTAssertEqual(removed, NSRange(location: 4, length: 3))
        XCTAssertEqual(view.string, editor.text)
    }

    func testBusyCardCannotIssueAnotherEdit() {
        var commands = 0
        let editor = CardTextView(text: "One two", selection: .constant(NSRange(location: 3, length: 0)),
                                  focused: .constant(false), enabled: false,
                                  onSplit: { commands += 1 }, onDelete: { _ in commands += 1 }, onUnsupportedEdit: {})
        let coordinator = editor.makeCoordinator()
        let view = NSTextView()
        XCTAssertTrue(coordinator.textView(view, doCommandBy: #selector(NSStandardKeyBindingResponding.insertNewline(_:))))
        XCTAssertFalse(coordinator.textView(view, shouldChangeTextIn: NSRange(location: 0, length: 3), replacementString: ""))
        XCTAssertEqual(commands, 0)
    }
    func testTextMeasurementTracksWidthAndChangedContent() {
        let view = CardText()
        let text = String(repeating: "A word to wrap. ", count: 12)
        let wide = view.measuredSize(text: text, width: 340)
        XCTAssertEqual(view.measuredSize(text: text, width: 340), wide)
        let narrow = view.measuredSize(text: text, width: 180)
        XCTAssertGreaterThan(narrow.height, wide.height)
        XCTAssertLessThan(view.measuredSize(text: "Short", width: 180).height, narrow.height)
    }

}
