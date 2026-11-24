import SwiftUI

/// Panel chrome with a JBIcon glyph (PanelChrome takes an SF symbol name only).
struct ExtrasShell<Actions: View, Content: View>: View {
    let title: String
    let glyph: JBGlyph
    @ViewBuilder var actions: Actions
    @ViewBuilder var content: Content

    var body: some View {
        VStack(spacing: JB.s3) {
            HStack(spacing: 10) {
                JBIcon(glyph, size: 16).foregroundStyle(JB.accent)
                Text(title).font(.jb(14, .semibold)).tracking(-0.2).foregroundStyle(JB.ink)
                Spacer()
                actions
            }
            .padding(.horizontal, JB.s4).padding(.vertical, 10)
            .jbSurface(.chrome, radius: JB.rPanel)
            content.frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

/// Small icon-only toolbar button.
struct ExtrasIconButton: View {
    let symbol: String
    let help: String
    var active = false
    let action: () -> Void
    var body: some View {
        Button(action: action) { Image(systemName: symbol) }
            .buttonStyle(JBIconButtonStyle(size: 26, active: active))
            .help(help)
    }
}

/// A row that reveals its actions on hover (always visible when `pinned`).
struct HoverActionRow<Lead: View, Actions: View>: View {
    var pinned = false
    @ViewBuilder var lead: Lead
    @ViewBuilder var actions: Actions
    @State private var hovering = false
    var body: some View {
        HStack(spacing: JB.s3) {
            lead
            Spacer(minLength: 8)
            actions.opacity(hovering || pinned ? 1 : 0).allowsHitTesting(hovering || pinned)
        }
        .jbCard(padding: JB.s3)
        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(hovering ? JB.hover.opacity(0.35) : .clear).allowsHitTesting(false))
        .onHover { h in withAnimation(JB.fade) { hovering = h } }
    }
}

/// "LABEL" eyebrow over a control, for forms.
struct ExtrasField<Control: View>: View {
    let label: String
    @ViewBuilder var control: Control
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label).jbEyebrow()
            control
        }
    }
}

/// Sheet header: title left, actions right, hairline beneath.
struct ExtrasSheetHeader<Actions: View>: View {
    let title: String
    @ViewBuilder var actions: Actions
    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text(title).font(.jb(14, .semibold)).foregroundStyle(JB.ink)
                Spacer()
                actions
            }.padding(JB.s3)
            Rectangle().fill(JB.line).frame(height: 1)
        }
    }
}

/// Choose a folder on this Mac into `path`.
func chooseFolder(into path: Binding<String>) {
    #if os(macOS)
    let p = NSOpenPanel()
    p.canChooseDirectories = true; p.canChooseFiles = false; p.allowsMultipleSelection = false
    if p.runModal() == .OK, let url = p.url { path.wrappedValue = url.path }
    #endif
}

/// Agent / model / computer / folder: shared by Night Shift and coding-agent tasks.
struct CodingTargetFields: View {
    @Binding var agent: String
    @Binding var model: String
    @Binding var host: String
    @Binding var repo: String
    var hosts: [NightHost] = []
    static let models = ["claude": ["opus", "sonnet", "haiku"], "codex": ["gpt-5-codex", "gpt-5"],
                         "opencode": ["qwen3-coder:30b", "llama3.3:70b", "deepseek-v3.2"],
                         "agy": ["gemini-4-neon-high", "gemini-4-carbon-medium", "gemini-3.8-flash-high"], "any": []]
    @FocusState private var repoFocus: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: JB.s3) {
            HStack(spacing: JB.s3) {
                ExtrasField(label: "Agent") {
                    Picker("", selection: $agent) {
                        Text("Claude Code").tag("claude"); Text("Codex").tag("codex"); Text("OpenCode").tag("opencode"); Text("Antigravity").tag("agy"); Text("Any").tag("any")
                    }.labelsHidden()
                }
                ExtrasField(label: "Model") {
                    Picker("", selection: $model) {
                        Text("Default").tag("")
                        ForEach(Self.models[agent] ?? [], id: \.self) { Text($0).tag($0) }
                    }.labelsHidden()
                }
                if hosts.count > 1 {
                    ExtrasField(label: "Runs on") {
                        Picker("", selection: $host) {
                            ForEach(hosts) { h in Text(h.up == false ? h.label + " (offline)" : h.label).tag(h.id) }
                        }.labelsHidden()
                    }
                }
            }
            .onChange(of: agent) { model = "" }
            .onAppear { if host.isEmpty, let l = hosts.first(where: { $0.local == true }) { host = l.id } }
            ExtrasField(label: "Folder") {
                HStack(spacing: 6) {
                    TextField("/path/to/repo", text: $repo).focused($repoFocus).jbField(focused: repoFocus)
                    if host.isEmpty || hosts.first(where: { $0.id == host })?.local == true {
                        ExtrasIconButton(symbol: "folder", help: "Choose folder") { chooseFolder(into: $repo) }
                    }
                }
            }
        }
    }
}
