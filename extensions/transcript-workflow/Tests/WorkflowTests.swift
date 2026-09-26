import XCTest
@testable import TranscriptWorkflow

@MainActor final class FakeWorkflowAPI: TimelineAPI {
    var reads = 0
    var writes = 0
    var value: TimelineSnapshot
    var bridgeDown: Bool
    init(_ value: TimelineSnapshot?, bridgeDown: Bool = false) {
        self.value = value ?? TimelineSnapshot(revision: "r", title: "T", duration: 0, slices: [], edit_error: "Transcribe first")
        self.bridgeDown = bridgeDown
    }
    func story() async throws -> TimelineSnapshot {
        reads += 1
        if bridgeDown { throw APIError(message: "connection refused") }
        return value
    }
    func edit(_ body: EditRequest) async throws -> EditResponse {
        writes += 1
        return EditResponse(ok: true, error: nil, story: value)
    }
    func status() async throws -> BridgeStatus {
        if bridgeDown { throw APIError(message: "connection refused") }
        return BridgeStatus(ok: true, error: nil, bridge: nil)
    }
}

@MainActor final class WorkflowTests: XCTestCase {
    func row() -> TimelineSlice {
        TimelineSlice(id: "a", text: "Hello world", start: 0, end: 2,
                      words: [WordToken(i: 1, w: "Hello", start: 0, end: 1),
                              WordToken(i: 2, w: "world", start: 1, end: 2)])
    }
    func testWaitingStateBlocksEditsUntilTranscribed() async {
        let waiting = TimelineSnapshot(revision: "r", title: "T", duration: 0, slices: [], edit_error: "A transcript with source word timings is required. Transcribe in FCP first.")
        let model = StoryModel(api: FakeWorkflowAPI(waiting))
        await model.refresh()
        XCTAssertFalse(model.canEdit)
        XCTAssertNotNil(model.waitingReason)
        await model.perform("delete", id: "a")
        XCTAssertEqual((model.api as! FakeWorkflowAPI).writes, 0)
    }
    func testBridgeDownSurfacesWaitingReason() async {
        let model = StoryModel(api: FakeWorkflowAPI(nil, bridgeDown: true))
        await model.refresh()
        XCTAssertNil(model.snapshot)
        XCTAssertNotNil(model.waitingReason)
        XCTAssertFalse(model.canEdit)
    }
    func testLiveSnapshotEnablesEdits() async {
        let live = TimelineSnapshot(revision: "r", title: "T", duration: 2, slices: [row()], edit_error: nil)
        let model = StoryModel(api: FakeWorkflowAPI(live))
        await model.refresh()
        XCTAssertTrue(model.canEdit)
        XCTAssertNil(model.waitingReason)
        XCTAssertEqual(model.slices.map(\.id), ["a"])
    }
}
