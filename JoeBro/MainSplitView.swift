import SwiftUI

/// Custom split layout: flat panels over the backdrop, sidebar on the left.
struct MainSplitView: View {
    @Environment(AppStore.self) private var store
    @State private var sidebarVisible = true
    @State private var showPalette = false
    @State private var isFullscreen =
        NSApp.windows.contains { $0.styleMask.contains(.fullScreen) }

    var body: some View {
        ZStack(alignment: .top) {
            HStack(spacing: 10) {
                if sidebarVisible {
                    SidebarView()
                        .frame(width: 260)
                        .jbSurface(.panel, radius: JB.rPanel)
                        .transition(.move(edge: .leading).combined(with: .opacity))
                }
                detail
                    .id(store.activeTab)
                    .transition(.opacity.combined(with: .offset(y: 6)))
            }
            .padding(10)
            // The panels float BELOW the traffic lights instead of colliding
            // with them; fullscreen hides the lights so the band collapses.
            .padding(.top, isFullscreen ? 2 : 26)
            .animation(JB.smooth, value: sidebarVisible)
            .animation(JB.smooth, value: store.activeTab)

            if showPalette {
                Color.black.opacity(0.22)
                    .ignoresSafeArea()
                    .onTapGesture { showPalette = false }
                    .transition(.opacity)
                CommandPalette(isPresented: $showPalette,
                               toggleSidebar: { sidebarVisible.toggle() })
                    .padding(.top, 96)
                    .transition(.opacity.combined(with: .offset(y: -6)))
            }
        }
        .animation(JB.snap, value: showPalette)
        .onReceive(NotificationCenter.default.publisher(for: .jbOpenPalette)) { _ in showPalette.toggle() }
        .onReceive(NotificationCenter.default.publisher(for: .jbToggleSidebar)) { _ in sidebarVisible.toggle() }
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
        case .brain, .memory:
            SharedMemoryPanel()
        case .nightShift:
            NightShiftPanel()
        case .notes:
            NotesPanel()
        case .tasks:
            TasksPanel()
        case .skills:
            SkillsPanel()
        case .agents:
            AgentsPanel()
        case .tools:
            ToolsPanel()
        case .plugins:
            PluginPanelsView()
        case .aiCheck:
            AICheckPanel()
        case .research:
            ResearchPanel()
        case .inbox:
            InboxPanel()
        }
    }
}
