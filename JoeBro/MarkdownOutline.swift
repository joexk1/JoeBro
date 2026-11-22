import SwiftUI

/// Heading outline + one-line stats for the open document. Updated on a short
/// debounce so typing never pays for a re-scan per keystroke.
@Observable
final class DocOutlineModel {
    var headings: [MDHeading] = []
    var active: Int? = nil
    var stats = ""
    @ObservationIgnored private var task: Task<Void, Never>?

    func refresh(text: String, markdown: Bool, immediately: Bool = false) {
        task?.cancel()
        task = Task { [weak self] in
            if !immediately { try? await Task.sleep(for: .milliseconds(250)) }
            guard !Task.isCancelled else { return }
            let result: ([MDHeading], String) = await Task.detached(priority: .utility) {
                markdown
                    ? (markdownOutline(text), markdownStats(text))
                    : ([], "\(text.utf8.reduce(1) { $1 == 10 ? $0 + 1 : $0 }) lines")
            }.value
            guard !Task.isCancelled, let self else { return }
            headings = result.0
            stats = result.1
            if let a = active, a >= headings.count { active = nil }
        }
    }

    /// Active section for the first visible UTF-16 offset.
    func track(offset: Int) {
        let id = headings.last(where: { $0.offset <= offset })?.id ?? headings.first?.id
        if id != active { active = id }
    }
}

/// Right-hand outline: indented headings, accent tick on the current section.
struct DocOutlineView: View {
    let model: DocOutlineModel
    let onSelect: (MDHeading) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Outline").jbEyebrow()
                Spacer()
                JBKbd("⌥⌘O")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            Rectangle().fill(JB.line).frame(height: 1)
            if model.headings.isEmpty {
                Spacer()
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 1) {
                            ForEach(model.headings) { h in
                                row(h).id(h.id)
                            }
                        }
                        .padding(6)
                    }
                    .onChange(of: model.active) { _, new in
                        if let new { withAnimation(JB.gentle) { proxy.scrollTo(new, anchor: .center) } }
                    }
                }
            }
        }
        .frame(width: 200)
        .background(JB.raised.opacity(0.5))
        .overlay(alignment: .leading) { Rectangle().fill(JB.line).frame(width: 1) }
    }

    private func row(_ h: MDHeading) -> some View {
        let selected = model.active == h.id
        return Button { onSelect(h) } label: {
            Text(h.title.isEmpty ? "—" : h.title)
                .font(.jb(12, h.level <= 2 ? .medium : .regular))
                .foregroundStyle(selected ? JB.ink : (h.level <= 2 ? JB.ink2 : JB.ink3))
                .lineLimit(1)
                .padding(.leading, CGFloat(max(0, h.level - 1)) * 10 + 8)
                .padding(.trailing, 8)
                .frame(maxWidth: .infinity, minHeight: 24, alignment: .leading)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .jbHoverRow(selected: selected)
    }
}
