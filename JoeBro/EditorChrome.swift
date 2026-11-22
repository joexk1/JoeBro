import SwiftUI

/// One flat tab: hairline-separated, accent tick when active, modified dot
/// that turns into a close button on hover.
struct EditorTab: View {
    let doc: EditorDoc
    let isActive: Bool
    let select: () -> Void
    let close: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 6) {
            if doc.aiWriting { JBSpinner(size: 10, tint: JB.accent) }
            Text(doc.title)
                .font(.jb(12, isActive ? .medium : .regular))
                .foregroundStyle(isActive ? JB.ink : (hovering ? JB.ink : JB.ink2))
                .lineLimit(1)
                .frame(maxWidth: 150)
            ZStack {
                if hovering {
                    Button(action: close) {
                        Image(systemName: "xmark").font(.jb(8, .semibold)).foregroundStyle(JB.ink2)
                    }
                    .buttonStyle(.plain)
                    .transition(.opacity)
                } else if doc.dirty {
                    Circle().fill(JB.accent).frame(width: 5, height: 5).transition(.opacity)
                }
            }
            .frame(width: 14, height: 14)
        }
        .padding(.leading, 12)
        .padding(.trailing, 6)
        .frame(height: 36)
        .background(hovering && !isActive ? JB.hover.opacity(0.6) : .clear)
        .overlay(alignment: .bottom) {
            Rectangle().fill(JB.accent).frame(height: 2).opacity(isActive ? 1 : 0)
        }
        .contentShape(Rectangle())
        .onHover { h in withAnimation(JB.fade) { hovering = h } }
        .onTapGesture(perform: select)
        .animation(JB.snap, value: isActive)
    }
}

/// Copy button: icon swaps to a check for 1.2s.
struct CopyIconButton: View {
    let text: String
    var help = "Copy document"
    @State private var done = false

    var body: some View {
        Button {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            withAnimation(JB.fade) { done = true }
            Task { try? await Task.sleep(for: .seconds(1.2)); withAnimation(JB.fade) { done = false } }
        } label: {
            Image(systemName: done ? "checkmark" : "doc.on.doc")
                .foregroundStyle(done ? JB.live : JB.ink2)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(JBIconButtonStyle(size: 26))
        .help(help)
    }
}
