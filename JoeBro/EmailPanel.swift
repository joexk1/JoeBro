import SwiftUI
import UniformTypeIdentifiers

struct EmailPanel: View {
    @Environment(AppStore.self) private var store
    @State private var folder = "INBOX"
    @State private var folders: [String] = ["INBOX"]
    @State private var emails: Loadable<[EmailSummary]> = .loading
    @State private var selected: EmailSummary?
    @State private var detail: Loadable<EmailDetail>?
    @State private var composeMode: ComposeMode?
    @State private var filter = "all"
    @State private var search = ""
    @State private var selection: Set<String> = []
    @State private var emailStatus: IntegrationStatus?
    @AppStorage("emailLoadCount") private var emailLoadCount = 0

    var body: some View {
        PanelChrome(title: "Email", icon: "envelope") {
            HStack(spacing: 2) {
                ForEach([("all", "All"), ("unread", "Unread")], id: \.0) { key, label in
                    Button(label) { withAnimation(JB.snap) { filter = key } }
                        .buttonStyle(JBButtonStyle(kind: filter == key ? .secondary : .ghost, compact: true))
                }
            }

            Menu {
                ForEach(folders, id: \.self) { f in
                    Button(f) { folder = f }
                }
            } label: {
                Label(folder, systemImage: "folder")
                    .font(.jbMono(11.5))
            }
            .menuStyle(.borderlessButton)
            .fixedSize()

            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.jbIcon)
            .help("Refresh")

            Button {
                composeMode = .new
            } label: {
                Image(systemName: "square.and.pencil")
            }
            .buttonStyle(.jbIcon)
            .help("Compose")
        } content: {
            if needsEmailConnection {
                PanelEmpty(art: .plug, text: "Connect email in Settings")
                    .jbSurface(.panel, radius: JB.rPanel)
            } else {
                HStack(spacing: 12) {
                    listColumn
                        .frame(width: 340)
                        .jbSurface(.panel, radius: JB.rPanel)
                    detailColumn
                        .frame(maxWidth: .infinity)
                        .jbSurface(.panel, radius: JB.rPanel)
                }
            }
        }
        .onAppear {
            // Instant paint from the prefetch cache; network refresh follows.
            if case .loading = emails, folder == "INBOX", filter == "all",
               let cached = store.emailCache {
                emails = .loaded(cached)
            }
            if let f = store.emailFoldersCache { folders = f }
        }
        .task(id: folder + filter) { await load() }
        .onChange(of: emailLoadCount) { store.emailCache = nil; Task { await load() } }
        .task {
            emailStatus = try? await APIClient.shared.emailStatus()
            if let f = store.emailFoldersCache { folders = f }
            else { folders = (try? await APIClient.shared.emailFolders()) ?? ["INBOX"] }
        }
        .sheet(item: $composeMode) { mode in
            EmailComposeSheet(mode: mode, folder: folder)
        }
    }

    private var needsEmailConnection: Bool {
        if case .loaded(let list) = emails, list.isEmpty, emailStatus?.configured != true {
            return true
        }
        return false
    }

    private func load() async {
        // Keep showing cached rows while refreshing — no loading flash
        if case .loading = emails {
            if folder == "INBOX", filter == "all", let cached = store.emailCache {
                emails = .loaded(cached)
            }
        }
        do {
            emailStatus = try? await APIClient.shared.emailStatus()
            let r = try await APIClient.shared.emailList(
                folder: folder, filter: filter,
                limit: max(0, emailLoadCount))
            if let err = r.error {
                if case .loaded = emails {} else { emails = .failed(err) }
            } else {
                emails = .loaded(r.emails)
                if folder == "INBOX", filter == "all" { store.emailCache = r.emails }
                syncUnreadBadge()
                // Pin the sidebar dot to the server's authoritative UNSEEN count
                // (not just the unread rows in this limited page), so a stray
                // mis-parsed flag can't leave a phantom dot when all mail is read.
                await store.refreshEmailUnread()
            }
        } catch {
            if case .loaded = emails {} else { emails = .failed(error.localizedDescription) }
        }
    }

    private func searchFiltered(_ list: [EmailSummary]) -> [EmailSummary] {
        // Apply the Unread filter client-side too, so the list is correct even
        // when the IMAP server's SEARCH UNSEEN is unreliable (e.g. the ProtonMail
        // bridge marks everything \Seen) or a stale list lingers from the previous
        // filter. With all mail read, the Unread view is then correctly empty.
        var list = list
        if filter == "unread" { list = list.filter { $0.isRead == false } }
        let q = search.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return list }
        return list.filter {
            ($0.subject ?? "").localizedCaseInsensitiveContains(q)
            || ($0.fromName ?? "").localizedCaseInsensitiveContains(q)
            || ($0.fromAddress ?? "").localizedCaseInsensitiveContains(q)
            || ($0.cachedSummary ?? "").localizedCaseInsensitiveContains(q)
        }
    }

    private var listColumn: some View {
        VStack(spacing: 0) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.jb(11))
                    .foregroundStyle(JB.ink3)
                TextField("Search", text: $search)
                    .textFieldStyle(.plain)
                    .font(.jb(12.5))
                if !search.isEmpty {
                    Button { search = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.jb(11))
                            .foregroundStyle(JB.ink3)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 9)
            .overlay(alignment: .bottom) { Rectangle().fill(JB.line).frame(height: 1) }
            if !selection.isEmpty {
                HStack(spacing: 10) {
                    Text("\(selection.count) selected")
                        .font(.jbMono(11, .medium))
                    Button(allSelected ? "None" : "All") {
                        if case .loaded(let list) = emails {
                            withAnimation(JB.snap) {
                                selection = allSelected ? [] : Set(list.map(\.uid))
                            }
                        }
                    }
                    .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                    Spacer()
                    bulkButton("envelope.open", "Mark read") { uid, f in
                        await APIClient.shared.emailMarkRead(uid: uid, folder: f, read: true)
                    }
                    bulkButton("envelope.badge", "Mark unread") { uid, f in
                        await APIClient.shared.emailMarkRead(uid: uid, folder: f, read: false)
                    }
                    bulkButton("archivebox", "Archive", removes: true) { uid, f in
                        try? await APIClient.shared.emailArchive(uid: uid, folder: f)
                    }
                    bulkButton("trash", "Delete", removes: true) { uid, f in
                        try? await APIClient.shared.emailDelete(uid: uid, folder: f)
                    }
                    Button {
                        withAnimation(JB.snap) { selection = [] }
                    } label: {
                        Image(systemName: "xmark")
                    }
                    .buttonStyle(.jbIcon)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(JB.accentSoft)
                .transition(.move(edge: .top).combined(with: .opacity))
                Rectangle().fill(JB.line).frame(height: 1)
            }
            listBody
        }
        .animation(JB.snap, value: selection.isEmpty)
        .focusable()
        .focusEffectDisabled()
        .onKeyPress(.downArrow) { step(1); return .handled }
        .onKeyPress(.upArrow) { step(-1); return .handled }
        .onKeyPress(.return) {
            if let m = selected { open(m) }
            return .handled
        }
    }

    /// Move the open message up or down the visible list.
    private func step(_ delta: Int) {
        guard case .loaded(let all) = emails else { return }
        let list = searchFiltered(all)
        guard !list.isEmpty else { return }
        let cur = list.firstIndex { $0.uid == selected?.uid } ?? (delta > 0 ? -1 : list.count)
        let next = min(max(cur + delta, 0), list.count - 1)
        open(list[next])
    }

    private func bulkButton(_ icon: String, _ help: String, removes: Bool = false,
                            _ action: @escaping (String, String) async -> Void) -> some View {
        Button {
            let uids = selection
            let f = folder
            selection = []
            if removes {
                for uid in uids { removeLocally(uid) }
            }
            Task {
                for uid in uids { await action(uid, f) }
                if !removes { await load() }
                await store.refreshEmailUnread()   // mark-read/unread/archive all shift the count
            }
        } label: {
            Image(systemName: icon)
        }
        .buttonStyle(.jbIcon)
        .help(help)
    }

    private var listBody: some View {
        Group {
            switch emails {
            case .loading:
                PanelLoading()
            case .failed(let e):
                PanelFailure(title: "Couldn't load", detail: e)
            case .loaded(let list) where searchFiltered(list).isEmpty:
                if emailStatus?.configured == true {
                    PanelEmpty(art: filter == "unread" ? .done : .inbox, text: filter == "unread" ? "All read" : "No emails")
                } else {
                    PanelEmpty(art: .plug, text: "Connect email in Settings")
                }
            case .loaded(let list):
                ScrollView {
                    LazyVStack(spacing: 1) {
                        ForEach(searchFiltered(list)) { mail in
                            EmailRow(mail: mail,
                                     isSelected: selected?.uid == mail.uid,
                                     isChecked: selection.contains(mail.uid),
                                     selecting: !selection.isEmpty,
                                     onToggleCheck: { toggleSelect(mail.uid) },
                                     onTap: {
                                        if selection.isEmpty { open(mail) }
                                        else { toggleSelect(mail.uid) }
                                     })
                                .transition(.opacity)
                                .contextMenu {
                                    Button(selection.contains(mail.uid) ? "Deselect" : "Select") {
                                        toggleSelect(mail.uid)
                                    }
                                    Button(mail.isRead == true ? "Mark Unread" : "Mark Read") {
                                        setRead(mail.uid, read: mail.isRead != true)
                                    }
                                    Button("Archive") {
                                        removeLocally(mail.uid)
                                        Task { try? await APIClient.shared.emailArchive(uid: mail.uid, folder: folder) }
                                    }
                                    Button("Delete", role: .destructive) {
                                        removeLocally(mail.uid)
                                        Task { try? await APIClient.shared.emailDelete(uid: mail.uid, folder: folder) }
                                    }
                                }
                        }
                    }
                    .padding(6)
                    .animation(JB.smooth, value: list.map(\.uid))
                }
            }
        }
    }

    /// Optimistic read-state flip — instant UI, server call in background.
    private func setRead(_ uid: String, read: Bool) {
        // In the Unread view, a message that's now read no longer belongs —
        // drop it from the list so the filter stays honest instead of showing
        // read mail. Keep the open detail (the user may have just opened it).
        if filter == "unread", read {
            if case .loaded(var list) = emails {
                list.removeAll { $0.uid == uid }
                emails = .loaded(list)
            }
            Task {
                await APIClient.shared.emailMarkRead(uid: uid, folder: folder, read: true)
                await store.refreshEmailUnread()
            }
            return
        }
        if case .loaded(var list) = emails,
           let i = list.firstIndex(where: { $0.uid == uid }) {
            list[i].isRead = read
            emails = .loaded(list)
            if folder == "INBOX", filter == "all" { store.emailCache = list }
            syncUnreadBadge()
        }
        Task {
            await APIClient.shared.emailMarkRead(uid: uid, folder: folder, read: read)
            await store.refreshEmailUnread()   // authoritative count once the server's caught up
        }
    }

    private func removeLocally(_ uid: String) {
        if case .loaded(var list) = emails {
            list.removeAll { $0.uid == uid }
            emails = .loaded(list)
            if folder == "INBOX", filter == "all" { store.emailCache = list }
            syncUnreadBadge()
        }
        if selected?.uid == uid { selected = nil; detail = nil }
    }

    /// Push the inbox's unread count to the sidebar badge immediately. Only the
    /// full inbox view is authoritative for the total; other folders/filters
    /// leave it to the background poll (refreshEmailUnread).
    private func syncUnreadBadge() {
        guard folder == "INBOX", filter == "all", case .loaded(let list) = emails else { return }
        store.applyEmailUnread(list.filter { $0.isRead == false }.count)
    }

    private var allSelected: Bool {
        if case .loaded(let list) = emails { return !list.isEmpty && selection.count == list.count }
        return false
    }

    private func toggleSelect(_ uid: String) {
        withAnimation(JB.snap) {
            if selection.contains(uid) { selection.remove(uid) } else { selection.insert(uid) }
        }
    }

    private func open(_ mail: EmailSummary) {
        selected = mail
        detail = .loading
        Task {
            do {
                let d = try await APIClient.shared.emailRead(uid: mail.uid, folder: folder)
                if let err = d.error { detail = .failed(err) }
                else {
                    var opened = mail
                    opened.isRead = true
                    selected = opened
                    detail = .loaded(d)
                    setRead(mail.uid, read: true)
                }
            } catch {
                detail = .failed(error.localizedDescription)
            }
        }
    }

    @ViewBuilder
    private var detailColumn: some View {
        switch detail {
        case nil:
            PanelEmpty(art: .inbox, text: "Select an email")
        case .loading:
            PanelLoading()
        case .failed(let e):
            PanelFailure(title: "Couldn't open", detail: e)
        case .loaded(let d):
            EmailDetailView(detail: d, folder: folder) { mode in
                composeMode = mode
            } onChanged: {
                Task { await load() }
            }
        }
    }
}

private struct EmailRow: View {
    let mail: EmailSummary
    let isSelected: Bool
    var isChecked = false
    var selecting = false
    var onToggleCheck: () -> Void = {}
    var onTap: () -> Void = {}
    @State private var hovering = false

    private var unread: Bool { mail.isRead == false }

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            // Accent dot = unread; the dot swaps for a check circle on hover or while selecting.
            let showCheck = selecting || hovering || isChecked
            ZStack {
                Circle().fill(unread ? JB.accent : .clear).frame(width: 6, height: 6)
                    .opacity(showCheck ? 0 : 1)
                Image(systemName: isChecked ? "checkmark.circle.fill" : "circle")
                    .font(.jb(14))
                    .foregroundStyle(isChecked ? JB.accent : JB.ink3)
                    .opacity(showCheck ? 1 : 0)
                    .contentShape(Rectangle())
                    .onTapGesture(perform: onToggleCheck)
                    .allowsHitTesting(showCheck)
            }
            .frame(width: 16, height: 18)
            rowContent
                .contentShape(Rectangle())
                .onTapGesture(perform: onTap)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 7)
        .background(isChecked ? JB.accentSoft : .clear, in: RoundedRectangle(cornerRadius: JB.rControl, style: .continuous))
        .jbHoverRow(selected: isSelected)
        .onHover { h in withAnimation(JB.fade) { hovering = h } }
    }

    private var rowContent: some View {
        VStack(alignment: .leading, spacing: 1) {
            HStack(spacing: 6) {
                Text(mail.fromName ?? mail.fromAddress ?? "Unknown")
                    .font(.jb(13, unread ? .semibold : .regular))
                    .foregroundStyle(unread ? JB.ink : JB.ink2)
                    .lineLimit(1)
                Spacer(minLength: 4)
                if mail.isFlagged == true {
                    Image(systemName: "star.fill").font(.jb(9)).foregroundStyle(JB.warn)
                }
                if mail.hasAttachments == true {
                    Image(systemName: "paperclip").font(.jb(9)).foregroundStyle(JB.ink3)
                }
                Text(relativeDate(mail.date))
                    .font(.jbMono(10.5))
                    .foregroundStyle(JB.ink3)
            }
            Text(mail.subject ?? "(no subject)")
                .font(.jb(12.5, unread ? .medium : .regular))
                .foregroundStyle(unread ? JB.ink : JB.ink2)
                .lineLimit(1)
            if let s = mail.cachedSummary {
                Text(s)
                    .font(.jb(11.5))
                    .foregroundStyle(JB.ink3)
                    .lineLimit(1)
            }
        }
    }
}

private struct EmailDetailView: View {
    let detail: EmailDetail
    let folder: String
    let onCompose: (ComposeMode) -> Void
    let onChanged: () -> Void
    @State private var previewURL: URL?
    @State private var summary: String?
    @State private var summarizing = false
    @State private var drafting = false
    @State private var aiError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 4) {
                    Text(detail.subject ?? "(no subject)")
                        .font(.jb(20, .semibold))
                        .tracking(-0.3)
                        .textSelection(.enabled)
                    Spacer(minLength: 8)
                    Button { summarize() } label: {
                        if summarizing { JBSpinner() } else { JBIcon(.spark, size: 15) }
                    }
                    .help("Summarize")
                    Button { draftReply() } label: {
                        if drafting { JBSpinner() } else { Image(systemName: "arrowshape.turn.up.left.2") }
                    }
                    .help("Draft a reply")
                    Rectangle().fill(JB.line).frame(width: 1, height: 14)
                    Button { onCompose(.reply(detail)) } label: { Image(systemName: "arrowshape.turn.up.left") }
                        .help("Reply")
                    Button { onCompose(.forward(detail)) } label: { Image(systemName: "arrowshape.turn.up.right") }
                        .help("Forward")
                    Menu {
                        Button("Archive") {
                            Task {
                                try? await APIClient.shared.emailArchive(uid: detail.uid ?? "", folder: folder)
                                onChanged()
                            }
                        }
                        Button("Delete", role: .destructive) {
                            Task {
                                try? await APIClient.shared.emailDelete(uid: detail.uid ?? "", folder: folder)
                                onChanged()
                            }
                        }
                    } label: {
                        Image(systemName: "ellipsis")
                            .frame(width: 26, height: 26)
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()
                }
                .buttonStyle(.jbIcon)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(detail.fromName ?? detail.fromAddress ?? "")
                        .font(.jb(13, .medium))
                    if detail.fromName != nil, let a = detail.fromAddress {
                        Text(a).font(.jbMono(11)).foregroundStyle(JB.ink3).lineLimit(1)
                    }
                    Spacer()
                    Text(relativeDate(detail.date)).font(.jbMono(11)).foregroundStyle(JB.ink3)
                }
                if let to = detail.to, !to.isEmpty {
                    Text("to \(to)").font(.jbMono(11)).foregroundStyle(JB.ink3).lineLimit(1)
                }
                if let atts = detail.attachments, !atts.isEmpty {
                    HStack(spacing: 6) {
                        ForEach(atts) { a in
                            Button {
                                Task {
                                    previewURL = try? await APIClient.shared.downloadEmailAttachment(
                                        uid: detail.uid ?? "", index: a.index ?? 0,
                                        folder: folder, filename: a.filename ?? "attachment")
                                }
                            } label: {
                                Label(a.filename ?? "file", systemImage: "paperclip")
                                    .font(.jbMono(11))
                                    .lineLimit(1)
                            }
                            .buttonStyle(JBButtonStyle(kind: .secondary, compact: true))
                        }
                    }
                }
                if let aiError {
                    Text(aiError).font(.jbCaption).foregroundStyle(JB.danger).lineLimit(2)
                }
                if summarizing || summary != nil {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Summary").jbEyebrow()
                        if let summary {
                            Text(inlineMarkdown(summary))
                                .font(.jb(12.5))
                                .textSelection(.enabled)
                        } else {
                            JBSpinner()
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(JB.accentSoft, in: RoundedRectangle(cornerRadius: JB.rCard, style: .continuous))
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            .padding(16)
            .animation(JB.smooth, value: summary)
            Rectangle().fill(JB.line).frame(height: 1)
            if let html = detail.bodyHtml, !html.isEmpty {
                HTMLView(html: Self.readerCSS + html)
            } else {
                ScrollView {
                    Text(detail.body ?? "")
                        .font(.jb(14))
                        .lineSpacing(4)
                        .textSelection(.enabled)
                        .frame(maxWidth: 680, alignment: .leading)
                        .frame(maxWidth: .infinity)
                        .padding(20)
                }
            }
        }
        .sheet(item: Binding(
            get: { previewURL.map { PreviewWrap(url: $0) } },
            set: { previewURL = $0?.url }
        )) { item in
            QuickLookSheet(url: item.url)
        }
    }

    /// Reading-pane typography, prepended to the message HTML.
    private static let readerCSS = """
    <style>
      body { font-family: "Geist", -apple-system, sans-serif; font-size: 14.5px; line-height: 1.62;
             margin: 20px auto; padding: 0 20px; max-width: 700px; -webkit-font-smoothing: antialiased; }
      p, div { margin-top: 0; }
      a { color: inherit; text-decoration: underline; text-decoration-color: rgba(128,128,128,.5); text-underline-offset: 2px; }
      blockquote { margin: 12px 0; padding: 2px 0 2px 14px; border-left: 2px solid rgba(128,128,128,.35); opacity: .75; }
      img { border-radius: 6px; }
      hr { border: 0; border-top: 1px solid rgba(128,128,128,.25); margin: 18px 0; }
      pre, code { font-family: "Geist Mono", ui-monospace, monospace; font-size: 12.5px; }
      h1, h2, h3 { letter-spacing: -0.01em; line-height: 1.25; }
    </style>
    """

    private func summarize() {
        guard !summarizing else { return }
        summarizing = true
        aiError = nil
        Task {
            defer { summarizing = false }
            do { summary = try await APIClient.shared.emailSummarize(detail, folder: folder) }
            catch { aiError = error.localizedDescription }
        }
    }

    private func draftReply() {
        guard !drafting else { return }
        drafting = true
        aiError = nil
        Task {
            defer { drafting = false }
            do {
                let draft = try await APIClient.shared.emailAIReply(detail, folder: folder)
                onCompose(.reply(detail, draft: draft))
            } catch {
                aiError = error.localizedDescription
            }
        }
    }
}

private struct PreviewWrap: Identifiable {
    let url: URL
    var id: String { url.path }
}

// MARK: - Compose

enum ComposeMode: Identifiable {
    case new
    case reply(EmailDetail, draft: String? = nil)
    case forward(EmailDetail)

    var id: String {
        switch self {
        case .new: return "new"
        case .reply(let d, _): return "re-\(d.uid ?? "")"
        case .forward(let d): return "fw-\(d.uid ?? "")"
        }
    }
}

struct EmailComposeSheet: View {
    let mode: ComposeMode
    let folder: String
    @Environment(\.dismiss) private var dismiss
    @State private var to = ""
    @State private var cc = ""
    @State private var subject = ""
    @State private var bodyText = ""
    @State private var attachments: [(token: String, filename: String)] = []
    @State private var showImporter = false
    @State private var sending = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(titleText)
                    .font(.jb(14, .semibold))
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .buttonStyle(.jbGhost)
                Button {
                    send()
                } label: {
                    if sending { JBSpinner(size: 12, tint: JB.onAccent) }
                    else { Text("Send") }
                }
                .buttonStyle(.jbPrimary)
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(sending || to.isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            VStack(spacing: 6) {
                TextField("To", text: $to).jbField()
                TextField("Cc", text: $cc).jbField()
                TextField("Subject", text: $subject).jbField()
            }
            .padding(.horizontal, 14)
            .padding(.top, 10)

            if !attachments.isEmpty {
                HStack(spacing: 6) {
                    ForEach(attachments, id: \.token) { a in
                        HStack(spacing: 4) {
                            Image(systemName: "paperclip").font(.jb(10))
                            Text(a.filename).font(.jbMono(11)).lineLimit(1)
                            Button {
                                attachments.removeAll { $0.token == a.token }
                            } label: {
                                Image(systemName: "xmark").font(.jb(9))
                            }
                            .buttonStyle(.plain)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .jbSurface(.raised, radius: nil)
                    }
                    Spacer()
                }
                .padding(.horizontal, 14)
                .padding(.top, 8)
            }

            TextEditor(text: $bodyText)
                .font(.jb(13))
                .scrollContentBackground(.hidden)
                .padding(10)
                .jbSurface(.raised, radius: JB.rCard)
                .padding(14)

            if let error {
                Text(error)
                    .font(.jbCaption)
                    .foregroundStyle(JB.danger)
                    .padding(.bottom, 8)
            }

            HStack {
                Button {
                    showImporter = true
                } label: {
                    Label("Attach", systemImage: "paperclip")
                }
                .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.bottom, 12)
        }
        .frame(width: 620, height: 540)
        .onAppear(perform: prefill)
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            if case .success(let urls) = result {
                for url in urls {
                    let scoped = url.startAccessingSecurityScopedResource()
                    Task {
                        // Silently dropping this used to send the mail with the
                        // attachment missing and no sign anything went wrong.
                        do { attachments.append(try await APIClient.shared.composeUpload(fileURL: url)) }
                        catch { self.error = error.localizedDescription }
                        if scoped { url.stopAccessingSecurityScopedResource() }
                    }
                }
            }
        }
    }

    private var titleText: String {
        switch mode {
        case .new: return "New email"
        case .reply: return "Reply"
        case .forward: return "Forward"
        }
    }

    private func prefill() {
        switch mode {
        case .new:
            break
        case .reply(let d, let draft):
            to = d.fromAddress ?? ""
            subject = (d.subject ?? "").hasPrefix("Re:") ? (d.subject ?? "") : "Re: \(d.subject ?? "")"
            bodyText = (draft ?? "") + "\n\n---\n" + quoted(d)
        case .forward(let d):
            subject = (d.subject ?? "").hasPrefix("Fwd:") ? (d.subject ?? "") : "Fwd: \(d.subject ?? "")"
            bodyText = "\n\n--- Forwarded message ---\n" + quoted(d)
        }
    }

    private func quoted(_ d: EmailDetail) -> String {
        "From: \(d.fromName ?? "") <\(d.fromAddress ?? "")>\nDate: \(d.date ?? "")\nSubject: \(d.subject ?? "")\n\n\(d.body ?? "")"
    }

    private func send() {
        sending = true
        error = nil
        Task {
            defer { sending = false }
            do {
                var inReplyTo: String?
                var references: String?
                if case .reply(let d, _) = mode {
                    inReplyTo = d.messageId
                    references = [d.references, d.messageId].compactMap { $0 }.joined(separator: " ")
                }
                try await APIClient.shared.emailSend(
                    to: to, cc: cc, subject: subject, body: bodyText,
                    inReplyTo: inReplyTo, references: references,
                    attachmentTokens: attachments.map(\.token)
                )
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
