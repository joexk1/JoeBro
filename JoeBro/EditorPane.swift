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
            Divider().opacity(0.4)
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
        .joeGlassRect(18)
        .shadow(color: .black.opacity(0.16), radius: 18, y: 8)
    }

    private func isEmailDoc(_ doc: EditorDoc) -> Bool {
        doc.language.lowercased() == "email"
            || doc.title.lowercased().hasSuffix(".eml")
            || EmailDocFields.parse(doc.content) != nil
    }

    // MARK: AI-edit approval

    @State private var showDiff = false

    private func approvalBanner(doc: EditorDoc, pending: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                withAnimation(.spring(duration: 0.25)) { showDiff.toggle() }
            } label: {
                HStack(spacing: 6) {
                    Label("JoeBro wants to edit this document", systemImage: "sparkles")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(Color.accentColor)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .bold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(showDiff ? 90 : 0))
                    Text(showDiff ? "hide changes" : "view changes")
                        .font(.system(size: 10.5))
                        .foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            Text(changeSummary(old: doc.content, new: pending))
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            if showDiff {
                DiffView(old: doc.content, new: pending)
                    .frame(maxHeight: 220)
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
            HStack(spacing: 8) {
                Button {
                    store.resolvePendingAIEdit(docID: doc.id, accept: true)
                } label: {
                    Label("Apply", systemImage: "checkmark")
                        .font(.system(size: 11.5, weight: .medium))
                }
                .buttonStyle(.glassProminent)
                .tint(.green)
                .controlSize(.small)
                Button {
                    store.resolvePendingAIEdit(docID: doc.id, accept: false)
                } label: {
                    Label("Reject", systemImage: "xmark")
                        .font(.system(size: 11.5, weight: .medium))
                }
                .buttonStyle(.glass)
                .controlSize(.small)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.accentColor.opacity(0.10))
        .overlay(Rectangle().frame(height: 1).foregroundStyle(.separator.opacity(0.4)), alignment: .bottom)
    }

    private func changeSummary(old: String, new: String) -> String {
        let oldLines = old.components(separatedBy: "\n").count
        let newLines = new.components(separatedBy: "\n").count
        let delta = newLines - oldLines
        let sign = delta >= 0 ? "+" : ""
        return "\(old.count) → \(new.count) characters (\(sign)\(delta) lines). Apply to view the change, Reject to keep your version."
    }

    // MARK: Tabs

    private var tabBar: some View {
        HStack(spacing: 6) {
            HStack(spacing: 0) {
                modeButton(icon: "square.and.pencil", active: !previewMode, help: "Edit") { previewMode = false }
                modeButton(icon: "eye", active: previewMode, help: "Preview (rendered)") { previewMode = true }
            }
            .padding(2)
            .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 4) {
                    ForEach(store.openDocs) { doc in
                        tab(doc)
                    }
                }
            }
            Spacer(minLength: 4)
            if !store.editorDetached {
                Button {
                    openWindow(id: "editor")
                } label: {
                    Image(systemName: "macwindow.on.rectangle")
                        .font(.system(size: 11))
                }
                .buttonStyle(.borderless)
                .help("Open editor in its own window")
            }
            if let i = store.docIndex(store.activeDocID) {
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(store.openDocs[i].content, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc")
                        .font(.system(size: 11))
                }
                .buttonStyle(.borderless)
                .help("Copy entire document")
            }
            Button {
                if store.editorDetached {
                    dismissWindow(id: "editor")
                } else {
                    store.editorVisible = false
                }
            } label: {
                Image(systemName: store.editorDetached ? "xmark.circle" : "sidebar.trailing")
                    .font(.system(size: 12))
            }
            .buttonStyle(.borderless)
            .help(store.editorDetached ? "Close editor window" : "Hide editor")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
    }

    private func modeButton(icon: String, active: Bool, help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .medium))
                .frame(width: 26, height: 22)
                .background(active ? AnyShapeStyle(.quaternary.opacity(0.8)) : AnyShapeStyle(.clear),
                            in: RoundedRectangle(cornerRadius: 6))
        }
        .buttonStyle(.borderless)
        .help(help)
    }

    private func tab(_ doc: EditorDoc) -> some View {
        let isActive = doc.id == store.activeDocID
        return HStack(spacing: 5) {
            if doc.aiWriting {
                ProgressView().controlSize(.mini)
            }
            Text(doc.title)
                .font(.system(size: 11.5, weight: isActive ? .semibold : .regular))
                .lineLimit(1)
                .frame(maxWidth: 140)
            if doc.dirty {
                Circle().fill(Color.accentColor).frame(width: 5, height: 5)
            }
            Button {
                store.closeDoc(doc.id)
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5)
        .background(
            Capsule().fill(isActive ? AnyShapeStyle(Color.accentColor.opacity(0.20)) : AnyShapeStyle(.quaternary.opacity(0.4)))
        )
        .contentShape(Capsule())
        .hoverBounce(1.04)
        .onTapGesture { store.activeDocID = doc.id }
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
                            Text(richFontName).font(.system(size: 11)).lineLimit(1)
                            Image(systemName: "chevron.down").font(.system(size: 7, weight: .bold)).foregroundStyle(.tertiary)
                        }
                        .frame(width: 110, alignment: .leading)
                    }
                    .menuStyle(.borderlessButton)
                    .menuIndicator(.hidden)
                    .fixedSize()
                    Divider().frame(height: 14).padding(.horizontal, 4)
                    fmtButton("bold", "Bold (⌘B)") { richCommands.bold() }
                    fmtButton("italic", "Italic (⌘I)") { richCommands.italic() }
                    Divider().frame(height: 14).padding(.horizontal, 4)
                    fmtButton("list.bullet", "Bullet list") { richCommands.bullet() }
                    Divider().frame(height: 14).padding(.horizontal, 4)
                    fmtButton("minus", "Smaller") { adjustFontSize(-1) }
                    TextField("", value: $richFontSize, format: .number)
                        .textFieldStyle(.plain)
                        .multilineTextAlignment(.center)
                        .font(.system(size: 11, weight: .medium))
                        .frame(width: 30)
                        .padding(.vertical, 2)
                        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 5))
                        .onSubmit { richCommands.setSize(CGFloat(richFontSize)) }
                    fmtButton("plus", "Larger") { adjustFontSize(+1) }
                    Spacer()
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                Divider().opacity(0.3)
                RichDocEditor(url: url, commands: richCommands, editable: true,
                              onChange: {
                                  guard store.openDocs.indices.contains(i) else { return }
                                  store.openDocs[i].dirty = true
                              },
                              onSaved: {
                                  guard store.openDocs.indices.contains(i) else { return }
                                  if let j = store.docIndex(store.openDocs[i].id) { store.openDocs[j].dirty = false }
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
            Divider().opacity(0.4)
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
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundStyle(preview ? Color.black.opacity(0.45) : Color.secondary)
            if isEditing {
                TextEditor(text: binding)
                    .font(.system(size: 11))
                    .foregroundStyle(Color.white.opacity(0.9))
                    .scrollContentBackground(.hidden)
                    .frame(height: 38)
                    .padding(.horizontal, -4)
            } else {
                ScrollView {
                    if text.isEmpty {
                        Text("")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        Text(text)
                            .font(.system(size: 11))
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
                .font: NSFont.systemFont(ofSize: 8),
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

        VStack(spacing: 0) {
            if doc.aiWriting {
                ScrollViewReader { proxy in
                    ScrollView {
                        Text(doc.content)
                            .font(editorFont(docLanguage(doc)))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .id("doc-tail")
                    }
                    .onChange(of: doc.content) {
                        proxy.scrollTo("doc-tail", anchor: .bottom)
                    }
                }
                .overlay(alignment: .topTrailing) {
                    Label("AI writing…", systemImage: "sparkles")
                        .font(.system(size: 10.5, weight: .medium))
                        .padding(.horizontal, 9)
                        .padding(.vertical, 4)
                        .glassEffect(.regular.tint(Color.accentColor.opacity(0.25)), in: .capsule)
                        .padding(10)
                }
            } else if previewMode {
                renderedPreview(doc)
            } else {
                if !isMonospaced(doc) {
                    // Markdown formatting toolbar
                    HStack(spacing: 2) {
                        fmtButton("bold", "Bold (⌘B)") { commands.bold() }
                        fmtButton("italic", "Italic (⌘I)") { commands.italic() }
                        Divider().frame(height: 14).padding(.horizontal, 4)
                        fmtButton("list.bullet", "Bullet list") { commands.bullet() }
                        fmtButton("list.number", "Numbered list") { commands.numbered() }
                        Spacer()
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    Divider().opacity(0.3)
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

            Divider().opacity(0.4)
            footer(doc)
        }
    }

    private func fmtButton(_ icon: String, _ help: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .medium))
                .frame(width: 24, height: 20)
        }
        .buttonStyle(.borderless)
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
            ? .system(size: 13)
            : .system(size: 12.5, design: .monospaced)
    }

    /// Rendered view — markdown native, HTML via WebKit, PDF via PDFKit,
    /// everything else as highlighted code.
    @ViewBuilder
    private func renderedPreview(_ doc: EditorDoc) -> some View {
        switch docLanguage(doc).lowercased() {
        case "markdown", "md", "email", "text", "":
            ScrollView {
                MarkdownText(text: doc.content)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(14)
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
                        Label(s.reason, systemImage: "lightbulb")
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(Color.accentColor)
                        HStack(alignment: .top, spacing: 8) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(s.find)
                                    .font(.system(size: 10.5, design: .monospaced))
                                    .strikethrough()
                                    .foregroundStyle(.secondary)
                                    .lineLimit(3)
                                Text(s.replace)
                                    .font(.system(size: 10.5, design: .monospaced))
                                    .lineLimit(3)
                            }
                            Spacer()
                            VStack(spacing: 4) {
                                Button {
                                    store.resolveSuggestion(docID: doc.id, suggestion: s, accept: true)
                                } label: {
                                    Image(systemName: "checkmark")
                                        .font(.system(size: 10, weight: .bold))
                                }
                                .buttonStyle(.glassProminent)
                                .tint(.green)
                                .controlSize(.small)
                                Button {
                                    store.resolveSuggestion(docID: doc.id, suggestion: s, accept: false)
                                } label: {
                                    Image(systemName: "xmark")
                                        .font(.system(size: 10, weight: .bold))
                                }
                                .buttonStyle(.glass)
                                .controlSize(.small)
                            }
                        }
                    }
                    .padding(10)
                    .background(.quaternary.opacity(0.35), in: RoundedRectangle(cornerRadius: 10))
                }
            }
            .padding(10)
        }
        .frame(maxHeight: 200)
    }

    // MARK: Footer

    private func footer(_ doc: EditorDoc) -> some View {
        HStack(spacing: 10) {
            Text(docLanguage(doc))
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(.quaternary.opacity(0.5), in: Capsule())
            if !doc.suggestions.isEmpty {
                Text("\(doc.suggestions.count) suggestion\(doc.suggestions.count == 1 ? "" : "s")")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Color.accentColor)
            }
            Spacer()
            // Autosave status — no button to remember
            HStack(spacing: 4) {
                if doc.dirty {
                    ProgressView().controlSize(.mini)
                    Text("Saving…")
                } else {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(.green.opacity(0.8))
                    Text("Saved")
                }
            }
            .font(.system(size: 10.5))
            .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
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
            .font(.system(size: 12.5))
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            Divider().opacity(0.4)
            EditorTextView(text: $fields.body, monospaced: false)
            Divider().opacity(0.4)
            HStack {
                Text("email")
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(.quaternary.opacity(0.5), in: Capsule())
                if let error {
                    Text(error).font(.caption).foregroundStyle(.red).lineLimit(1)
                }
                Spacer()
                Button {
                    send()
                } label: {
                    if sending {
                        ProgressView().controlSize(.small)
                    } else if sent {
                        Label("Sent", systemImage: "checkmark")
                    } else {
                        Label("Send", systemImage: "paperplane.fill")
                    }
                }
                .buttonStyle(.glassProminent)
                .tint(sent ? .green : Color.accentColor)
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

/// Image preview — opens in the editor pane like a PDF: fit to width, scrollable,
/// pinch to zoom (double-click to reset).
struct ImagePreview: View {
    let url: URL
    @State private var scale: CGFloat = 1
    @State private var committed: CGFloat = 1

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
                .onTapGesture(count: 2) {
                    withAnimation(.spring(duration: 0.25)) { scale = 1; committed = 1 }
                }
            } else {
                ContentUnavailableView("Couldn't open image", systemImage: "photo")
                    .padding(40)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct PDFPreview: NSViewRepresentable {
    var data: Data? = nil
    var url: URL? = nil

    func makeNSView(context: Context) -> PDFView {
        let v = PDFView()
        v.autoScales = true
        // Trackpad pinch-zoom: PDFView only honours magnify gestures when a
        // scale range is set around the auto-fit scale.
        v.minScaleFactor = 0.25
        v.maxScaleFactor = 6
        v.document = makeDoc()
        return v
    }

    func updateNSView(_ v: PDFView, context: Context) {
        let doc = makeDoc()
        if v.document?.documentURL != doc?.documentURL || v.document == nil {
            v.document = doc
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
                    Text("No line-level changes (whitespace or reorder only)")
                        .font(.system(size: 11))
                        .foregroundStyle(.tertiary)
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, entry in
                    HStack(alignment: .top, spacing: 6) {
                        Text(entry.1 == 1 ? "+" : "−")
                            .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                        Text(entry.0.isEmpty ? " " : entry.0)
                            .font(.system(size: 10.5, design: .monospaced))
                            .lineLimit(2)
                    }
                    .foregroundStyle(entry.1 == 1 ? Color.green : Color.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1)
                    .background((entry.1 == 1 ? Color.green : Color.red).opacity(0.08))
                }
            }
        }
        .background(.quaternary.opacity(0.25), in: RoundedRectangle(cornerRadius: 8))
    }
}
