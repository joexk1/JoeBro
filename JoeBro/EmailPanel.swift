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
            Picker("", selection: $filter) {
                Text("All").tag("all")
                Text("Unread").tag("unread")
            }
            .pickerStyle(.segmented)
            .controlSize(.small)
            .frame(width: 140)

            Menu {
                ForEach(folders, id: \.self) { f in
                    Button(f) { folder = f }
                }
            } label: {
                Label(folder, systemImage: "folder")
                    .font(.system(size: 12))
            }
            .menuStyle(.borderlessButton)
            .fixedSize()

            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.borderless)
            .help("Refresh")

            Button {
                composeMode = .new
            } label: {
                Image(systemName: "square.and.pencil")
            }
            .buttonStyle(.borderless)
            .help("Compose")
        } content: {
            if needsEmailConnection {
                ContentUnavailableView("Connect email in Settings", systemImage: "envelope.badge",
                                       description: Text("Open Settings > General to connect an email account."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .joeGlassRect(16)
            } else {
                HStack(spacing: 12) {
                    listColumn
                        .frame(width: 320)
                        .joeGlassRect(16)
                    detailColumn
                        .frame(maxWidth: .infinity)
                        .joeGlassRect(16)
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
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                TextField("Search emails", text: $search)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                if !search.isEmpty {
                    Button { search = "" } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(
                .quaternary.opacity(0.35),
                in: UnevenRoundedRectangle(topLeadingRadius: 16, bottomLeadingRadius: 0,
                                           bottomTrailingRadius: 0, topTrailingRadius: 16)
            )
            .overlay(alignment: .bottom) {
                Divider().opacity(0.18)
            }
            if !selection.isEmpty {
                HStack(spacing: 10) {
                    Text("\(selection.count) selected")
                        .font(.system(size: 11, weight: .semibold))
                    Button(allSelected ? "None" : "All") {
                        if case .loaded(let list) = emails {
                            withAnimation(.spring(duration: 0.25)) {
                                selection = allSelected ? [] : Set(list.map(\.uid))
                            }
                        }
                    }
                    .buttonStyle(.borderless)
                    .font(.system(size: 11))
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
                        withAnimation(.spring(duration: 0.25)) { selection = [] }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.accentColor.opacity(0.10))
                .transition(.move(edge: .top).combined(with: .opacity))
                Divider().opacity(0.4)
            }
            listBody
        }
        .animation(.spring(duration: 0.25), value: selection.isEmpty)
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
                .font(.system(size: 12))
        }
        .buttonStyle(.borderless)
        .help(help)
    }

    private var listBody: some View {
        Group {
            switch emails {
            case .loading:
                ProgressView().frame(maxHeight: .infinity)
            case .failed(let e):
                ContentUnavailableView("Couldn't load", systemImage: "wifi.exclamationmark", description: Text(e))
            case .loaded(let list) where searchFiltered(list).isEmpty:
                if emailStatus?.configured == true {
                    ContentUnavailableView("No emails", systemImage: "tray")
                } else {
                    ContentUnavailableView("Connect email in Settings", systemImage: "envelope.badge",
                                           description: Text("Open Settings > General to connect an email account."))
                }
            case .loaded(let list):
                ScrollView {
                    LazyVStack(spacing: 2) {
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
                    .padding(8)
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
        withAnimation(.spring(duration: 0.2)) {
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
            ContentUnavailableView("Select an email", systemImage: "envelope.open")
        case .loading:
            ProgressView().frame(maxHeight: .infinity)
        case .failed(let e):
            ContentUnavailableView("Couldn't open", systemImage: "exclamationmark.triangle", description: Text(e))
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

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            // Checkbox owns its own tap; the rest of the row owns the open/
            // toggle tap — previously both fired and cancelled each other.
            // Slot is always reserved — inserting it on hover reflowed the
            // row under the pointer and made the circle flicker.
            let showCheck = selecting || hovering || isChecked
            Image(systemName: isChecked ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 15))
                .foregroundStyle(isChecked ? Color.accentColor : .secondary)
                .frame(width: 24, height: 24, alignment: .center)
                .padding(.top, -3)
                .contentShape(Rectangle())
                .onTapGesture(perform: onToggleCheck)
                .opacity(showCheck ? 1 : 0)
                .scaleEffect(showCheck ? 1 : 0.5)
                .allowsHitTesting(showCheck)
            rowContent
                .contentShape(Rectangle())
                .onTapGesture(perform: onTap)
        }
        .panelCard()
        .background(
            RoundedRectangle(cornerRadius: 14)
                .fill(isSelected || isChecked
                      ? Color.accentColor.opacity(0.14)
                      : hovering ? Color.white.opacity(0.045) : Color.clear)
        )
        .scaleEffect(hovering ? 1.012 : 1)
        .shadow(color: .black.opacity(0.10), radius: 6, y: 3)
        .onHover { h in
            withAnimation(.spring(duration: 0.22)) { hovering = h }
        }
    }

    private var unread: Bool { mail.isRead == false }

    private var rowContent: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(unread ? Color.accentColor : Color.clear)
                .frame(width: 7, height: 7)
                .padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(mail.fromName ?? mail.fromAddress ?? "Unknown")
                        .font(.system(size: 12.5, weight: unread ? .semibold : .regular))
                        .lineLimit(1)
                    Spacer()
                    Text(relativeDate(mail.date))
                        .font(.system(size: 10.5))
                        .foregroundStyle(.tertiary)
                }
                Text(mail.subject ?? "(no subject)")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if let s = mail.cachedSummary {
                    Text(s)
                        .font(.system(size: 11))
                        .foregroundStyle(.tertiary)
                        .lineLimit(2)
                }
            }
            if mail.isFlagged == true {
                Image(systemName: "star.fill")
                    .font(.system(size: 9))
                    .foregroundStyle(.yellow)
            }
            if mail.hasAttachments == true {
                Image(systemName: "paperclip")
                    .font(.system(size: 9))
                    .foregroundStyle(.tertiary)
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
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(detail.subject ?? "(no subject)")
                        .font(.system(size: 15, weight: .semibold))
                    Spacer()
                    // AI actions
                    Button {
                        summarize()
                    } label: {
                        if summarizing { ProgressView().controlSize(.mini) }
                        else { Image(systemName: "sparkles") }
                    }
                    .help("AI summary")
                    Button {
                        draftReply()
                    } label: {
                        if drafting { ProgressView().controlSize(.mini) }
                        else { Image(systemName: "sparkles.rectangle.stack") }
                    }
                    .help("AI reply draft")
                    Divider().frame(height: 14)
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
                        Image(systemName: "ellipsis.circle")
                    }
                    .menuStyle(.borderlessButton)
                    .fixedSize()
                }
                .buttonStyle(.borderless)
                Text("\(detail.fromName ?? "") <\(detail.fromAddress ?? "")>")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                HStack {
                    if let to = detail.to, !to.isEmpty {
                        Text("To: \(to)").font(.system(size: 11)).foregroundStyle(.tertiary).lineLimit(1)
                    }
                    Spacer()
                    Text(relativeDate(detail.date)).font(.system(size: 11)).foregroundStyle(.tertiary)
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
                                    .font(.system(size: 11))
                                    .lineLimit(1)
                            }
                            .buttonStyle(.borderless)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .joeGlassCapsule()
                        }
                    }
                    .padding(.top, 4)
                }
                if let aiError {
                    Text(aiError).font(.caption).foregroundStyle(.red).lineLimit(2)
                }
                if summarizing || summary != nil {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("AI Summary", systemImage: "sparkles")
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(Color.accentColor)
                        if let summary {
                            Text(inlineMarkdown(summary))
                                .font(.system(size: 12))
                                .textSelection(.enabled)
                        } else {
                            ProgressView().controlSize(.small)
                        }
                    }
                    .padding(10)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                    .padding(.top, 6)
                    .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            .padding(14)
            .animation(.spring(duration: 0.3), value: summary)
            Divider().opacity(0.4)
            if let html = detail.bodyHtml, !html.isEmpty {
                HTMLView(html: html)
            } else {
                ScrollView {
                    Text(detail.body ?? "")
                        .font(.system(size: 13))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(14)
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
                    .font(.system(size: 14, weight: .semibold))
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button {
                    send()
                } label: {
                    if sending { ProgressView().controlSize(.small) }
                    else { Label("Send", systemImage: "paperplane.fill") }
                }
                .buttonStyle(.glassProminent)
                .tint(Color.accentColor)
                .disabled(sending || to.isEmpty)
            }
            .padding(14)
            Divider()
            Form {
                TextField("To", text: $to)
                TextField("Cc", text: $cc)
                TextField("Subject", text: $subject)
            }
            .textFieldStyle(.roundedBorder)
            .padding(.horizontal, 14)
            .padding(.top, 10)

            if !attachments.isEmpty {
                HStack(spacing: 6) {
                    ForEach(attachments, id: \.token) { a in
                        HStack(spacing: 4) {
                            Image(systemName: "paperclip").font(.system(size: 10))
                            Text(a.filename).font(.system(size: 11)).lineLimit(1)
                            Button {
                                attachments.removeAll { $0.token == a.token }
                            } label: {
                                Image(systemName: "xmark.circle.fill").font(.system(size: 10))
                            }
                            .buttonStyle(.borderless)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 4)
                        .background(.quaternary.opacity(0.5), in: Capsule())
                    }
                    Spacer()
                }
                .padding(.horizontal, 14)
                .padding(.top, 8)
            }

            TextEditor(text: $bodyText)
                .font(.system(size: 13))
                .scrollContentBackground(.hidden)
                .padding(10)
                .background(.quaternary.opacity(0.3), in: RoundedRectangle(cornerRadius: 10))
                .padding(14)

            if let error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.bottom, 8)
            }

            HStack {
                Button {
                    showImporter = true
                } label: {
                    Label("Attach", systemImage: "paperclip")
                }
                .buttonStyle(.borderless)
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
        case .new: return "New Email"
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
