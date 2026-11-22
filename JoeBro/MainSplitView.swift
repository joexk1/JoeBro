import SwiftUI

/// Custom split layout: flat panels over the backdrop, sidebar on the left.
struct MainSplitView: View {
    @Environment(AppStore.self) private var store
    @State private var sidebarVisible = true
    @State private var nav = AgentNav.shared
    @State private var showPalette = false
    @State private var isFullscreen =
        NSApp.windows.contains { $0.styleMask.contains(.fullScreen) }

    var body: some View {
        ZStack(alignment: .top) {
            HStack(spacing: 0) {
                // Width animates (no insert/remove), so the chat panel and its
                // header slide in one smooth motion instead of jumping.
                SidebarView()
                    .frame(width: 260)
                    .jbSurface(.panel, radius: JB.rPanel)
                    .frame(width: sidebarVisible ? 260 : 0, alignment: .trailing)
                    // Clips only while hiding, so the appearance panel can float past the edge.
                    .clipShape(Rectangle().inset(by: sidebarVisible ? -600 : 0))
                    .opacity(sidebarVisible ? 1 : 0)
                    .padding(.trailing, sidebarVisible ? 10 : 0)
                    .allowsHitTesting(sidebarVisible)
                    .accessibilityHidden(!sidebarVisible)
                    .zIndex(1)
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
        .onChange(of: store.selectedSessionID) { nav.close() }   // picking a chat leaves the agent
        .task { nav.startPolling() }
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
            if let id = nav.openID {
                CodingAgentConversation(agentID: id, sidebarVisible: $sidebarVisible)
            } else {
                ChatView(sidebarVisible: $sidebarVisible)
            }
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
