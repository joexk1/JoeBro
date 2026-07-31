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
    // Style inline code spans
    for run in attr.runs where run.inlinePresentationIntent == .code {
        attr[run.range].font = .system(.body, design: .monospaced)
        attr[run.range].backgroundColor = Color.primary.opacity(0.08)
    }
    return attr
}

private func inline(_ s: String) -> AttributedString { inlineMarkdown(s) }

// MARK: - View

struct MarkdownText: View {
    // Blocks are parsed once per init (not per body eval), and rendered
    // with positional ids — a fresh-UUID-per-access id here previously
    // forced SwiftUI to tear down every block on every stream tick.
    private let blocks: [MDBlock]

    init(text: String) {
        self.blocks = parseBlocks(text)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
        .textSelection(.enabled)
    }

    @ViewBuilder
    private func blockView(_ block: MDBlock) -> some View {
        switch block {
        case .heading(let level, let s):
            Text(inline(s))
                .font(headingFont(level))
                .padding(.top, level <= 2 ? 6 : 2)
        case .paragraph(let s):
            Text(inline(s))
                .lineSpacing(3)
        case .code(let lang, let body):
            CodeBlockView(language: lang, code: body)
        case .listItem(let indent, let marker, let s):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(marker.hasSuffix(".") ? marker : "•")
                    .foregroundStyle(.secondary)
                    .frame(minWidth: 14, alignment: .trailing)
                Text(inline(s)).lineSpacing(3)
            }
            .padding(.leading, CGFloat(indent) * 18)
        case .quote(let s):
            HStack(spacing: 10) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.accentColor.opacity(0.6))
                    .frame(width: 3)
                Text(inline(s))
                    .foregroundStyle(.secondary)
            }
            .fixedSize(horizontal: false, vertical: true)
        case .rule:
            Divider()
        case .image(let url):
            AsyncImage(url: URL(string: url)) { phase in
                switch phase {
                case .success(let img):
                    img.resizable().scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: 300, alignment: .center)
                        .clipShape(RoundedRectangle(cornerRadius: 12))
                case .failure:
                    EmptyView()   // best-effort: a dead image link just disappears
                case .empty:
                    RoundedRectangle(cornerRadius: 12).fill(.quaternary.opacity(0.3))
                        .frame(height: 160)
                        .overlay(ProgressView().controlSize(.small))
                @unknown default:
                    EmptyView()
                }
            }
            .padding(.vertical, 2)
        case .table(let header, let rows):
            tableView(header: header, rows: rows)
        }
    }

    private func headingFont(_ level: Int) -> Font {
        switch level {
        case 1: return .system(.title, design: .rounded, weight: .bold)
        case 2: return .system(.title2, design: .rounded, weight: .bold)
        case 3: return .system(.title3, design: .rounded, weight: .semibold)
        default: return .system(.headline, weight: .semibold)
        }
    }

    private func tableView(header: [String], rows: [[String]]) -> some View {
        Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 6) {
            GridRow {
                ForEach(Array(header.enumerated()), id: \.offset) { _, h in
                    Text(inline(h)).fontWeight(.semibold)
                }
            }
            Divider()
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                GridRow {
                    ForEach(Array(row.enumerated()), id: \.offset) { _, cell in
                        Text(inline(cell))
                    }
                }
            }
        }
        .padding(12)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10))
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
                Text(language.isEmpty ? "code" : language)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(code, forType: .string)
                    copied = true
                    Task { try? await Task.sleep(for: .seconds(1.5)); copied = false }
                } label: {
                    Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
                .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            Divider().opacity(0.5)
            ScrollView(.horizontal, showsIndicators: false) {
                Text(highlighted ?? AttributedString(code))
                    .font(.system(size: 12.5, design: .monospaced))
                    .lineSpacing(2)
                    .padding(12)
                    .textSelection(.enabled)
            }
        }
        .background(Color(nsColor: .textBackgroundColor).opacity(0.55), in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.separator.opacity(0.5), lineWidth: 1))
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
    color(mdNumberRegex, .cyan)
    for word in line.split(whereSeparator: { !$0.isLetter && $0 != "_" }) {
        if mdKeywords.contains(String(word)),
           let r = attr.range(of: String(word)) {
            attr[r].foregroundColor = .pink
        }
    }
    color(mdStringRegex, .orange)
    color(mdCommentRegex, Color.secondary)
    return attr
}
