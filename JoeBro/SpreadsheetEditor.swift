import SwiftUI

/// Editable spreadsheet grid. The doc's `content` is always CSV — the single
/// in-memory model — so the AI co-edits spreadsheets as plain text through the
/// usual document tools, .csv saves straight to disk, and .xlsx round-trips
/// through the backend converter on open/save.
/// ponytail: per-cell TextFields, fine for everyday sheets. If someone opens a
/// 10k-row monster it'll get heavy — swap in a virtualized grid then.
struct SpreadsheetEditor: View {
    @Environment(AppStore.self) private var store
    let index: Int

    @State private var rows: [[String]] = []
    @State private var loadedFrom = ""
    private struct Cell: Hashable { let r: Int; let c: Int }
    @FocusState private var focus: Cell?

    private var doc: EditorDoc? { store.openDocs.indices.contains(index) ? store.openDocs[index] : nil }

    private static func colName(_ c: Int) -> String {
        var n = c, s = ""
        repeat { s = String(UnicodeScalar(65 + n % 26)!) + s; n = n / 26 - 1 } while n >= 0
        return s
    }

    private static func isNumber(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: CharacterSet(charactersIn: " $€£%,"))
        return !t.isEmpty && Double(t) != nil
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Rectangle().fill(JB.line).frame(height: 1)
            ScrollView([.horizontal, .vertical]) {
                LazyVStack(alignment: .leading, spacing: 0, pinnedViews: [.sectionHeaders]) {
                    Section {
                        ForEach(rows.indices, id: \.self) { r in
                            HStack(spacing: 0) {
                                Text("\(r + 1)")
                                    .font(.jbMono(10))
                                    .foregroundStyle(focus?.r == r ? JB.accent : JB.ink3)
                                    .frame(width: 38, height: 26, alignment: .trailing)
                                    .padding(.trailing, 8)
                                ForEach(columnRange(r), id: \.self) { c in
                                    cell(r, c)
                                }
                            }
                        }
                    } header: {
                        HStack(spacing: 0) {
                            Color.clear.frame(width: 46, height: 24)
                            ForEach(0..<columnCount, id: \.self) { c in
                                Text(Self.colName(c))
                                    .font(.jbMono(10, .medium))
                                    .foregroundStyle(focus?.c == c ? JB.accent : JB.ink3)
                                    .frame(width: 130, height: 24)
                            }
                        }
                        .background(JB.surface)
                        .overlay(alignment: .bottom) { Rectangle().fill(JB.lineStrong).frame(height: 1) }
                    }
                }
                .padding(.trailing, 8)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .onAppear(perform: reloadIfNeeded)
        .onChange(of: doc?.content ?? "") { reloadIfNeeded() }
    }

    private func cell(_ r: Int, _ c: Int) -> some View {
        let value = rows.indices.contains(r) && rows[r].indices.contains(c) ? rows[r][c] : ""
        let numeric = Self.isNumber(value)
        let selected = focus == Cell(r: r, c: c)
        return TextField("", text: cellBinding(r, c))
            .textFieldStyle(.plain)
            .font(numeric ? .jbMono(12) : .jb(12))
            .multilineTextAlignment(numeric ? .trailing : .leading)
            .focused($focus, equals: Cell(r: r, c: c))
            .onSubmit { focus = Cell(r: min(r + 1, rows.count - 1), c: c) }
            .padding(.horizontal, 8)
            .frame(width: 130, height: 26, alignment: numeric ? .trailing : .leading)
            .background(selected ? JB.accentSoft : .clear)
            .overlay(Rectangle().strokeBorder(JB.line, lineWidth: 0.5))
            .overlay { if selected { Rectangle().strokeBorder(JB.accent, lineWidth: 1.5) } }
            .zIndex(selected ? 1 : 0)
    }

    private var toolbar: some View {
        HStack(spacing: 6) {
            Button { addRow() } label: { Label("Row", systemImage: "plus") }
            Button { addColumn() } label: { Label("Column", systemImage: "plus") }
            Spacer()
            if let f = focus {
                Text("\(Self.colName(f.c))\(f.r + 1)").font(.jbMono(11)).foregroundStyle(JB.ink2)
            }
            Text("\(rows.count) × \(columnCount)").font(.jbMono(11)).foregroundStyle(JB.ink3)
        }
        .buttonStyle(.jbGhost)
        .controlSize(.small)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }

    private var columnCount: Int { rows.map(\.count).max() ?? 0 }
    private func columnRange(_ r: Int) -> Range<Int> { 0..<columnCount }

    private func cellBinding(_ r: Int, _ c: Int) -> Binding<String> {
        Binding(
            get: { rows.indices.contains(r) && rows[r].indices.contains(c) ? rows[r][c] : "" },
            set: { newValue in
                guard rows.indices.contains(r) else { return }
                while rows[r].count <= c { rows[r].append("") }
                rows[r][c] = newValue
                commit()
            }
        )
    }

    private func addRow() {
        rows.append(Array(repeating: "", count: max(1, columnCount)))
        commit()
    }

    private func addColumn() {
        if rows.isEmpty { rows = [[""]] }
        let target = columnCount + 1
        for i in rows.indices { while rows[i].count < target { rows[i].append("") } }
        commit()
    }

    private func commit() {
        let csv = CSVTable.serialize(rows)
        loadedFrom = csv
        guard store.openDocs.indices.contains(index) else { return }
        store.openDocs[index].content = csv
        store.openDocs[index].dirty = true
        store.scheduleAutosave(store.openDocs[index].id)
    }

    private func reloadIfNeeded() {
        let content = doc?.content ?? ""
        if content == loadedFrom { return }   // our own edit echoing back
        rows = CSVTable.parse(content)
        if rows.isEmpty { rows = [[""]] }
        loadedFrom = content
    }
}

/// Minimal RFC-4180-ish CSV. Handles quoted fields, escaped quotes, and
/// commas / newlines inside quotes. Serializes with `\n` and quotes only when
/// a field needs it.
enum CSVTable {
    static func parse(_ text: String) -> [[String]] {
        var rows: [[String]] = []
        var row: [String] = []
        var field = ""
        var inQuotes = false
        var sawAny = false
        let chars = Array(text)
        var i = 0
        while i < chars.count {
            let ch = chars[i]
            if inQuotes {
                if ch == "\"" {
                    if i + 1 < chars.count, chars[i + 1] == "\"" { field.append("\""); i += 1 }
                    else { inQuotes = false }
                } else {
                    field.append(ch)
                }
            } else {
                switch ch {
                case "\"": inQuotes = true; sawAny = true
                case ",": row.append(field); field = ""; sawAny = true
                case "\n": row.append(field); field = ""; rows.append(row); row = []; sawAny = false
                case "\r": break   // CRLF: ignore; the \n commits the row
                default: field.append(ch); sawAny = true
                }
            }
            i += 1
        }
        if sawAny || !field.isEmpty || !row.isEmpty {
            row.append(field)
            rows.append(row)
        }
        return rows
    }

    static func serialize(_ rows: [[String]]) -> String {
        rows.map { row in
            row.map { cell in
                if cell.contains(",") || cell.contains("\"") || cell.contains("\n") {
                    return "\"" + cell.replacingOccurrences(of: "\"", with: "\"\"") + "\""
                }
                return cell
            }.joined(separator: ",")
        }.joined(separator: "\n")
    }
}
