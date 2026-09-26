import AppKit
import SwiftUI

/// FCP-hosted transcript panel. Same cards as the floating applet, but the
/// window chrome (traffic lights, toolbar, resize) belongs to Final Cut, so
/// there is no close button, drag region, or resize grip here.
///
/// Until the MCP service has a transcript, this shows bridge/MCP status
/// (`waitingReason`) instead of cards.
struct WorkflowContentView: View {
    @Environment(StoryModel.self) private var model
    /// Set by the principal view controller when the FCP window closes.
    var isVisible: Bool = true

    var body: some View {
        @Bindable var model = model
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Transcript").font(.system(size: 16, weight: .semibold))
                    Text(model.snapshot?.title ?? "Final Cut Pro").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                Spacer()
                Button { Task { await model.refresh() } } label: {
                    Image(systemName: "arrow.clockwise").frame(width: 26, height: 26)
                }.disabled(model.busy).help("Refresh from Final Cut Pro").keyboardShortcut("r", modifiers: .command)
            }
            .buttonStyle(.plain).foregroundStyle(.secondary).padding(.horizontal, 22).padding(.top, 22).padding(.bottom, 16)

            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(.tertiary)
                TextField("Search transcript", text: $model.search).textFieldStyle(.plain)
                if !model.search.isEmpty {
                    Button { model.search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(.tertiary) }
                        .buttonStyle(.plain).help("Clear search")
                }
            }
            .font(.system(size: 14)).padding(.horizontal, 13).padding(.vertical, 11)
            .background(.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(.primary.opacity(0.035)))
            .padding(.horizontal, 18).padding(.bottom, 12)

            if let error = model.error ?? model.snapshot?.edit_error {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
                    Text(error).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled)
                }.font(.callout).padding(12).background(.orange.opacity(0.06)).padding(.horizontal, 18)
            }
            if model.snapshot == nil {
                ContentUnavailableView(
                    "Waiting for timeline",
                    systemImage: "film.stack",
                    description: Text(model.waitingReason ?? "Open Final Cut Pro and run make ui.")
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.filteredSlices.isEmpty && model.slices.isEmpty {
                ContentUnavailableView("No speech remaining", systemImage: "text.magnifyingglass")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                WorkflowCardStack()
            }
            HStack(spacing: 7) {
                ZStack {
                    if model.busy { ProgressView().controlSize(.mini) }
                    else { Circle().fill(model.needsRefresh ? Color.orange : Color.secondary.opacity(0.5)).frame(width: 5, height: 5) }
                }.frame(width: 12, height: 12)
                Text(model.busy ? "Saving to Final Cut…" : model.needsRefresh ? "Check timeline · refresh" : "\(model.slices.count) slices · \(timecode(model.snapshot?.duration ?? 0))")
                    .lineLimit(1).help(model.message)
                Spacer(minLength: 4)
                Text("↵ split  ⌫ trim").foregroundStyle(.tertiary)
            }.font(.system(size: 11)).foregroundStyle(.secondary).padding(.horizontal, 22).padding(.vertical, 15)
        }
        .frame(minWidth: 300, idealWidth: 400, maxWidth: .infinity, minHeight: 380, maxHeight: .infinity)
        .task {
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(2)) } catch { break }
                if isVisible { await model.syncIfIdle() }
            }
        }
    }
}

private final class CardGeometry {
    var frames: [String: CGRect] = [:]
}

private struct CardFrames: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

struct WorkflowCardStack: View {
    @Environment(StoryModel.self) private var model
    @State private var order: [String]?
    @State private var draggingID: String?
    @State private var geometry = CardGeometry()
    @State private var origin = CGRect.zero
    @State private var translation = CGSize.zero
    @State private var pointer = CGPoint.zero
    @State private var viewportHeight: CGFloat = 0
    @State private var cancelled = false
    @GestureState private var gestureActive = false

    private var rows: [TimelineSlice] {
        guard let order, model.search.isEmpty else { return model.filteredSlices }
        let byID = Dictionary(uniqueKeysWithValues: model.slices.map { ($0.id, $0) })
        return order.compactMap { byID[$0] }
    }
    private let spring = Animation.spring(response: 0.3, dampingFraction: 0.84)

    var body: some View {
        ScrollViewReader { proxy in
            GeometryReader { viewport in
                ZStack(alignment: .topLeading) {
                    ScrollView {
                        LazyVStack(spacing: 8) {
                            ForEach(rows) { slice in
                                WorkflowSliceCard(slice: slice, drag: dragGesture(for: slice.id))
                                    .opacity(draggingID == slice.id ? 0.0 : 1)
                                    .background {
                                        if draggingID == slice.id {
                                            RoundedRectangle(cornerRadius: 15).fill(.primary.opacity(0.035))
                                        }
                                    }
                                    .background(GeometryReader { geometry in
                                        Color.clear.preference(key: CardFrames.self, value: [slice.id: geometry.frame(in: .named("cardViewport"))])
                                    })
                                    .id(slice.id)
                                    .transition(.opacity.combined(with: .scale(scale: 0.97)))
                            }
                        }.padding(.horizontal, 18).padding(.vertical, 4)
                    }
                    .scrollIndicators(.hidden)
                    .onPreferenceChange(CardFrames.self) { geometry.frames = $0 }
                    .animation(spring, value: rows.map(\.id))
                    if let id = draggingID, let slice = model.slices.first(where: { $0.id == id }) {
                        WorkflowSliceCard(slice: slice, lifted: true, drag: dragGesture(for: id))
                            .frame(width: origin.width)
                            .scaleEffect(1.015)
                            .shadow(color: .black.opacity(0.12), radius: 16, y: 8)
                            .offset(x: origin.minX + translation.width * 0.12, y: origin.minY + translation.height)
                            .allowsHitTesting(false)
                            .zIndex(1)
                    }
                    if model.snapshot != nil && rows.isEmpty {
                        ContentUnavailableView(model.search.isEmpty ? "No speech remaining" : "No matching words", systemImage: "text.magnifyingglass")
                    }
                }
                .coordinateSpace(name: "cardViewport")
                .clipped()
                .onAppear { viewportHeight = viewport.size.height }
                .onChange(of: viewport.size.height) { _, height in viewportHeight = height }
            }
            .task(id: draggingID) {
                guard draggingID != nil else { return }
                while !Task.isCancelled {
                    do { try await Task.sleep(for: .milliseconds(180)) } catch { return }
                    guard draggingID != nil else { return }
                    let visible = rows.filter { (geometry.frames[$0.id]?.maxY ?? -1) > 0 && (geometry.frames[$0.id]?.minY ?? .infinity) < viewportHeight }
                    if pointer.y < 35, let first = visible.first,
                       let index = rows.firstIndex(where: { $0.id == first.id }), index > 0 {
                        withAnimation(.linear(duration: 0.18)) { proxy.scrollTo(rows[index - 1].id, anchor: .top) }
                    } else if pointer.y > viewportHeight - 35, let last = visible.last,
                              let index = rows.firstIndex(where: { $0.id == last.id }), index + 1 < rows.count {
                        withAnimation(.linear(duration: 0.18)) { proxy.scrollTo(rows[index + 1].id, anchor: .bottom) }
                    }
                    moveAside(at: pointer.y)
                }
            }
        }
        .onChange(of: gestureActive) { _, active in
            if !active { cancelDrag(); cancelled = false }
        }
        .onExitCommand { cancelDrag() }
        .onChange(of: model.search) { _, _ in cancelDrag() }
        .onDisappear { cancelDrag() }
    }

    private func dragGesture(for id: String) -> some Gesture {
        DragGesture(minimumDistance: 4, coordinateSpace: .named("cardViewport"))
            .updating($gestureActive) { _, active, _ in active = true }
            .onChanged { value in
                guard model.canEdit, model.search.isEmpty, !cancelled else { return }
                if draggingID == nil {
                    guard let frame = geometry.frames[id] else { return }
                    origin = frame
                    order = model.slices.map(\.id)
                    draggingID = id
                    model.dragging = true
                }
                translation = value.translation
                pointer = value.location
                moveAside(at: value.location.y)
            }
            .onEnded { _ in
                guard !cancelled else { cancelDrag(); cancelled = false; return }
                guard let id = draggingID, let order,
                      let originalIndex = model.slices.firstIndex(where: { $0.id == id }),
                      let finalIndex = order.firstIndex(of: id) else { cancelDrag(); return }
                withAnimation(spring) {
                    model.moveCards(from: IndexSet(integer: originalIndex), to: finalIndex > originalIndex ? finalIndex + 1 : finalIndex)
                    draggingID = nil
                    self.order = nil
                    model.dragging = false
                }
            }
    }

    private func moveAside(at y: CGFloat) {
        guard let id = draggingID, var ids = order, let from = ids.firstIndex(of: id) else { return }
        var destination = from
        for (index, other) in ids.enumerated() where other != id {
            guard let frame = geometry.frames[other] else { continue }
            if index > from && y > frame.midY { destination = max(destination, index) }
            if index < from && y < frame.midY { destination = min(destination, index) }
        }
        guard destination != from else { return }
        ids.remove(at: from)
        ids.insert(id, at: destination)
        withAnimation(spring) { order = ids }
    }

    private func cancelDrag() {
        if draggingID != nil { cancelled = true }
        withAnimation(spring) { draggingID = nil; order = nil; model.dragging = false }
    }
}

struct WorkflowSliceCard<Drag: Gesture>: View {
    let slice: TimelineSlice
    var lifted = false
    let drag: Drag
    @Environment(StoryModel.self) private var model
    @State private var selection = NSRange(location: 0, length: 0)
    @State private var focused = false
    @State private var hovered = false

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 8) {
                HStack(spacing: 7) {
                    Image(systemName: "line.3.horizontal").font(.system(size: 9, weight: .medium)).opacity(hovered || lifted ? 0.7 : 0.3)
                    Text(timecode(slice.start)).monospacedDigit()
                    Spacer()
                    Text(String(format: "%.1fs", slice.end - slice.start)).foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle()).gesture(drag).help("Drag to reorder")
                Button(role: .destructive) { Task { await model.perform("delete", id: slice.id) } } label: {
                    Image(systemName: "trash").frame(width: 18, height: 18)
                }
                .buttonStyle(.plain).opacity(hovered || focused ? 0.75 : 0.35)
                .disabled(!model.canEdit).help("Delete slice").accessibilityLabel("Delete slice at \(timecode(slice.start))")
            }.font(.system(size: 11)).foregroundStyle(.secondary)
            CardTextView(text: slice.text, selection: $selection, focused: $focused,
                         enabled: model.canEdit && !lifted,
                         onSplit: { edit("split") }, onDelete: { range in edit("trim", range: range) },
                         onUnsupportedEdit: { model.error = "Select words and press Delete to trim recorded speech. Enter splits a card." })
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 15).padding(.vertical, 13)
        .background(Color(nsColor: .textBackgroundColor).opacity(lifted ? 0.95 : focused ? 0.64 : hovered ? 0.5 : 0.3), in: RoundedRectangle(cornerRadius: 15))
        .overlay(RoundedRectangle(cornerRadius: 15).strokeBorder(focused ? Color.accentColor.opacity(0.24) : Color.white.opacity(0.2), lineWidth: 0.6))
        .onHover { hovered = $0 }
        .contextMenu {
            Button("Split at Cursor") { edit("split") }.disabled(!model.canEdit)
            Button("Move to End") { model.moveCardToEnd(slice.id) }.disabled(!model.canEdit || !model.search.isEmpty)
            Button("Delete Slice", role: .destructive) { Task { await model.perform("delete", id: slice.id) } }.disabled(!model.canEdit)
        }
    }

    private func edit(_ action: String, range: NSRange? = nil) {
        guard model.canEdit else { return }
        model.select(slice.id)
        model.selection = range ?? selection
        let words = action == "split" ? model.splitWords : model.selectedWords
        guard !words.isEmpty else { return }
        Task { await model.perform(action, id: slice.id, words: words) }
    }
}
