import SwiftUI

// MARK: - Block model

private enum MDBlock {
    case heading(Int, String)
    case paragraph(String)
    case code(lang: String, body: String)
    case listItem(indent: Int, marker: String, text: String)
    case quote(String)
    case rule
    case image(String)
    case table(header: [String], rows: [[String]])
}

// MARK: - Parser

private func parseBlocks(_ source: String) -> [MDBlock] {
    var blocks: [MDBlock] = []
    var lines = source.components(separatedBy: "\n")[...]
    var paragraph: [String] = []

    func flushParagraph() {
        if !paragraph.isEmpty {
            blocks.append(.paragraph(paragraph.joined(separator: "\n")))
            paragraph = []
        }
    }

    while let line = lines.first {
        lines = lines.dropFirst()
        let trimmed = line.trimmingCharacters(in: .whitespaces)

        // Fenced code — an unterminated fence (mid-stream) runs to the end.
        if trimmed.hasPrefix("```") {
            flushParagraph()
            let lang = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
            var body: [String] = []
            while let l = lines.first {
                lines = lines.dropFirst()
                if l.trimmingCharacters(in: .whitespaces).hasPrefix("```") { break }
                body.append(l)
            }
            blocks.append(.code(lang: lang, body: body.joined(separator: "\n")))
            continue
        }

        if trimmed.isEmpty { flushParagraph(); continue }

        if trimmed == "---" || trimmed == "***" || trimmed == "___" {
            flushParagraph(); blocks.append(.rule); continue
        }

        // Standalone image: ![alt](url)
        if trimmed.hasPrefix("!["), let open = trimmed.firstIndex(of: "("),
           trimmed.hasSuffix(")") {
            let url = String(trimmed[trimmed.index(after: open)..<trimmed.index(before: trimmed.endIndex)])
            if url.hasPrefix("http") {
                flushParagraph(); blocks.append(.image(url)); continue
            }
        }

        if trimmed.hasPrefix("#") {
            let level = trimmed.prefix(while: { $0 == "#" }).count
            if level <= 6, trimmed.count > level, trimmed[trimmed.index(trimmed.startIndex, offsetBy: level)] == " " {
                flushParagraph()
                blocks.append(.heading(level, String(trimmed.dropFirst(level + 1))))
                continue
            }
        }

        if trimmed.hasPrefix("> ") || trimmed == ">" {
            flushParagraph()
            blocks.append(.quote(String(trimmed.dropFirst(trimmed == ">" ? 1 : 2))))
            continue
        }

        // Lists: -, *, +, "1." (with indentation)
        let indent = line.prefix(while: { $0 == " " }).count / 2
        if let range = trimmed.range(of: #"^([-*+]|\d{1,3}\.)\s+"#, options: .regularExpression) {
            flushParagraph()
            let marker = String(trimmed[range]).trimmingCharacters(in: .whitespaces)
            blocks.append(.listItem(indent: indent, marker: marker, text: String(trimmed[range.upperBound...])))
            continue
        }

        // Table: header | header  /  --- | ---
        if trimmed.contains("|"),
           let sep = lines.first?.trimmingCharacters(in: .whitespaces),
           sep.range(of: #"^\|?\s*:?-{2,}.*\|"#, options: .regularExpression) != nil {
            flushParagraph()
            lines = lines.dropFirst()  // separator row
            func cells(_ s: String) -> [String] {
                s.trimmingCharacters(in: CharacterSet(charactersIn: "| "))
                    .components(separatedBy: "|")
                    .map { $0.trimmingCharacters(in: .whitespaces) }
            }
            let header = cells(trimmed)
            var rows: [[String]] = []
            while let r = lines.first, r.contains("|") {
                lines = lines.dropFirst()
                rows.append(cells(r.trimmingCharacters(in: .whitespaces)))
            }
            blocks.append(.table(header: header, rows: rows))
            continue
        }

        paragraph.append(line)
    }
    flushParagraph()
    return blocks
}

// MARK: - Inline rendering

/// Inline markdown → AttributedString. Shared by chat and editor.
func inlineMarkdown(_ s: String) -> AttributedString {
    // Foundation's markdown parser chokes on megabyte paragraphs — a pasted
    // dataset shouldn't hang the render loop.
    guard s.count <= 12000 else { return AttributedString(s) }
    var opts = AttributedString.MarkdownParsingOptions()
    opts.interpretedSyntax = .inlineOnlyPreservingWhitespace
    var attr = (try? AttributedString(markdown: s, options: opts)) ?? AttributedString(s)
    let accent = JB.accent
    for run in attr.runs {
        if run.inlinePresentationIntent?.contains(.code) == true {
            attr[run.range].font = .jbMono(12.5)
            attr[run.range].backgroundColor = JB.raised
        }
        if run.link != nil {
            attr[run.range].foregroundColor = accent
        }
    }
    return attr
}

private func inline(_ s: String) -> AttributedString { inlineMarkdown(s) }

// MARK: - Outline

struct MDHeading: Identifiable, Equatable {
    let id: Int          // ordinal among headings
    let level: Int
    let title: String
    let offset: Int      // UTF-16 offset of the heading line in the source
}

/// Headings of a markdown source, skipping fenced code. Matches `parseBlocks`.
func markdownOutline(_ source: String) -> [MDHeading] {
    var out: [MDHeading] = []
    var inFence = false
    var offset = 0
    source.enumerateLines { line, _ in
        defer { offset += line.utf16.count + 1 }
        let t = line.trimmingCharacters(in: .whitespaces)
        if t.hasPrefix("```") { inFence.toggle(); return }
        guard !inFence, t.hasPrefix("#") else { return }
        let level = t.prefix(while: { $0 == "#" }).count
        guard level <= 6, t.count > level,
              t[t.index(t.startIndex, offsetBy: level)] == " " else { return }
        let title = String(t.dropFirst(level + 1)).trimmingCharacters(in: .whitespaces)
        out.append(MDHeading(id: out.count, level: level, title: title, offset: offset))
    }
    return out
}

/// Words and a reading-time estimate, in one quiet mono line.
func markdownStats(_ source: String) -> String {
    var words = 0
    source.enumerateSubstrings(in: source.startIndex..., options: [.byWords, .substringNotRequired]) { _, _, _, _ in words += 1 }
    let mins = max(1, Int((Double(words) / 220).rounded()))
    return "\(words) words · \(mins) min"
}

func markdownHeadingID(_ n: Int) -> String { "mdh-\(n)" }

// MARK: - View

struct MarkdownText: View {
    // Blocks are parsed once per init (not per body eval), and rendered
    // with positional ids — a fresh-UUID-per-access id here previously
    // forced SwiftUI to tear down every block on every stream tick.
    private let blocks: [MDBlock]
    private let headingOrdinal: [Int: Int]
    private let document: Bool

    /// `document` switches to the larger long-form type scale (editor preview).
    init(text: String, document: Bool = false) {
        let b = parseBlocks(text)
        self.blocks = b
        self.document = document
        var map: [Int: Int] = [:]
        for (i, blk) in b.enumerated() { if case .heading = blk { map[i] = map.count } }
        self.headingOrdinal = map
    }

    var body: some View {
        VStack(alignment: .leading, spacing: document ? 14 : 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { i, block in
                if let n = headingOrdinal[i] {
                    blockView(block).id(markdownHeadingID(n))
                } else {
                    blockView(block)
                }
            }
        }
        .font(document ? .jb(15) : nil)
        .tint(JB.accent)
        .textSelection(.enabled)
    }

    private var lineGap: CGFloat { document ? 5 : 3 }

    @ViewBuilder
    private func blockView(_ block: MDBlock) -> some View {
        switch block {
        case .heading(let level, let s):
            Text(inline(s))
                .font(headingFont(level))
                .tracking(level <= 2 ? -0.4 : (level == 3 ? -0.2 : 0))
                .foregroundStyle(JB.ink)
                .padding(.top, level <= 2 ? (document ? 12 : 6) : 3)
        case .paragraph(let s):
            Text(inline(s))
                .lineSpacing(lineGap)
        case .code(let lang, let body):
            CodeBlockView(language: lang, code: body)
        case .listItem(let indent, let marker, let s):
            listRow(indent: indent, marker: marker, text: s)
        case .quote(let s):
            HStack(spacing: 12) {
                Rectangle().fill(JB.accent).frame(width: 2)
                Text(inline(s))
                    .lineSpacing(lineGap)
                    .foregroundStyle(JB.ink2)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .rule:
            Rectangle().fill(JB.line).frame(height: 1).padding(.vertical, 4)
        case .image(let url):
            AsyncImage(url: URL(string: url)) { phase in
                switch phase {
                case .success(let img):
                    img.resizable().scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: 300, alignment: .center)
                        .clipShape(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous))
                case .failure:
                    EmptyView()   // best-effort: a dead image link just disappears
                case .empty:
                    RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).fill(JB.raised)
                        .frame(height: 160)
                        .overlay(JBSpinner())
                @unknown default:
                    EmptyView()
                }
            }
            .padding(.vertical, 2)
        case .table(let header, let rows):
            tableView(header: header, rows: rows)
        }
    }

    @ViewBuilder
    private func listRow(indent: Int, marker: String, text: String) -> some View {
        let (task, body) = Self.splitTask(text)
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            if let done = task {
                RoundedRectangle(cornerRadius: 3.5, style: .continuous)
                    .fill(done ? JB.accent : .clear)
                    .overlay(RoundedRectangle(cornerRadius: 3.5, style: .continuous)
                        .strokeBorder(done ? JB.accent : JB.lineStrong, lineWidth: 1))
                    .overlay {
                        if done {
                            Image(systemName: "checkmark").font(.system(size: 8, weight: .bold)).foregroundStyle(JB.onAccent)
                        }
                    }
                    .frame(width: 14, height: 14)
                    .offset(y: 2)
                Text(inline(body)).lineSpacing(lineGap)
                    .foregroundStyle(done ? JB.ink3 : JB.ink)
                    .strikethrough(done, color: JB.ink3)
            } else {
                Text(marker.hasSuffix(".") ? marker : "•")
                    .font(marker.hasSuffix(".") ? .jbMono(12) : nil)
                    .foregroundStyle(JB.ink3)
                    .frame(minWidth: 14, alignment: .trailing)
                Text(inline(body)).lineSpacing(lineGap)
            }
        }
        .padding(.leading, CGFloat(indent) * 18)
    }

    private static func splitTask(_ t: String) -> (Bool?, String) {
        if t.hasPrefix("[ ] ") { return (false, String(t.dropFirst(4))) }
        if t.hasPrefix("[x] ") || t.hasPrefix("[X] ") { return (true, String(t.dropFirst(4))) }
        return (nil, t)
    }

    private func headingFont(_ level: Int) -> Font {
        let sizes: [CGFloat] = document ? [28, 22, 18, 15.5] : [21, 17.5, 15.5, 14.5]
        let size = sizes[min(level, 4) - 1]
        return .jb(size, level <= 3 ? .semibold : .medium)
    }

    private static func isNumeric(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: CharacterSet(charactersIn: " $€£%+-−,"))
        return !t.isEmpty && t.allSatisfy { $0.isNumber || $0 == "." }
    }

    private func tableView(header: [String], rows: [[String]]) -> some View {
        let numeric: [Bool] = header.indices.map { c in
            !rows.isEmpty && rows.allSatisfy { c < $0.count && Self.isNumeric($0[c]) }
        }
        func align(_ c: Int) -> Alignment { c < numeric.count && numeric[c] ? .trailing : .leading }
        return ScrollView(.horizontal, showsIndicators: false) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                GridRow {
                    ForEach(Array(header.enumerated()), id: \.offset) { c, h in
                        Text(inline(h)).font(.jb(12.5, .semibold)).foregroundStyle(JB.ink2)
                            .padding(.horizontal, 12).padding(.vertical, 7)
                            .frame(maxWidth: .infinity, alignment: align(c))
                    }
                }
                .background(JB.raised)
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Rectangle().fill(JB.line).frame(height: 1).gridCellColumns(max(header.count, 1))
                    GridRow {
                        ForEach(Array(row.enumerated()), id: \.offset) { c, cell in
                            Text(inline(cell))
                                .font(numeric.indices.contains(c) && numeric[c] ? .jbMono(12.5) : nil)
                                .padding(.horizontal, 12).padding(.vertical, 6)
                                .frame(maxWidth: .infinity, alignment: align(c))
                        }
                    }
                }
            }
        }
        .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous))
    }
}

// MARK: - Code block

struct CodeBlockView: View {
    let language: String
    let code: String
    @State private var copied = false
    @State private var highlighted: AttributedString?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language.isEmpty ? "code" : language).jbEyebrow()
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    withAnimation(JB.fade) { copied = true }
                    Task { try? await Task.sleep(for: .seconds(1.2)); withAnimation(JB.fade) { copied = false } }
                } label: {
                    Image(systemName: copied ? "checkmark" : "doc.on.doc")
                        .font(.jb(11, .medium))
                        .foregroundStyle(copied ? JB.live : JB.ink3)
                        .frame(width: 22, height: 18)
                        .contentTransition(.symbolEffect(.replace))
                }
                .buttonStyle(.plain)
                .help("Copy")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            Rectangle().fill(JB.line).frame(height: 1)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(highlighted ?? AttributedString(code))
                    .font(.jbMono(12.5))
                    .foregroundStyle(JB.ink)
                    .lineSpacing(2)
                    .fixedSize(horizontal: true, vertical: false)
                    .padding(12)
                    .textSelection(.enabled)
            }
        }
        .background(JB.raised, in: RoundedRectangle(cornerRadius: JB.rCard, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
        .onAppear { highlighted = highlight(code, language: language) }
        .onChange(of: code) { highlighted = highlight(code, language: language) }
    }
}

// MARK: - Lightweight syntax highlighting

private let mdKeywords: Set<String> = [
    // shared
    "if", "else", "for", "while", "return", "func", "function", "def", "class", "struct",
    "enum", "let", "var", "const", "import", "from", "in", "try", "catch", "except",
    "throw", "throws", "async", "await", "switch", "case", "default", "break", "continue",
    "true", "false", "nil", "null", "None", "True", "False", "self", "this", "new",
    "public", "private", "static", "guard", "extension", "protocol", "with", "as", "is",
    "not", "and", "or", "lambda", "yield", "pass", "raise", "elif", "type", "interface",
]

private func highlight(_ code: String, language: String) -> AttributedString {
    // Highlighting is O(lines × patterns); skip it for huge blocks so a
    // pasted log file can't stall the UI.
    let lines = code.components(separatedBy: "\n")
    guard lines.count <= 400 else { return AttributedString(code) }
    var out = AttributedString()
    for (i, line) in lines.enumerated() {
        if i > 0 { out += AttributedString("\n") }
        out += line.count <= 500 ? highlightLine(line) : AttributedString(line)
    }
    return out
}

// Compiled once: highlightLine runs up to 400× per block and re-runs on every
// stream delta.
private let mdNumberRegex = try? NSRegularExpression(pattern: #"\b\d+(\.\d+)?\b"#)
private let mdStringRegex = try? NSRegularExpression(pattern: #""[^"]*"|'[^']*'"#)
private let mdCommentRegex = try? NSRegularExpression(pattern: #"(//|#).*$"#)

private func highlightLine(_ line: String) -> AttributedString {
    var attr = AttributedString(line)

    func color(_ regex: NSRegularExpression?, _ color: Color) {
        guard let regex else { return }
        let ns = line as NSString
        for m in regex.matches(in: line, range: NSRange(location: 0, length: ns.length)) {
            guard let r = Range(m.range, in: line),
                  let ar = attr.range(of: String(line[r]), options: [], locale: nil)
            else { continue }
            // attr.range(of:) finds the FIRST occurrence — good enough for a
            // lightweight highlighter; exact offsets matter less than vibes.
            attr[ar].foregroundColor = color
        }
    }

    // order matters: later passes overwrite earlier ones
    color(mdNumberRegex, JB.warn)
    for word in line.split(whereSeparator: { !$0.isLetter && $0 != "_" }) {
        if mdKeywords.contains(String(word)),
           let r = attr.range(of: String(word)) {
            attr[r].foregroundColor = JB.accent
        }
    }
    color(mdStringRegex, JB.live)
    color(mdCommentRegex, JB.ink3)
    return attr
}
