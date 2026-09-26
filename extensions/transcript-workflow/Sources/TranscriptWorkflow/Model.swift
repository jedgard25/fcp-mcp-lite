import Foundation
import SwiftUI

/// Same editing model as the floating Transcript applet, minus window
/// ownership. The Workflow Extension host (Final Cut) owns the window, so
/// there is no NSPanel, drag-region, or resize-region here — just state.
@Observable @MainActor
final class StoryModel {
    let api: any TimelineAPI
    var snapshot: TimelineSnapshot?
    var selectedID: String?
    var selection = NSRange(location: 0, length: 0)
    var busy = false
    var needsRefresh = false
    var error: String?
    var bridgeError: String?
    var message = "Connect to your Final Cut timeline"
    var search = ""
    private var previewOrder: [String]?
    var dragging = false
    private var syncing = false

    init(port: Int) { api = APIClient(port: port) }
    init(api: any TimelineAPI) { self.api = api }
    var slices: [TimelineSlice] {
        let rows = snapshot?.slices ?? []
        guard let previewOrder else { return rows }
        let byID = Dictionary(uniqueKeysWithValues: rows.map { ($0.id, $0) })
        return previewOrder.compactMap { byID[$0] }
    }
    var selected: TimelineSlice? { slices.first { $0.id == selectedID } }
    var canEdit: Bool { !busy && !needsRefresh && snapshot != nil && snapshot?.edit_error == nil }
    /// Status panel state: nil snapshot + bridgeError/edit_error means "waiting".
    var waitingReason: String? {
        if let e = snapshot?.edit_error { return e }
        if snapshot == nil { return bridgeError ?? error ?? "Starting timeline service…" }
        return nil
    }
    var filteredSlices: [TimelineSlice] {
        search.isEmpty ? slices : slices.filter { $0.text.localizedCaseInsensitiveContains(search) }
    }
    var selectedWords: [Int] {
        guard let selected, selection.length > 0 else { return [] }
        var offset = 0
        return selected.words.compactMap { word in
            let range = NSRange(location: offset, length: (word.w as NSString).length)
            offset += range.length + 1
            return NSIntersectionRange(range, selection).length > 0 ? word.i : nil
        }
    }
    var splitWords: [Int] {
        guard let selected else { return [] }
        let position = NSMaxRange(selection)
        var offset = 0
        var index: Int?
        for word in selected.words {
            if offset < position { index = word.i }
            offset += (word.w as NSString).length + 1
        }
        guard let index, index != selected.words.last?.i else { return [] }
        return [index]
    }

    func select(_ id: String?) {
        guard selectedID != id else { return }
        selectedID = id
        selection = NSRange(location: 0, length: 0)
    }

    private func apply(_ story: TimelineSnapshot) {
        let previous = selected
        snapshot = story
        previewOrder = nil
        if !story.slices.contains(where: { $0.id == selectedID }) {
            select(story.slices.first?.id)
        }
        if selected?.text != previous?.text { selection = NSRange(location: 0, length: 0) }
    }

    func refresh() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            // Bridge status first so the panel can show MCP state until a
            // transcript exists (no words yet -> snapshot.edit_error path).
            if snapshot == nil {
                do { _ = try await api.status() ; bridgeError = nil }
                catch { bridgeError = error.localizedDescription }
            }
            apply(try await api.story())
            needsRefresh = false
            error = nil
            message = "Up to date with Final Cut Pro"
        } catch {
            self.error = error.localizedDescription
            needsRefresh = true
            message = "Unable to read timeline"
        }
    }

    func syncIfIdle() async {
        guard !busy, !dragging, !syncing, !needsRefresh else { return }
        syncing = true
        let revision = snapshot?.revision
        defer { syncing = false }
        do {
            let fresh = try await api.story()
            guard !busy, !dragging, snapshot?.revision == revision else { return }
            if fresh.revision != revision {
                withAnimation(.spring(response: 0.32, dampingFraction: 0.86)) { apply(fresh) }
            }
        } catch {
            // Background reads never disrupt typing or promote a transient
            // bridge delay into an error banner.
        }
    }

    func perform(_ action: String, id: String? = nil, before: String? = nil, words: [Int] = []) async {
        guard canEdit, let snapshot, let source = id ?? selectedID else { return }
        busy = true
        await send(EditRequest(revision: snapshot.revision, action: action, id: source, before_id: before, words: words))
    }

    func moveCards(from offsets: IndexSet, to destination: Int) {
        guard canEdit, search.isEmpty, offsets.count == 1,
              let source = offsets.first, slices.indices.contains(source),
              (0...slices.count).contains(destination), let snapshot else { return }
        var ids = slices.map(\.id)
        let original = ids
        let moved = ids[source]
        ids.move(fromOffsets: offsets, toOffset: destination)
        guard ids != original, let position = ids.firstIndex(of: moved) else { return }
        let before = position + 1 < ids.count ? ids[position + 1] : nil
        busy = true
        previewOrder = ids
        Task { await send(EditRequest(revision: snapshot.revision, action: "move", id: moved, before_id: before)) }
    }

    func moveCardToEnd(_ id: String) {
        guard let index = slices.firstIndex(where: { $0.id == id }) else { return }
        moveCards(from: IndexSet(integer: index), to: slices.count)
    }

    private func send(_ request: EditRequest) async {
        error = nil
        message = "Applying edit in Final Cut Pro…"
        defer { busy = false }
        do {
            var intent = request
            intent.timeline = snapshot?.timeline
            intent.source_words = snapshot?.slices.first(where: { $0.id == request.id })?.words.map(\.i)
            intent.anchor_words = snapshot?.slices.first(where: { $0.id == request.before_id })?.words.map(\.i)
            let result = try await api.edit(intent)
            if let story = result.story { apply(story) }
            if result.code == "stale", result.story != nil {
                needsRefresh = false
                error = nil
                message = "Updated from Final Cut Pro — that card changed"
                return
            }
            needsRefresh = !result.ok || result.story == nil
            if result.ok && result.story != nil {
                message = "Edit confirmed in Final Cut Pro"
            } else {
                error = result.error ?? "Final Cut did not confirm the edit. Refresh before continuing."
                message = "Review timeline before continuing"
            }
        } catch {
            self.error = "The edit could not be confirmed. Refresh before retrying. \(error.localizedDescription)"
            needsRefresh = true
            message = "Timeline state needs checking"
        }
    }

    func nudge(_ direction: Int) async {
        guard let selectedID, let index = slices.firstIndex(where: { $0.id == selectedID }), slices.indices.contains(index + direction) else { return }
        let before = direction < 0 ? slices[index - 1].id : (index + 2 < slices.count ? slices[index + 2].id : nil)
        await perform("move", before: before)
    }
}
