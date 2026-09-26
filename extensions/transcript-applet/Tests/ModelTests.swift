import XCTest
@testable import TranscriptApplet

@MainActor final class FakeAPI: TimelineAPI {
    var pending: CheckedContinuation<EditResponse, Error>?
    var reads = 0
    var writes = 0
    var value: TimelineSnapshot
    init(_ value: TimelineSnapshot) { self.value = value }
    func story() async throws -> TimelineSnapshot { reads += 1; return value }
    func edit(_ body: EditRequest) async throws -> EditResponse {
        writes += 1
        return try await withCheckedThrowingContinuation { pending = $0 }
    }
}

@MainActor final class ModelTests: XCTestCase {
    let row = TimelineSlice(id: "a", text: "Hi 👋 again", start: 0, end: 3,
                            words: [WordToken(i: 5, w: "Hi", start: 0, end: 1),
                                    WordToken(i: 8, w: "👋", start: 1, end: 2),
                                    WordToken(i: 9, w: "again", start: 2, end: 3)])
    func story(_ rows: [TimelineSlice]) -> TimelineSnapshot {
        TimelineSnapshot(revision: "r", title: "Test", duration: 3, slices: rows, edit_error: nil)
    }
    func testUnicodeSelectionUsesActualVisibleWordIDs() async {
        let model = StoryModel(api: FakeAPI(story([row])))
        await model.refresh()
        model.selection = NSRange(location: 3, length: 2)
        XCTAssertEqual(model.selectedWords, [8])
        XCTAssertEqual(model.splitWords, [8])
    }
    func testRefreshAndSecondMutationCannotReleaseAnActiveEdit() async {
        let api = FakeAPI(story([row]))
        let model = StoryModel(api: api)
        await model.refresh()
        let edit = Task { await model.perform("delete") }
        while api.pending == nil { await Task.yield() }
        await model.refresh()
        await model.perform("delete")
        XCTAssertTrue(model.busy)
        XCTAssertEqual(api.reads, 1)
        XCTAssertEqual(api.writes, 1)
        XCTAssertEqual(model.slices.map(\.id), ["a"])
        api.pending?.resume(returning: EditResponse(ok: true, error: nil, story: story([])))
        await edit.value
        XCTAssertFalse(model.busy)
        XCTAssertTrue(model.slices.isEmpty)
    }
    func testFailedWriteAppliesActualStateAndRequiresRefresh() async {
        let api = FakeAPI(story([row]))
        let model = StoryModel(api: api)
        await model.refresh()
        let edit = Task { await model.perform("delete") }
        while api.pending == nil { await Task.yield() }
        api.pending?.resume(returning: EditResponse(ok: false, error: "Verification failed", story: story([])))
        await edit.value
        XCTAssertTrue(model.slices.isEmpty)
        XCTAssertFalse(model.canEdit)
        XCTAssertEqual(model.error, "Verification failed")
    }
    func testNativeMoveLocksImmediatelyAndAcknowledgmentPreservesSelection() async {
        let other = TimelineSlice(id: "b", text: "Next", start: 3, end: 4,
                                  words: [WordToken(i: 10, w: "Next", start: 3, end: 4)])
        let api = FakeAPI(story([row, other]))
        let model = StoryModel(api: api)
        await model.refresh()
        model.selection = NSRange(location: 3, length: 2)
        model.moveCards(from: IndexSet(integer: 0), to: 2)
        XCTAssertTrue(model.busy)
        XCTAssertEqual(model.slices.map(\.id), ["b", "a"])
        model.moveCards(from: IndexSet(integer: 0), to: 2)
        while api.pending == nil { await Task.yield() }
        XCTAssertEqual(api.writes, 1)
        api.pending?.resume(returning: EditResponse(ok: true, error: nil, story: story([other, row])))
        while model.busy { await Task.yield() }
        XCTAssertEqual(model.slices.map(\.id), ["b", "a"])
        XCTAssertEqual(model.selection, NSRange(location: 3, length: 2))
    }

    func testFailedNativeMoveReconcilesActualOrder() async {
        let other = TimelineSlice(id: "b", text: "Next", start: 3, end: 4,
                                  words: [WordToken(i: 10, w: "Next", start: 3, end: 4)])
        let original = story([row, other])
        let api = FakeAPI(original)
        let model = StoryModel(api: api)
        await model.refresh()
        model.moveCards(from: IndexSet(integer: 0), to: 2)
        while api.pending == nil { await Task.yield() }
        api.pending?.resume(returning: EditResponse(ok: false, error: "Move failed", story: original))
        while model.busy { await Task.yield() }
        XCTAssertEqual(model.slices.map(\.id), ["a", "b"])
        XCTAssertFalse(model.canEdit)
    }

    func testBackgroundSyncKeepsSelectionAndDoesNotBecomeAnEdit() async {
        let api = FakeAPI(story([row]))
        let model = StoryModel(api: api)
        await model.refresh()
        model.selection = NSRange(location: 3, length: 2)
        api.value = TimelineSnapshot(revision: "new", title: "Test", duration: 4, slices: [row], edit_error: nil)
        await model.syncIfIdle()
        XCTAssertEqual(model.snapshot?.revision, "new")
        XCTAssertEqual(model.selection, NSRange(location: 3, length: 2))
        XCTAssertEqual(api.writes, 0)
        XCTAssertFalse(model.busy)
        model.dragging = true
        await model.syncIfIdle()
        XCTAssertEqual(api.reads, 2)
    }

    func testStaleRejectionAlreadyContainsRefreshAndDoesNotLockEditor() async {
        let api = FakeAPI(story([row]))
        let model = StoryModel(api: api)
        await model.refresh()
        let edit = Task { await model.perform("delete") }
        while api.pending == nil { await Task.yield() }
        api.pending?.resume(returning: EditResponse(ok: false, error: "Card changed", story: story([]), code: "stale"))
        await edit.value
        XCTAssertTrue(model.slices.isEmpty)
        XCTAssertFalse(model.needsRefresh)
        XCTAssertNil(model.error)
        XCTAssertEqual(api.writes, 1)
    }

}
