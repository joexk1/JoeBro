import SwiftUI

/// Custom split layout — floating liquid-glass panels over the wallpaper,
/// rather than the opaque system NavigationSplitView chrome.
struct MainSplitView: View {
    @Environment(AppStore.self) private var store
    @State private var sidebarVisible = true
    @State private var isFullscreen =
        NSApp.windows.contains { $0.styleMask.contains(.fullScreen) }

    var body: some View {
        HStack(spacing: 12) {
            if sidebarVisible {
                SidebarView()
                    .frame(width: 268)
                    .joeGlassRect(18)
                    .shadow(color: .black.opacity(0.16), radius: 18, y: 8)
                    .transition(.move(edge: .leading).combined(with: .opacity))
            }
            detail
                .id(store.activeTab)
                .transition(.asymmetric(
                    insertion: .opacity.combined(with: .offset(y: 8)),
                    removal: .opacity))
        }
        .padding(12)
        // The panels float BELOW the traffic lights instead of colliding
        // with them; fullscreen hides the lights so the band collapses.
        .padding(.top, isFullscreen ? 2 : 26)
        .animation(.spring(duration: 0.32), value: sidebarVisible)
        .animation(.spring(duration: 0.34), value: store.activeTab)
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didEnterFullScreenNotification)) { note in
            if (note.object as? NSWindow)?.isMainWindow == true { isFullscreen = true }
        }
        .onReceive(NotificationCenter.default.publisher(for: NSWindow.didExitFullScreenNotification)) { note in
            if (note.object as? NSWindow)?.isMainWindow == true { isFullscreen = false }
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch store.activeTab {
        case .chat:
            ChatView(sidebarVisible: $sidebarVisible)
        case .email:
            EmailPanel()
        case .calendar:
            CalendarPanel()
        case .brain:
            BrainPanel()
        case .notes:
            NotesPanel()
        case .tasks:
            TasksPanel()
        case .skills:
            SkillsPanel()
        case .tools:
            ToolsPanel()
        case .aiCheck:
            AICheckPanel()
        case .research:
            ResearchPanel()
        }
    }
}
