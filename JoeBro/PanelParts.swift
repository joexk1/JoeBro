import SwiftUI
import AppKit

/// Shared parts for the workspace panels (state views, hover actions, copy feedback).

struct PanelLoading: View {
    var body: some View {
        JBSpinner(size: 14).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct PanelEmpty: View {
    var art: JBEmptyState.Art = .inbox
    let text: String
    var body: some View { JBEmptyState(art: art, text: text) }
}

struct PanelFailure: View {
    let title: String
    var detail: String? = nil
    var body: some View {
        VStack(spacing: 6) {
            JBEmptyState(art: .plug, text: title)
            if let detail, !detail.isEmpty {
                Text(detail).font(.jbMono(11)).foregroundStyle(JB.ink3).lineLimit(3)
                    .padding(.horizontal, 24).padding(.bottom, 16)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Copy button: icon swaps to a check for 1.2 s.
struct CopyCheckButton: View {
    let text: String
    var size: CGFloat = 24
    @State private var done = false
    var body: some View {
        Button {
            JBPasteboard.copy(text)
            withAnimation(JB.snap) { done = true }
            Task { try? await Task.sleep(for: .seconds(1.2)); withAnimation(JB.snap) { done = false } }
        } label: {
            Image(systemName: done ? "checkmark" : "doc.on.doc")
                .foregroundStyle(done ? JB.live : JB.ink2)
                .contentTransition(.symbolEffect(.replace))
        }
        .buttonStyle(JBIconButtonStyle(size: size))
        .help("Copy")
    }
}

enum JBPasteboard {
    static func copy(_ s: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(s, forType: .string)
    }
}

/// Reveals trailing actions only while the row is hovered.
private struct HoverActions<Actions: View>: ViewModifier {
    @ViewBuilder var actions: () -> Actions
    @State private var hovering = false
    func body(content: Content) -> some View {
        content
            .overlay(alignment: .trailing) {
                if hovering {
                    HStack(spacing: 2) { actions() }
                        .padding(.horizontal, 4)
                        .background(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).fill(JB.panelFill))
                        .transition(.opacity)
                        .padding(.trailing, 6)
                }
            }
            .onHover { h in withAnimation(JB.fade) { hovering = h } }
    }
}

extension View {
    func jbHoverActions<A: View>(@ViewBuilder _ actions: @escaping () -> A) -> some View {
        modifier(HoverActions(actions: actions))
    }
    func jbToggle() -> some View { toggleStyle(JBSwitchStyle()) }
}

/// Small icon-only button in the JB style.
extension ButtonStyle where Self == JBIconButtonStyle {
    static var jbIcon: JBIconButtonStyle { JBIconButtonStyle(size: 26) }
}

/// Bottom strip offering to undo a deferred destructive action.
struct UndoStrip: View {
    let text: String
    let onUndo: () -> Void
    var body: some View {
        HStack(spacing: 10) {
            Text(text).font(.jb(12)).foregroundStyle(JB.ink)
            Button("Undo", action: onUndo).buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
        }
        .padding(.leading, 12).padding(.trailing, 4).padding(.vertical, 4)
        .jbSurface(.raised, radius: JB.rControl)
        .jbFloating()
        .padding(.bottom, 12)
        .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}
