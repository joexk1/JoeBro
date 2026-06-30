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

    private var doc: EditorDoc? { store.openDocs.indices.contains(index) ? store.openDocs[index] : nil }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider().opacity(0.4)
            ScrollView([.horizontal, .vertical]) {
                Grid(alignment: .leading, horizontalSpacing: 1, verticalSpacing: 1) {
                    ForEach(rows.indices, id: \.self) { r in
                        GridRow {
                            Text("\(r + 1)")
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                                .frame(width: 34, alignment: .trailing)
                            ForEach(columnRange(r), id: \.self) { c in
                                TextField("", text: cellBinding(r, c))
                                    .textFieldStyle(.plain)
                                    .font(.system(size: 12))
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 3)
                                    .frame(width: 130, alignment: .leading)
                                    .background(.background.opacity(0.5))
                                    .overlay(Rectangle().stroke(.quaternary, lineWidth: 0.5))
                            }
                        }
                    }
                }
                .padding(8)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .onAppear(perform: reloadIfNeeded)
        .onChange(of: doc?.content ?? "") { reloadIfNeeded() }
    }

    private var toolbar: some View {
        HStack(spacing: 8) {
            Button { addRow() } label: { Label("Row", systemImage: "plus") }
            Button { addColumn() } label: { Label("Column", systemImage: "plus") }
            Spacer()
            if doc?.dirty == true {
                Text("unsaved").font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.borderless)
        .font(.system(size: 11))
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
