import SwiftUI
import AppKit

/// Commands bridge so SwiftUI toolbar buttons can drive the NSTextView.
final class EditorCommands {
    weak var textView: JBTextView?

    func bold() { textView?.toggleWrap("**") }
    func italic() { textView?.toggleWrap("*") }
    func bullet() { textView?.prefixLines("- ") }
    func numbered() { textView?.prefixLines(numbered: true) }
}

/// NSTextView-backed editor: native find bar (⌘F), line-number gutter,
/// ruled lines + no-wrap for code, ⌘B/⌘I, Obsidian-style bracket pairing.
struct EditorTextView: NSViewRepresentable {
    @Binding var text: String
    var monospaced = false
    var commands: EditorCommands? = nil

    func makeNSView(context: Context) -> NSScrollView {
        // Start from the system-built scroll/text pair (proven sizing), then
        // swap in our subclass with the same geometry.
        let scaffold = NSTextView.scrollableTextView()
        let scroll = scaffold
        let proto = scaffold.documentView as! NSTextView
        let tv = JBTextView(frame: proto.frame)
        tv.autoresizingMask = proto.autoresizingMask
        tv.minSize = proto.minSize
        tv.maxSize = proto.maxSize
        tv.isVerticallyResizable = proto.isVerticallyResizable
        tv.isHorizontallyResizable = proto.isHorizontallyResizable
        scroll.documentView = tv

        tv.ruledLines = monospaced
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.allowsUndo = true
        // ponytail: force contiguous layout — non-contiguous (the default) lays
        // out glyphs lazily and intermittently leaves the last-edited line at the
        // bottom blank until the next event, especially with our custom drawing.
        tv.layoutManager?.allowsNonContiguousLayout = false
        tv.usesFindBar = true
        tv.isIncrementalSearchingEnabled = true
        tv.drawsBackground = false
        scroll.drawsBackground = false
        tv.textColor = .textColor
        tv.insertionPointColor = .textColor
        tv.textContainerInset = NSSize(width: 8, height: 10)
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.isAutomaticTextReplacementEnabled = false

        if monospaced {
            // Code: in-view gutter (numbers + margin + ruled lines).
            // NSRulerView under layer-backed SwiftUI glass corrupted the
            // whole window's compositing — drawing inside the text view
            // sidesteps that machinery entirely.
            tv.gutterWidth = 46
            tv.textContainer?.exclusionPaths = [
                NSBezierPath(rect: NSRect(x: 0, y: 0, width: 46, height: 50_000_000))
            ]
        }

        applyFont(tv)
        tv.string = text
        commands?.textView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        guard let tv = scroll.documentView as? JBTextView else { return }
        context.coordinator.parent = self
        commands?.textView = tv
        // Re-apply mode chrome when the doc type changes — the gutter and
        // ruled lines from a code tab used to persist onto markdown docs.
        if tv.ruledLines != monospaced || (monospaced && tv.gutterWidth == 0) {
            tv.ruledLines = monospaced
            tv.gutterWidth = monospaced ? 46 : 0
            tv.textContainer?.exclusionPaths = monospaced
                ? [NSBezierPath(rect: NSRect(x: 0, y: 0, width: 46, height: 50_000_000))]
                : []
            tv.needsDisplay = true
        }
        if tv.string != text {
            let sel = tv.selectedRange()
            tv.string = text
            if sel.location <= (text as NSString).length {
                tv.setSelectedRange(NSRange(location: min(sel.location, (text as NSString).length), length: 0))
            }
            tv.needsDisplay = true
        }
        applyFont(tv)
    }

    private func applyFont(_ tv: NSTextView) {
        tv.font = monospaced
            ? NSFont.monospacedSystemFont(ofSize: 12.5, weight: .regular)
            : NSFont.systemFont(ofSize: 13)
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: EditorTextView
        init(_ parent: EditorTextView) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let tv = notification.object as? NSTextView else { return }
            parent.text = tv.string
            tv.needsDisplay = true
        }
    }
}

// MARK: - Rich Word-document editor (.doc / .docx)

/// Commands bridge for the rich editor toolbar -> NSTextView.
final class RichEditorCommands {
    weak var textView: NSTextView?

    func bold() { applyTrait(.boldFontMask) }
    func italic() { applyTrait(.italicFontMask) }

    private func applyTrait(_ trait: NSFontTraitMask) {
        guard let tv = textView, let storage = tv.textStorage else { return }
        let range = tv.selectedRange()
        guard range.length > 0 else { return }
        let fm = NSFontManager.shared
        storage.beginEditing()
        storage.enumerateAttribute(.font, in: range, options: []) { val, r, _ in
            let f = (val as? NSFont) ?? NSFont.systemFont(ofSize: 13)
            let has = fm.traits(of: f).contains(trait)
            let nf = has ? fm.convert(f, toNotHaveTrait: trait) : fm.convert(f, toHaveTrait: trait)
            storage.addAttribute(.font, value: nf, range: r)
        }
        storage.endEditing()
        tv.didChangeText()
    }

    /// Toggle a leading bullet on each selected line. Plain "•\t" text so it
    /// survives the round-trip into Word with no list-machinery fragility.
    func bullet() {
        guard let tv = textView, let storage = tv.textStorage else { return }
        let ns = storage.string as NSString
        let lineRange = ns.lineRange(for: tv.selectedRange())
        let lines = ns.substring(with: lineRange).components(separatedBy: "\n")
        let out = lines.map { line -> String in
            if line.isEmpty { return line }
            return line.hasPrefix("•\t") ? String(line.dropFirst(2)) : "•\t" + line
        }.joined(separator: "\n")
        if tv.shouldChangeText(in: lineRange, replacementString: out) {
            storage.replaceCharacters(in: lineRange, with: out)
            tv.didChangeText()
        }
    }

    /// Switch the selection to a font family, keeping its size and bold/italic.
    func setFont(_ family: String) {
        guard let tv = textView, let storage = tv.textStorage else { return }
        let range = tv.selectedRange()
        guard range.length > 0 else { return }
        let fm = NSFontManager.shared
        storage.beginEditing()
        storage.enumerateAttribute(.font, in: range, options: []) { val, r, _ in
            let f = (val as? NSFont) ?? NSFont.systemFont(ofSize: 13)
            let traits = fm.traits(of: f)
            var nf = fm.font(withFamily: family, traits: [], weight: 5, size: f.pointSize)
                ?? NSFont(name: family, size: f.pointSize) ?? f
            if traits.contains(.boldFontMask) { nf = fm.convert(nf, toHaveTrait: .boldFontMask) }
            if traits.contains(.italicFontMask) { nf = fm.convert(nf, toHaveTrait: .italicFontMask) }
            storage.addAttribute(.font, value: nf, range: r)
        }
        storage.endEditing()
        tv.didChangeText()
    }

    /// Set an absolute point size (Word measures type in points) on the
    /// selection — driven by the numeric size field in the toolbar.
    func setSize(_ pt: CGFloat) {
        guard let tv = textView, let storage = tv.textStorage else { return }
        let range = tv.selectedRange()
        guard range.length > 0, pt >= 6 else { return }
        storage.beginEditing()
        storage.enumerateAttribute(.font, in: range, options: []) { val, r, _ in
            let f = (val as? NSFont) ?? NSFont.systemFont(ofSize: 13)
            storage.addAttribute(.font, value: NSFont(descriptor: f.fontDescriptor, size: pt) ?? f, range: r)
        }
        storage.endEditing()
        tv.didChangeText()
    }
}

final class RichPageTextView: NSTextView {
    var showsPageBreakRules = true
    var pageHeight: CGFloat = 792
    var pageTopInset: CGFloat = 0
    var onAddFootnote: (() -> Void)?

    // ponytail: layer-backed NSTextView intermittently leaves the just-typed
    // bottom line stale/blank until the next event. Ensure layout, then force a
    // full redraw this tick and the next (after any frame growth settles).
    override func didChangeText() {
        super.didChangeText()
        if let c = textContainer { layoutManager?.ensureLayout(for: c) }
        needsDisplay = true
        DispatchQueue.main.async { [weak self] in self?.needsDisplay = true }
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard showsPageBreakRules,
              let layoutManager,
              let textContainer,
              !string.isEmpty else { return }

        let ns = string as NSString
        var search = NSRange(location: 0, length: ns.length)
        let marker = "\u{000C}"

        while search.length > 0 {
            let range = ns.range(of: marker, options: [], range: search)
            if range.location == NSNotFound { break }
            drawPageBreakRule(at: range, layoutManager: layoutManager, textContainer: textContainer)
            let next = range.location + max(range.length, 1)
            search = NSRange(location: next, length: max(0, ns.length - next))
        }
    }

    private func drawPageBreakRule(at charRange: NSRange,
                                   layoutManager: NSLayoutManager,
                                   textContainer: NSTextContainer) {
        let glyphRange = layoutManager.glyphRange(forCharacterRange: charRange, actualCharacterRange: nil)
        guard glyphRange.location != NSNotFound else { return }
        var effective = NSRange()
        let line = layoutManager.lineFragmentRect(forGlyphAt: glyphRange.location, effectiveRange: &effective)
        let origin = textContainerOrigin
        let y = origin.y + line.midY
        drawRule(y: y)
    }

    private func drawRule(y: CGFloat) {
        let origin = textContainerOrigin
        let minX = origin.x + 10
        let maxX = bounds.maxX - 24

        NSGraphicsContext.saveGraphicsState()
        let path = NSBezierPath()
        path.move(to: NSPoint(x: minX, y: y))
        path.line(to: NSPoint(x: maxX, y: y))
        path.lineWidth = 1
        path.setLineDash([6, 5], count: 2, phase: 0)
        NSColor.separatorColor.withAlphaComponent(0.75).setStroke()
        path.stroke()
        NSGraphicsContext.restoreGraphicsState()
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        let menu = super.menu(for: event) ?? NSMenu()
        if isEditable {
            if menu.items.isEmpty == false { menu.addItem(.separator()) }
            let item = NSMenuItem(title: "Add Footnote", action: #selector(addFootnoteFromMenu), keyEquivalent: "")
            item.target = self
            menu.addItem(item)
        }
        return menu
    }

    @objc private func addFootnoteFromMenu() {
        onAddFootnote?()
    }
}

/// Native rich-text editor for real Word files. NSAttributedString reads and
/// writes .doc/.docx directly, so formatting, inline images and layout all
/// survive — no Markdown ever. Colours and highlights are flattened on load so
/// everything renders in the editor's text colour (white on the dark theme),
/// and aren't re-baked into the saved file.
struct RichDocEditor: NSViewRepresentable {
    let url: URL
    var commands: RichEditorCommands
    var editable = true
    var onChange: () -> Void = {}
    var onSaved: () -> Void = {}
    var onSelectionSize: (CGFloat) -> Void = { _ in }
    var onSelectionFont: (String) -> Void = { _ in }
    var onAddFootnote: ((NSTextView) -> Void)? = nil

    func makeNSView(context: Context) -> NSScrollView {
        let scaffold = NSTextView.scrollableTextView()
        let proto = scaffold.documentView as! NSTextView
        let tv = RichPageTextView(frame: proto.frame)
        tv.autoresizingMask = proto.autoresizingMask
        tv.minSize = proto.minSize
        tv.maxSize = proto.maxSize
        tv.isVerticallyResizable = proto.isVerticallyResizable
        tv.isHorizontallyResizable = proto.isHorizontallyResizable
        scaffold.documentView = tv
        tv.isRichText = true
        tv.isEditable = editable
        tv.allowsUndo = true
        // ponytail: see EditorTextView — non-contiguous layout drops bottom lines.
        tv.layoutManager?.allowsNonContiguousLayout = false
        tv.usesFindBar = true
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.delegate = context.coordinator
        tv.allowsImageEditing = true
        tv.importsGraphics = true
        tv.onAddFootnote = { [weak tv] in
            guard let tv else { return }
            onAddFootnote?(tv)
        }
        // View mode = the real page: white background, full fidelity. Edit mode
        // = the dark editor: transparent background, text flattened to white.
        tv.drawsBackground = !editable
        tv.backgroundColor = .white
        scaffold.drawsBackground = !editable
        scaffold.backgroundColor = .white
        // Wrap to the visible width (follow the page margins, no horizontal
        // scroll / right-edge hugging).
        tv.isHorizontallyResizable = false
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.lineFragmentPadding = 0

        var docAttrs: NSDictionary?
        if let attr = try? NSAttributedString(url: url, options: [:], documentAttributes: &docAttrs) {
            let m = NSMutableAttributedString(attributedString: attr)
            let full = NSRange(location: 0, length: m.length)
            if editable {
                // Edit mode: flatten colour/highlight to the adaptive system text
                // colour (dark on light mode, light on dark) so the transparent
                // editor never renders white-on-white. Save strips colour again.
                m.addAttribute(.foregroundColor, value: NSColor.textColor, range: full)
                m.removeAttribute(.backgroundColor, range: full)
                m.removeAttribute(.underlineColor, range: full)
            }
            // View mode keeps the document's real colours, sizes and highlights.
            // Some Word docs carry a right-to-left base direction (<w:bidi/>),
            // which flushes every line to the right margin. Force LTR so text
            // reads from the left margin like any normal document.
            m.enumerateAttribute(.paragraphStyle, in: full, options: []) { val, r, _ in
                let base = (val as? NSParagraphStyle) ?? .default
                let ps = base.mutableCopy() as! NSMutableParagraphStyle
                ps.baseWritingDirection = .leftToRight
                m.addAttribute(.paragraphStyle, value: ps, range: r)
            }
            tv.textStorage?.setAttributedString(m)
        }
        // Honour the document's own page margins so layout matches Word, but
        // cap them so a 1-inch page margin doesn't strand text in a thin column.
        let left = (docAttrs?[NSAttributedString.DocumentAttributeKey.leftMargin] as? CGFloat) ?? 48
        let top = (docAttrs?[NSAttributedString.DocumentAttributeKey.topMargin] as? CGFloat) ?? 20
        if let paper = docAttrs?[NSAttributedString.DocumentAttributeKey.paperSize] as? NSValue {
            tv.pageHeight = max(300, paper.sizeValue.height)
        }
        tv.pageTopInset = min(max(top, 10), 48)
        tv.textContainerInset = NSSize(width: min(max(left, 14), 72), height: min(max(top, 10), 48))
        tv.insertionPointColor = .textColor
        if editable { commands.textView = tv }
        context.coordinator.textView = tv
        context.coordinator.url = url
        return scaffold
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        if editable, let tv = scroll.documentView as? RichPageTextView {
            commands.textView = tv
            tv.onAddFootnote = { [weak tv] in
                guard let tv else { return }
                onAddFootnote?(tv)
            }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    static func dismantleNSView(_ nsView: NSScrollView, coordinator: Coordinator) {
        coordinator.flush()   // never lose the last edits when the tab closes
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: RichDocEditor
        weak var textView: NSTextView?
        var url: URL?
        private var pending: DispatchWorkItem?

        init(_ parent: RichDocEditor) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard parent.editable else { return }
            parent.onChange()
            pending?.cancel()
            let work = DispatchWorkItem { [weak self] in self?.save() }
            pending = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.2, execute: work)
        }

        func textViewDidChangeSelection(_ notification: Notification) {
            guard parent.editable, let tv = textView, let storage = tv.textStorage,
                  storage.length > 0 else { return }
            let loc = min(tv.selectedRange().location, storage.length - 1)
            if let f = storage.attribute(.font, at: loc, effectiveRange: nil) as? NSFont {
                parent.onSelectionSize(f.pointSize)
                parent.onSelectionFont(f.familyName ?? f.fontName)
            }
        }

        func flush() {
            pending?.cancel()
            save()
        }

        private func save() {
            guard parent.editable, let tv = textView, let url, let storage = tv.textStorage else { return }
            // Serialize a colour-stripped copy so the on-screen white isn't
            // persisted — the saved .docx keeps Word's default text colour.
            let clean = NSMutableAttributedString(attributedString: storage)
            let full = NSRange(location: 0, length: clean.length)
            clean.removeAttribute(.foregroundColor, range: full)
            clean.removeAttribute(.backgroundColor, range: full)
            let type: NSAttributedString.DocumentType =
                url.pathExtension.lowercased() == "doc" ? .docFormat : .officeOpenXML
            guard let data = try? clean.data(
                from: full, documentAttributes: [.documentType: type]) else { return }
            do {
                try data.write(to: url, options: .atomic)
                parent.onSaved()
            } catch { /* surfaced elsewhere; never crash on save */ }
        }
    }
}

// MARK: - Text view with markdown niceties

final class JBTextView: NSTextView {
    var ruledLines = false
    var gutterWidth: CGFloat = 0

    private static let pairs: [String: String] = [
        "(": ")", "[": "]", "{": "}", "\"": "\"", "'": "'", "`": "`",
    ]

    // ponytail: layer-backed NSTextView intermittently leaves the just-typed
    // bottom line stale/blank until the next event. Ensure layout, then force a
    // full redraw this tick and the next (after any frame growth settles).
    override func didChangeText() {
        super.didChangeText()
        if let c = textContainer { layoutManager?.ensureLayout(for: c) }
        needsDisplay = true
        DispatchQueue.main.async { [weak self] in self?.needsDisplay = true }
    }

    // ⌘B / ⌘I
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        if event.modifierFlags.contains(.command), !event.modifierFlags.contains(.shift) {
            switch event.charactersIgnoringModifiers {
            case "b": toggleWrap("**"); return true
            case "i": toggleWrap("*"); return true
            case "f":
                // No Edit▸Find menu in this app — drive the find bar directly
                let item = NSMenuItem()
                item.tag = NSTextFinder.Action.showFindInterface.rawValue
                performTextFinderAction(item)
                return true
            default: break
            }
        }
        return super.performKeyEquivalent(with: event)
    }

    /// Obsidian-style pairing: opener types both and parks the cursor inside;
    /// typing the closer when it's already next just steps over it.
    override func insertText(_ insertString: Any, replacementRange: NSRange) {
        guard let typed = insertString as? String, typed.count == 1 else {
            super.insertText(insertString, replacementRange: replacementRange)
            return
        }
        let sel = selectedRange()
        let ns = string as NSString

        // Step over an existing closer
        if Self.pairs.values.contains(typed), sel.length == 0,
           sel.location < ns.length,
           ns.substring(with: NSRange(location: sel.location, length: 1)) == typed,
           // for symmetric pairs only step over, never double-insert
           (Self.pairs[typed] == typed || !Self.pairs.keys.contains(typed)) {
            setSelectedRange(NSRange(location: sel.location + 1, length: 0))
            return
        }

        if let closer = Self.pairs[typed] {
            if sel.length > 0 {
                // Wrap the selection
                let inner = ns.substring(with: sel)
                super.insertText(typed + inner + closer, replacementRange: sel)
                setSelectedRange(NSRange(location: sel.location + 1, length: inner.utf16.count))
                return
            }
            super.insertText(typed + closer, replacementRange: replacementRange)
            setSelectedRange(NSRange(location: sel.location + 1, length: 0))
            return
        }

        super.insertText(insertString, replacementRange: replacementRange)
    }

    /// Backspace inside an empty pair removes both.
    override func deleteBackward(_ sender: Any?) {
        let sel = selectedRange()
        let ns = string as NSString
        if sel.length == 0, sel.location > 0, sel.location < ns.length {
            let before = ns.substring(with: NSRange(location: sel.location - 1, length: 1))
            let after = ns.substring(with: NSRange(location: sel.location, length: 1))
            if Self.pairs[before] == after {
                insertText("", replacementRange: NSRange(location: sel.location - 1, length: 2))
                return
            }
        }
        super.deleteBackward(sender)
    }

    /// Toggle a markdown wrapper (** or *) around the selection.
    func toggleWrap(_ marker: String) {
        let sel = selectedRange()
        let ns = string as NSString
        let m = marker.utf16.count
        if sel.length > 0 {
            let inner = ns.substring(with: sel)
            if inner.hasPrefix(marker) && inner.hasSuffix(marker) && inner.utf16.count >= 2 * m {
                let unwrapped = String(inner.dropFirst(marker.count).dropLast(marker.count))
                insertText(unwrapped, replacementRange: sel)
                setSelectedRange(NSRange(location: sel.location, length: unwrapped.utf16.count))
            } else {
                insertText(marker + inner + marker, replacementRange: sel)
                setSelectedRange(NSRange(location: sel.location + m, length: inner.utf16.count))
            }
        } else {
            insertText(marker + marker, replacementRange: sel)
            setSelectedRange(NSRange(location: sel.location + m, length: 0))
        }
    }

    /// Prefix each selected line for bullet / numbered lists.
    func prefixLines(_ prefix: String = "", numbered: Bool = false) {
        let ns = string as NSString
        let lineRange = ns.lineRange(for: selectedRange())
        let block = ns.substring(with: lineRange)
        let lines = block.components(separatedBy: "\n")
        var out: [String] = []
        var n = 1
        for line in lines {
            if line.isEmpty { out.append(line); continue }
            out.append(numbered ? "\(n). \(line)" : prefix + line)
            n += 1
        }
        let replaced = out.joined(separator: "\n")
        insertText(replaced, replacementRange: lineRange)
        setSelectedRange(NSRange(location: lineRange.location, length: replaced.utf16.count))
    }

    // MARK: List & indent niceties (markdown)

    private static let indentUnit = "  "   // two spaces = one indent level

    /// Tab indents the current line / selection; Shift-Tab outdents. On a list
    /// item this turns "- x" into "  - x" (a sub-bullet).
    override func insertTab(_ sender: Any?) { indentSelection(by: 1) }
    override func insertBacktab(_ sender: Any?) { indentSelection(by: -1) }

    /// Return continues a list: it repeats the bullet / next number with the same
    /// indent. Pressing it on an empty item ends the list instead.
    override func insertNewline(_ sender: Any?) {
        let ns = string as NSString
        let sel = selectedRange()
        guard sel.length == 0 else { super.insertNewline(sender); return }
        let lineRange = ns.lineRange(for: NSRange(location: sel.location, length: 0))
        let raw = ns.substring(with: lineRange)
        let line = raw.hasSuffix("\n") ? String(raw.dropLast()) : raw
        guard let info = Self.listPrefix(line) else { super.insertNewline(sender); return }

        let afterMarker = String(line.dropFirst(info.prefix.count))
        if afterMarker.trimmingCharacters(in: .whitespaces).isEmpty {
            // Empty item — end the list by clearing this line's marker.
            let clear = NSRange(location: lineRange.location, length: (info.prefix as NSString).length)
            guard shouldChangeText(in: clear, replacementString: "") else { return }
            textStorage?.replaceCharacters(in: clear, with: "")
            setSelectedRange(NSRange(location: lineRange.location, length: 0))
            didChangeText()
            return
        }
        let nextPrefix = info.isNumber
            ? info.indent + "\(info.number + 1). "
            : info.indent + info.bullet + " "
        let insert = "\n" + nextPrefix
        guard shouldChangeText(in: sel, replacementString: insert) else { return }
        textStorage?.replaceCharacters(in: sel, with: insert)
        setSelectedRange(NSRange(location: sel.location + (insert as NSString).length, length: 0))
        didChangeText()
    }

    private func indentSelection(by direction: Int) {
        let ns = string as NSString
        let sel = selectedRange()
        if sel.length == 0 {
            let lineRange = ns.lineRange(for: sel)
            let raw = ns.substring(with: lineRange)
            let line = raw.hasSuffix("\n") ? String(raw.dropLast()) : raw
            let suffix = raw.hasSuffix("\n") ? "\n" : ""
            let newLine = direction > 0 ? Self.indentUnit + line : Self.outdent(line)
            let replacement = newLine + suffix
            guard shouldChangeText(in: lineRange, replacementString: replacement) else { return }
            textStorage?.replaceCharacters(in: lineRange, with: replacement)
            let diff = (newLine as NSString).length - (line as NSString).length
            setSelectedRange(NSRange(location: max(lineRange.location, sel.location + diff), length: 0))
            didChangeText()
            return
        }
        let lineRange = ns.lineRange(for: sel)
        var lines = ns.substring(with: lineRange).components(separatedBy: "\n")
        let lastEmpty = lines.last == ""
        for i in lines.indices where !(i == lines.count - 1 && lastEmpty) {
            lines[i] = direction > 0 ? Self.indentUnit + lines[i] : Self.outdent(lines[i])
        }
        let out = lines.joined(separator: "\n")
        guard shouldChangeText(in: lineRange, replacementString: out) else { return }
        textStorage?.replaceCharacters(in: lineRange, with: out)
        setSelectedRange(NSRange(location: lineRange.location, length: (out as NSString).length))
        didChangeText()
    }

    private static func outdent(_ line: String) -> String {
        if line.hasPrefix(indentUnit) { return String(line.dropFirst(indentUnit.count)) }
        if line.hasPrefix("\t") { return String(line.dropFirst()) }
        var s = line, n = indentUnit.count
        while n > 0, s.hasPrefix(" ") { s.removeFirst(); n -= 1 }
        return s
    }

    /// Leading indent + list marker on a line, or nil if it isn't a list item.
    private static func listPrefix(_ line: String) -> (prefix: String, indent: String, bullet: String, isNumber: Bool, number: Int)? {
        let chars = Array(line)
        var i = 0
        while i < chars.count, chars[i] == " " || chars[i] == "\t" { i += 1 }
        let indent = String(chars[0..<i])
        guard i < chars.count else { return nil }
        if "-*+".contains(chars[i]), i + 1 < chars.count, chars[i + 1] == " " {
            let b = String(chars[i])
            return (indent + b + " ", indent, b, false, 0)
        }
        var j = i
        while j < chars.count, chars[j].isNumber { j += 1 }
        if j > i, j + 1 < chars.count, chars[j] == ".", chars[j + 1] == " " {
            let num = Int(String(chars[i..<j])) ?? 0
            return (indent + String(chars[i..<j]) + ". ", indent, "", true, num)
        }
        return nil
    }

    /// In-view gutter: line numbers, full-height margin line, and
    /// ruled-paper hairlines — all in plain view drawing, no NSRulerView.
    override func drawBackground(in rect: NSRect) {
        super.drawBackground(in: rect)
        guard gutterWidth > 0 || ruledLines,
              let layout = layoutManager, let container = textContainer else { return }
        let drawRect = rect.intersection(visibleRect)
        guard !drawRect.isEmpty else { return }

        // Margin line — full visible height
        if gutterWidth > 0 {
            NSColor.separatorColor.withAlphaComponent(0.35).setStroke()
            let margin = NSBezierPath()
            margin.move(to: NSPoint(x: gutterWidth - 0.5, y: drawRect.minY))
            margin.line(to: NSPoint(x: gutterWidth - 0.5, y: drawRect.maxY))
            margin.lineWidth = 1
            margin.stroke()
        }

        let glyphs = layout.glyphRange(forBoundingRect: drawRect, in: container)
        let chars = string as NSString
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor,
        ]

        // Line number of the first visible glyph
        var lineNumber = 1
        var idx = 0
        let firstChar = layout.characterIndexForGlyph(at: glyphs.location)
        while idx < firstChar && idx < chars.length {
            idx = NSMaxRange(chars.lineRange(for: NSRange(location: idx, length: 0)))
            lineNumber += 1
        }

        var glyphIdx = glyphs.location
        while glyphIdx < NSMaxRange(glyphs) {
            let charIdx = layout.characterIndexForGlyph(at: glyphIdx)
            let lineRange = chars.lineRange(for: NSRange(location: charIdx, length: 0))
            let lineGlyphRange = layout.glyphRange(forCharacterRange: lineRange, actualCharacterRange: nil)
            let lineRect = layout.boundingRect(forGlyphRange: lineGlyphRange, in: container)
            let topY = lineRect.minY + textContainerInset.height

            if gutterWidth > 0 {
                let label = "\(lineNumber)" as NSString
                let size = label.size(withAttributes: attrs)
                label.draw(at: NSPoint(x: gutterWidth - size.width - 10, y: topY + 1), withAttributes: attrs)
            }
            if ruledLines {
                NSColor.separatorColor.withAlphaComponent(0.16).setStroke()
                let y = (lineRect.maxY + textContainerInset.height - 0.5).rounded() + 0.5
                let line = NSBezierPath()
                line.move(to: NSPoint(x: max(drawRect.minX, gutterWidth), y: y))
                line.line(to: NSPoint(x: drawRect.maxX, y: y))
                line.lineWidth = 0.5
                line.stroke()
            }
            glyphIdx = NSMaxRange(lineGlyphRange)
            lineNumber += 1
        }
    }
}
