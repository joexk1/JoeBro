import SwiftUI
import PDFKit
import AppKit

/// Side-by-side document editor — the co-editing surface shared with the AI.
/// AI writes stream in live; AI edits to your open doc require approval;
/// your edits autosave. Email-shaped docs become a composer with Send.
struct EditorPane: View {
    @Environment(AppStore.self) private var store
    @Environment(\.openWindow) private var openWindow
    @Environment(\.dismissWindow) private var dismissWindow
    @State private var previewMode = false
    @State private var commands = EditorCommands()
    @State private var richCommands = RichEditorCommands()
    @State private var richFontSize: Double = 12
    @State private var richFontName: String = "Calibri"
    @State private var richExtraSaveTask: Task<Void, Never>?
    @State private var outline = DocOutlineModel()
    @AppStorage("editorOutlineVisible") private var outlineVisible = true
    @State private var previewProxy: ScrollViewProxy?

    /// The ~20 families that cover the overwhelming majority of Word / Google
    /// Docs documents. macOS substitutes a close match for any not installed.
    private static let docFonts = [
        "Arial", "Arial Black", "Calibri", "Cambria", "Comic Sans MS",
        "Courier New", "Garamond", "Georgia", "Helvetica", "Helvetica Neue",
        "Lato", "Montserrat", "Open Sans", "Palatino", "Roboto",
        "Tahoma", "Times New Roman", "Trebuchet MS", "Verdana", "Gill Sans",
    ]

    var body: some View {
        VStack(spacing: 0) {
            tabBar
            Rectangle().fill(JB.line).frame(height: 1)
            if let i = store.docIndex(store.activeDocID) {
                if store.openDocs[i].flashcards {
                    FlashcardsEditor(index: i, study: previewMode)
                } else if store.openDocs[i].spreadsheet {
                    SpreadsheetEditor(index: i)
                } else if store.openDocs[i].richDoc, let url = store.openDocs[i].localFileURL {
                    richDocEditor(index: i, url: url)
                } else if let fileURL = store.openDocs[i].localFileURL {
                    if store.openDocs[i].language == "image" {
                        ImagePreview(url: fileURL)
                    } else {
                        PDFPreview(url: fileURL)
                    }
                } else {
                    if let pending = store.openDocs[i].pendingAIContent {
                        approvalBanner(doc: store.openDocs[i], pending: pending)
                    }
                    if isEmailDoc(store.openDocs[i]) {
                        EmailDocEditor(index: i)
                    } else {
                        editor(index: i)
                    }
                }
            } else {
                ContentUnavailableView("No document open", systemImage: "doc.text")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background {
            Button("") { withAnimation(JB.snap) { outlineVisible.toggle() } }
                .keyboardShortcut("o", modifiers: [.command, .option])
                .opacity(0).allowsHitTesting(false)
        }
        .joeGlassRect(18)
    }

    private func isMarkdownDoc(_ doc: EditorDoc) -> Bool {
        ["markdown", "md", ""].contains(docLanguage(doc).lowercased()) && !isEmailDoc(doc)
    }

    private func selectHeading(_ h: MDHeading) {
        outline.active = h.id
        if previewMode {
            withAnimation(JB.smooth) { previewProxy?.scrollTo(markdownHeadingID(h.id), anchor: .top) }
        } else {
            commands.scroll(toOffset: h.offset)
        }
    }

    private func isEmailDoc(_ doc: EditorDoc) -> Bool {
        doc.language.lowercased() == "email"
            || doc.title.lowercased().hasSuffix(".eml")
            || EmailDocFields.parse(doc.content) != nil
    }

    // MARK: AI-edit approval

    @State private var showDiff = false

    private func approvalBanner(doc: EditorDoc, pending: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button {
                    withAnimation(JB.smooth) { showDiff.toggle() }
                } label: {
                    HStack(spacing: 6) {
                        JBIcon(.spark, size: 13).foregroundStyle(JB.accent)
                        Text("JoeBro edited this").font(.jb(12, .medium)).foregroundStyle(JB.ink)
                        Text(changeSummary(old: doc.content, new: pending))
                            .font(.jbMono(11)).foregroundStyle(JB.ink3)
                        Image(systemName: "chevron.right")
                            .font(.jb(8, .semibold))
                            .foregroundStyle(JB.ink3)
                            .rotationEffect(.degrees(showDiff ? 90 : 0))
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(showDiff ? "Hide changes" : "View changes")
                Spacer()
                Button("Reject") {
                    store.resolvePendingAIEdit(docID: doc.id, accept: false)
                }
                .buttonStyle(.jbSecondary)
                Button("Apply") {
                    store.resolvePendingAIEdit(docID: doc.id, accept: true)
                }
                .buttonStyle(.jbPrimary)
            }
            .controlSize(.small)
            if showDiff {
                DiffView(old: doc.content, new: pending)
                    .frame(maxHeight: 220)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(JB.accentSoft)
        .overlay(alignment: .leading) { Rectangle().fill(JB.accent).frame(width: 2) }
        .overlay(alignment: .bottom) { Rectangle().fill(JB.line).frame(height: 1) }
    }

    private func changeSummary(old: String, new: String) -> String {
        let delta = new.utf8.reduce(1) { $1 == 10 ? $0 + 1 : $0 } - old.utf8.reduce(1) { $1 == 10 ? $0 + 1 : $0 }
        return "\(delta >= 0 ? "+" : "−")\(abs(delta)) lines"
    }

    // MARK: Tabs

    private var tabBar: some View {
        HStack(spacing: 2) {
            HStack(spacing: 0) {
                modeButton(icon: "square.and.pencil", active: !previewMode, help: "Edit") { previewMode = false }
                modeButton(icon: "eye", active: previewMode, help: "Preview") { previewMode = true }
            }
            .padding(.leading, 8)
            .padding(.trailing, 6)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 0) {
                    ForEach(store.openDocs) { doc in
                        EditorTab(doc: doc, isActive: doc.id == store.activeDocID,
                                  select: { store.activeDocID = doc.id },
                                  close: { store.closeDoc(doc.id) })
                    }
                }
            }
            Spacer(minLength: 4)
            if let i = store.docIndex(store.activeDocID), isMarkdownDoc(store.openDocs[i]) {
                Button {
                    withAnimation(JB.snap) { outlineVisible.toggle() }
                } label: { Image(systemName: "list.bullet.indent") }
                .buttonStyle(JBIconButtonStyle(size: 26, active: outlineVisible))
                .help("Outline (⌥⌘O)")
            }
            if !store.editorDetached {
                Button { openWindow(id: "editor") } label: { Image(systemName: "macwindow.on.rectangle") }
                    .buttonStyle(JBIconButtonStyle(size: 26))
                    .help("Open in window")
            }
            if let i = store.docIndex(store.activeDocID) {
                CopyIconButton(text: store.openDocs[i].content)
            }
            Button {
                if store.editorDetached { dismissWindow(id: "editor") } else { store.editorVisible = false }
            } label: { Image(systemName: store.editorDetached ? "xmark" : "sidebar.trailing") }
            .buttonStyle(JBIconButtonStyle(size: 26))
            .help(store.editorDetached ? "Close window" : "Hide editor")
            .padding(.trailing, 6)
        }
        .frame(height: 36)
    }

    private func modeButton(icon: String, active: Bool, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: icon) }
            .buttonStyle(JBIconButtonStyle(size: 26, active: active))
            .help(help)
    }

    // MARK: Rich Word-document editor (.doc / .docx)

    @ViewBuilder
    private func richDocEditor(index i: Int, url: URL) -> some View {
        @Bindable var store = store
        let doc = store.openDocs[i]
        VStack(spacing: 0) {
            if !previewMode || !(doc.docHeader ?? "").isEmpty {
                docBand(index: i, region: "header", label: "Header",
                        icon: "textformat.abc.dottedunderline", preview: previewMode)
            }
            if previewMode {
                // Eye = real page: full fidelity (colour, sizes, highlights) on a
                // white background, like the actual .docx.
                RichDocEditor(url: url, commands: richCommands, editable: false)
                    .id("preview-\(url.path)")
            } else {
                HStack(spacing: 2) {
                    Menu {
                        ForEach(Self.docFonts, id: \.self) { name in
                            Button {
                                richFontName = name
                                richCommands.setFont(name)
                            } label: {
                                if name == richFontName { Label(name, systemImage: "checkmark") }
                                else { Text(name) }
                            }
                        }
                    } label: {
                        HStack(spacing: 3) {
                            Text(richFontName).font(.jb(11)).lineLimit(1)
                            Image(systemName: "chevron.down").font(.jb(7, .bold)).foregroundStyle(JB.ink3)
                        }
                        .frame(width: 110, alignment: .leading)
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()
                    Rectangle().fill(JB.line).frame(width: 1, height: 14).padding(.horizontal, 4)
                    fmtButton("bold", "Bold (⌘B)") { richCommands.bold() }
                    fmtButton("italic", "Italic (⌘I)") { richCommands.italic() }
                    Rectangle().fill(JB.line).frame(width: 1, height: 14).padding(.horizontal, 4)
                    fmtButton("list.bullet", "Bullet list") { richCommands.bullet() }
                    Rectangle().fill(JB.line).frame(width: 1, height: 14).padding(.horizontal, 4)
                    fmtButton("minus", "Smaller") { adjustFontSize(-1) }
                    TextField("", value: $richFontSize, format: .number)
                        .textFieldStyle(.plain)
                        .multilineTextAlignment(.center)
                        .font(.jb(11, .medium))
                        .frame(width: 30)
                        .padding(.vertical, 2)
                        .background(JB.raised, in: RoundedRectangle(cornerRadius: 5))
                        .onSubmit { richCommands.setSize(CGFloat(richFontSize)) }
                    fmtButton("plus", "Larger") { adjustFontSize(+1) }
                    Spacer()
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                Rectangle().fill(JB.line).frame(height: 1)
                RichDocEditor(url: url, commands: richCommands, editable: true,
                              onChange: {
                                  guard store.openDocs.indices.contains(i) else { return }
                                  store.openDocs[i].dirty = true
                              },
                              onSaved: {
                                  guard store.openDocs.indices.contains(i) else { return }
                                  let id = store.openDocs[i].id
                                  if let j = store.docIndex(id) { store.openDocs[j].dirty = false }
                                  // Remote docs save to a local temp: push the bytes back.
                                  store.pushRemoteDocxBody(docID: id)
                              },
                              onSelectionSize: { richFontSize = Double($0.rounded()) },
                              onSelectionFont: { richFontName = $0 },
                              onAddFootnote: { textView in addFootnote(index: i, textView: textView) })
                    .id("edit-\(url.path)-\(doc.reloadNonce)")
            }
            if !previewMode || !(doc.docFooter ?? "").isEmpty || !(doc.docFootnotes ?? "").isEmpty {
                docBand(index: i, region: "footer", label: "Footer & footnotes",
                        icon: "text.append", preview: previewMode)
            }
            Rectangle().fill(JB.line).frame(height: 1)
            footer(doc)
        }
    }

    /// Header / footnotes band shown around the page. Styled to sit on the white
    /// page in view mode, and on the dark editor in edit mode.
    private func docBand(index i: Int, region: String, label: String, icon: String, preview: Bool) -> some View {
        @Bindable var store = store
        let isEditing = !preview
        let storedText = region == "header"
            ? (store.openDocs[i].docHeader ?? "")
            : (store.openDocs[i].docFooter ?? "")
        let footnotes = region == "footer" ? (store.openDocs[i].docFootnotes ?? "") : ""
        let text = isEditing ? storedText : [storedText, footnotes].filter { !$0.isEmpty }.joined(separator: "\n\n")
        let binding = Binding<String>(
            get: {
                guard store.openDocs.indices.contains(i) else { return "" }
                return region == "header"
                    ? (store.openDocs[i].docHeader ?? "")
                    : (store.openDocs[i].docFooter ?? "")
            },
            set: { value in
                guard store.openDocs.indices.contains(i) else { return }
                if region == "header" {
                    store.openDocs[i].docHeader = value
                } else {
                    store.openDocs[i].docFooter = value
                }
                store.openDocs[i].dirty = true
                scheduleRichExtraSave(docID: store.openDocs[i].id, region: region, content: value)
            }
        )
        return VStack(alignment: .leading, spacing: 3) {
            Label(label, systemImage: icon)
                .font(.jb(9.5, .semibold))
                .foregroundStyle(preview ? Color.black.opacity(0.45) : JB.ink2)
            if isEditing {
                TextEditor(text: binding)
                    .font(.jb(11))
                    .foregroundStyle(Color.white.opacity(0.9))
                    .scrollContentBackground(.hidden)
                    .frame(height: 38)
                    .padding(.horizontal, -4)
            } else {
                ScrollView {
                    if text.isEmpty {
                        Text("")
                            .font(.jb(11))
                            .foregroundStyle(JB.ink3)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        Text(text)
                            .font(.jb(11))
                            .foregroundStyle(preview ? Color.black.opacity(0.78) : Color.white.opacity(0.85))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                    }
                }
                .frame(maxHeight: 46)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 5)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(preview ? Color.white : Color.clear)
        .overlay(Rectangle().frame(height: 1).foregroundStyle(.separator.opacity(0.3)),
                 alignment: preview ? .bottom : .top)
    }

    private func scheduleRichExtraSave(docID: String, region: String, content: String) {
        richExtraSaveTask?.cancel()
        richExtraSaveTask = Task {
            try? await Task.sleep(for: .milliseconds(900))
            guard !Task.isCancelled else { return }
            await MainActor.run {
                store.saveDocxExtra(docID: docID, region: region, content: content)
            }
        }
    }

    private func addFootnote(index i: Int, textView: NSTextView) {
        guard store.openDocs.indices.contains(i) else { return }
        let current = store.openDocs[i].docFootnotes ?? ""
        let next = nextFootnoteNumber(in: current)

        let alert = NSAlert()
        alert.messageText = "Add Footnote"
        alert.informativeText = "Footnote \(next)"
        alert.addButton(withTitle: "Add")
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        input.placeholderString = "Footnote text"
        alert.accessoryView = input
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let note = input.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !note.isEmpty else { return }

        let marker = NSAttributedString(
            string: "\(next)",
            attributes: [
                .font: JBFonts.nsFont(8),
                .baselineOffset: 5,
                .foregroundColor: textView.textColor ?? NSColor.labelColor,
            ]
        )
        let range = textView.selectedRange()
        if textView.shouldChangeText(in: range, replacementString: marker.string) {
            textView.textStorage?.replaceCharacters(in: range, with: marker)
            textView.setSelectedRange(NSRange(location: range.location + marker.length, length: 0))
            textView.didChangeText()
        }

        let updated = ([current.trimmingCharacters(in: .whitespacesAndNewlines), "\(next). \(note)"]
            .filter { !$0.isEmpty }).joined(separator: "\n")
        store.openDocs[i].docFootnotes = updated
        store.openDocs[i].dirty = true
        let docID = store.openDocs[i].id
        store.saveDocxExtra(docID: docID, region: "footnotes", content: updated)
        Task {
            try? await Task.sleep(for: .milliseconds(1700))
            await MainActor.run {
                store.saveDocxExtra(docID: docID, region: "footnotes", content: updated)
            }
        }
    }

    private func nextFootnoteNumber(in text: String) -> Int {
        let lines = text.components(separatedBy: .newlines)
        let nums = lines.compactMap { line -> Int? in
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard let dot = trimmed.firstIndex(of: ".") else { return nil }
            return Int(trimmed[..<dot])
        }
        return (nums.max() ?? 0) + 1
    }

    private func adjustFontSize(_ delta: Double) {
        richFontSize = max(6, richFontSize + delta)
        richCommands.setSize(CGFloat(richFontSize))
    }

    // MARK: Editor

    @ViewBuilder
    private func editor(index i: Int) -> some View {
        @Bindable var store = store
        let doc = store.openDocs[i]
        let md = isMarkdownDoc(doc)

        VStack(spacing: 0) {
            HStack(spacing: 0) {
                VStack(spacing: 0) {
                    if doc.aiWriting {
                        ScrollViewReader { proxy in
                            ScrollView {
                                Text(doc.content)
                                    .font(editorFont(docLanguage(doc)))
                                    .foregroundStyle(JB.ink)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                                    .padding(12)
                                    .id("doc-tail")
                            }
                            .onChange(of: doc.content) {
                                proxy.scrollTo("doc-tail", anchor: .bottom)
                            }
                        }
                        .overlay(alignment: .topTrailing) {
                            HStack(spacing: 6) {
                                JBSpinner(size: 10, tint: JB.accent)
                                Text("writing").font(.jbMono(10.5)).foregroundStyle(JB.ink2)
                            }
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .jbSurface(.raised, radius: JB.rControl)
                            .padding(10)
                        }
                    } else if previewMode {
                        renderedPreview(doc)
                    } else {
                        if !isMonospaced(doc) {
                            HStack(spacing: 0) {
                                fmtButton("bold", "Bold (⌘B)") { commands.bold() }
                                fmtButton("italic", "Italic (⌘I)") { commands.italic() }
                                Rectangle().fill(JB.line).frame(width: 1, height: 14).padding(.horizontal, 4)
                                fmtButton("list.bullet", "Bullets") { commands.bullet() }
                                fmtButton("list.number", "Numbers") { commands.numbered() }
                                Spacer()
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            Rectangle().fill(JB.line).frame(height: 1)
                        }
                        EditorTextView(text: $store.openDocs[i].content,
                                       monospaced: isMonospaced(doc),
                                       commands: commands)
                            .onChange(of: store.openDocs[i].content) {
                                guard store.openDocs.indices.contains(i) else { return }
                                store.openDocs[i].dirty = true
                                store.scheduleAutosave(doc.id)
                            }
                    }

                    if !doc.suggestions.isEmpty {
                        suggestionsView(doc)
                    }
                }
                if md && outlineVisible && !outline.headings.isEmpty {
                    DocOutlineView(model: outline, onSelect: selectHeading)
                        .transition(.move(edge: .trailing).combined(with: .opacity))
                }
            }
            .animation(JB.smooth, value: outlineVisible)

            Rectangle().fill(JB.line).frame(height: 1)
            footer(doc)
        }
        .onAppear { commands.onTopOffset = { [outline] in outline.track(offset: $0) } }
        .task(id: doc.id) { outline.refresh(text: doc.content, markdown: md, immediately: true) }
        .onChange(of: doc.content) { outline.refresh(text: doc.content, markdown: md) }
    }

    private func fmtButton(_ icon: String, _ help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: icon) }
            .buttonStyle(JBIconButtonStyle(size: 24))
            .help(help)
    }

    private func isMonospaced(_ doc: EditorDoc) -> Bool {
        !["markdown", "md", "email", "text", ""].contains(docLanguage(doc).lowercased())
    }

    private func docLanguage(_ doc: EditorDoc) -> String {
        // Filename extension beats a server guess of "markdown"/empty
        if let fromTitle = AppStore.languageFromTitle(doc.title),
           doc.language.isEmpty || doc.language == "markdown" || fromTitle == "svg" {
            return fromTitle
        }
        return doc.language
    }

    private func editorFont(_ language: String) -> Font {
        ["markdown", "md", "email", "text", ""].contains(language.lowercased())
            ? .jb(14)
            : .jbMono(12.5)
    }

    /// Rendered view — markdown native, HTML via WebKit, PDF via PDFKit,
    /// everything else as highlighted code.
    @ViewBuilder
    private func renderedPreview(_ doc: EditorDoc) -> some View {
        switch docLanguage(doc).lowercased() {
        case "markdown", "md", "email", "text", "":
            ScrollViewReader { proxy in
                ScrollView {
                    MarkdownText(text: doc.content, document: true)
                        .frame(maxWidth: 720, alignment: .leading)
                        .frame(maxWidth: .infinity)
                        .padding(.horizontal, 24)
                        .padding(.vertical, 20)
                }
                .onAppear { previewProxy = proxy }
            }
        case "html":
            HTMLView(html: doc.content)
        case "svg":
            // Checkerboard-free rendered SVG, centred, via WebKit
            HTMLView(html: """
                <div style="display:flex;align-items:center;justify-content:center;height:95vh">
                \(doc.content)
                </div>
                """)
        case "pdf":
            PDFPreview(data: Data(doc.content.utf8))
        default:
            ScrollView([.vertical, .horizontal]) {
                CodeBlockView(language: doc.language, code: doc.content)
                    .padding(10)
            }
        }
    }

    // MARK: AI suggestions

    private func suggestionsView(_ doc: EditorDoc) -> some View {
        ScrollView {
            VStack(spacing: 8) {
                ForEach(doc.suggestions) { s in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(s.reason)
                            .font(.jb(11.5, .medium))
                            .foregroundStyle(JB.ink)
                        HStack(alignment: .top, spacing: 8) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(s.find)
                                    .font(.jbMono(10.5))
                                    .strikethrough()
                                    .foregroundStyle(JB.danger)
                                    .lineLimit(3)
                                Text(s.replace)
                                    .font(.jbMono(10.5))
                                    .foregroundStyle(JB.live)
                                    .lineLimit(3)
                            }
                            Spacer()
                            VStack(spacing: 4) {
                                Button {
                                    store.resolveSuggestion(docID: doc.id, suggestion: s, accept: true)
                                } label: {
                                    Image(systemName: "checkmark")
                                        .font(.jb(10, .semibold))
                                }
                                .buttonStyle(.jbPrimary)
                                .controlSize(.small)
                                Button {
                                    store.resolveSuggestion(docID: doc.id, suggestion: s, accept: false)
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.jb(10, .bold))
                                }
                                .buttonStyle(.jbSecondary)
                                .controlSize(.small)
                            }
                        }
                    }
                    .padding(10)
                    .background(JB.raised, in: RoundedRectangle(cornerRadius: JB.rCard, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                }
            }
            .padding(10)
        }
        .frame(maxHeight: 200)
    }

    // MARK: Footer

    private func footer(_ doc: EditorDoc) -> some View {
        HStack(spacing: 8) {
            Text(docLanguage(doc).isEmpty ? "text" : docLanguage(doc))
            if !outline.stats.isEmpty {
                Text("·")
                Text(outline.stats)
            }
            if !doc.suggestions.isEmpty {
                Text("·")
                Text("\(doc.suggestions.count) suggested").foregroundStyle(JB.accent)
            }
            Spacer()
            if doc.dirty {
                JBSpinner(size: 10)
            } else {
                Image(systemName: "checkmark").font(.jb(9, .medium)).foregroundStyle(JB.ink3)
            }
        }
        .font(.jbMono(10.5))
        .foregroundStyle(JB.ink3)
        .padding(.horizontal, 12)
        .frame(height: 26)
    }
}

// MARK: - Email document editor (To/Subject/---/body + Send)

struct EmailDocEditor: View {
    @Environment(AppStore.self) private var store
    let index: Int
    @State private var fields = EmailDocFields()
    @State private var sending = false
    @State private var sent = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            Form {
                TextField("To", text: $fields.to)
                TextField("Cc", text: $fields.cc)
                TextField("Subject", text: $fields.subject)
            }
            .textFieldStyle(.plain)
            .formStyle(.columns)
            .font(.jb(12.5))
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            Rectangle().fill(JB.line).frame(height: 1)
            EditorTextView(text: $fields.body, monospaced: false)
            Rectangle().fill(JB.line).frame(height: 1)
            HStack {
                Text("email").font(.jbMono(10.5)).foregroundStyle(JB.ink3)
                if let error {
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger).lineLimit(1)
                }
                Spacer()
                Button {
                    send()
                } label: {
                    if sending {
                        JBSpinner(size: 11, tint: JB.onAccent)
                    } else if sent {
                        Label("Sent", systemImage: "checkmark")
                    } else {
                        Label("Send", systemImage: "paperplane")
                    }
                }
                .buttonStyle(.jbPrimary)
                .controlSize(.small)
                .disabled(sending || sent || fields.to.isEmpty)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .onAppear(perform: load)
        .onChange(of: fields.to) { sync() }
        .onChange(of: fields.cc) { sync() }
        .onChange(of: fields.subject) { sync() }
        .onChange(of: fields.body) { sync() }
        .onChange(of: store.activeDocID) { load() }
    }

    private func load() {
        guard store.openDocs.indices.contains(index) else { return }
        fields = EmailDocFields.parse(store.openDocs[index].content) ?? {
            var f = EmailDocFields()
            f.body = store.openDocs[index].content
            return f
        }()
        sent = false
    }

    private func sync() {
        guard store.openDocs.indices.contains(index) else { return }
        let serialized = fields.serialize()
        if store.openDocs[index].content != serialized {
            store.openDocs[index].content = serialized
            store.openDocs[index].dirty = true
            store.scheduleAutosave(store.openDocs[index].id)
        }
    }

    private func send() {
        sending = true
        error = nil
        Task {
            defer { sending = false }
            do {
                try await APIClient.shared.emailSend(
                    to: fields.to, cc: fields.cc, subject: fields.subject,
                    body: fields.body, inReplyTo: fields.inReplyTo.isEmpty ? nil : fields.inReplyTo,
                    references: fields.references.isEmpty ? nil : fields.references,
                    attachmentTokens: [])
                sent = true
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

// MARK: - PDF preview

/// Image preview: fit to width, pinch or buttons to zoom, double-click to reset.
struct ImagePreview: View {
    let url: URL
    @State private var scale: CGFloat = 1
    @State private var committed: CGFloat = 1

    private func set(_ v: CGFloat) {
        withAnimation(JB.snap) { scale = max(0.25, min(v, 8)); committed = scale }
    }

    var body: some View {
        Group {
            if let img = NSImage(contentsOf: url) {
                ScrollView([.vertical, .horizontal]) {
                    Image(nsImage: img)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .frame(maxWidth: .infinity)
                        .scaleEffect(scale, anchor: .top)
                        .padding(12)
                }
                .gesture(
                    MagnifyGesture()
                        .onChanged { scale = max(0.25, min(committed * $0.magnification, 8)) }
                        .onEnded { _ in committed = scale }
                )
                .onTapGesture(count: 2) { set(1) }
                .overlay(alignment: .bottomTrailing) {
                    ViewerControls(label: "\(Int((scale * 100).rounded()))%",
                                   minus: { set(scale / 1.25) }, plus: { set(scale * 1.25) }, fit: { set(1) })
                        .padding(10)
                }
            } else {
                ContentUnavailableView("Couldn't open image", systemImage: "photo")
                    .padding(40)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Quiet floating zoom cluster shared by the image and PDF viewers.
struct ViewerControls: View {
    let label: String
    let minus: () -> Void
    let plus: () -> Void
    let fit: () -> Void

    var body: some View {
        HStack(spacing: 0) {
            Button(action: minus) { Image(systemName: "minus") }
                .buttonStyle(JBIconButtonStyle(size: 24)).help("Zoom out")
            Text(label).font(.jbMono(10.5)).foregroundStyle(JB.ink2)
                .frame(minWidth: 52).onTapGesture(perform: fit).help("Fit")
            Button(action: plus) { Image(systemName: "plus") }
                .buttonStyle(JBIconButtonStyle(size: 24)).help("Zoom in")
        }
        .padding(2)
        .jbSurface(.raised, radius: JB.rControl)
    }
}

/// PDF viewer: PDFKit with a mono page indicator and zoom cluster.
struct PDFPreview: View {
    var data: Data? = nil
    var url: URL? = nil
    @State private var model = PDFModel()

    var body: some View {
        PDFKitView(data: data, url: url, model: model)
            .overlay(alignment: .bottomTrailing) {
                HStack(spacing: 8) {
                    if model.pages > 0 {
                        Text("\(model.page) / \(model.pages)")
                            .font(.jbMono(10.5)).foregroundStyle(JB.ink2)
                            .padding(.horizontal, 9).frame(height: 28)
                            .jbSurface(.raised, radius: JB.rControl)
                    }
                    ViewerControls(label: "\(Int((model.zoom * 100).rounded()))%",
                                   minus: { model.view?.zoomOut(nil) }, plus: { model.view?.zoomIn(nil) },
                                   fit: { model.view?.autoScales = true })
                }
                .padding(10)
            }
    }
}

@Observable
final class PDFModel {
    var page = 1
    var pages = 0
    var zoom: CGFloat = 1
    @ObservationIgnored weak var view: PDFView?
    @ObservationIgnored private var tokens: [NSObjectProtocol] = []

    func attach(_ v: PDFView) {
        view = v
        tokens.forEach(NotificationCenter.default.removeObserver)
        let nc = NotificationCenter.default
        tokens = [
            nc.addObserver(forName: .PDFViewPageChanged, object: v, queue: .main) { [weak self] _ in self?.sync() },
            nc.addObserver(forName: .PDFViewScaleChanged, object: v, queue: .main) { [weak self] _ in self?.sync() },
        ]
        sync()
    }

    func sync() {
        guard let v = view, let doc = v.document else { return }
        pages = doc.pageCount
        page = v.currentPage.map { doc.index(for: $0) + 1 } ?? 1
        zoom = v.scaleFactor / max(v.scaleFactorForSizeToFit, 0.01)
    }
    deinit { tokens.forEach(NotificationCenter.default.removeObserver) }
}

struct PDFKitView: NSViewRepresentable {
    var data: Data?
    var url: URL?
    let model: PDFModel

    func makeNSView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        // Trackpad pinch-zoom: PDFView only honours magnify gestures when a
        // scale range is set around the auto-fit scale.
        v.minScaleFactor = 0.25
        v.maxScaleFactor = 6
        v.document = makeDoc()
        model.attach(v)
        return v
    }

    func updateNSView(_ v: PDFView, context: Context) {
        let doc = makeDoc()
        if v.document?.documentURL != doc?.documentURL || v.document == nil {
            v.document = doc
            model.attach(v)
        }
    }

    private func makeDoc() -> PDFDocument? {
        if let url { return PDFDocument(url: url) }
        if let data { return PDFDocument(data: data) }
        return nil
    }
}


/// Minimal line diff — green additions, red removals.
struct DiffView: View {
    let old: String
    let new: String

    private var lines: [(String, Int)] {   // (text, kind: 0 same trimmed out, 1 added, -1 removed)
        let oldLines = old.components(separatedBy: "\n")
        let newLines = new.components(separatedBy: "\n")
        let diff = newLines.difference(from: oldLines)
        var out: [(String, Int)] = []
        for change in diff.removals.prefix(80) {
            if case .remove(_, let line, _) = change { out.append((line, -1)) }
        }
        for change in diff.insertions.prefix(80) {
            if case .insert(_, let line, _) = change { out.append((line, 1)) }
        }
        return out
    }

    var body: some View {
        let rows = lines   // full difference(from:) — compute it once per body
        return ScrollView {
            VStack(alignment: .leading, spacing: 1) {
                if rows.isEmpty {
                    Text("whitespace only")
                        .font(.jb(11))
                        .foregroundStyle(JB.ink3)
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, entry in
                    HStack(alignment: .top, spacing: 6) {
                        Text(entry.1 == 1 ? "+" : "−")
                            .font(.jbMono(10.5, .bold))
                        Text(entry.0.isEmpty ? " " : entry.0)
                            .font(.jbMono(10.5))
                            .lineLimit(2)
                    }
                    .foregroundStyle(entry.1 == 1 ? JB.live : JB.danger)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background((entry.1 == 1 ? JB.live : JB.danger).opacity(0.10))
                }
            }
        }
        .background(JB.raised, in: RoundedRectangle(cornerRadius: JB.rControl, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
    }
}
