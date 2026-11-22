import SwiftUI
import AppKit

/// NSTextView that reports focus changes so the composer can draw its ring.
final class ChatFocusTextView: NSTextView {
    var onFocus: ((Bool) -> Void)?
    override func becomeFirstResponder() -> Bool {
        let ok = super.becomeFirstResponder()
        if ok { onFocus?(true) }
        return ok
    }
    override func resignFirstResponder() -> Bool {
        let ok = super.resignFirstResponder()
        if ok { onFocus?(false) }
        return ok
    }
}

/// The chat input: hand-wrapped NSTextView (the canonical macOS approach —
/// SwiftUI's vertical TextField mis-wraps when focused). Wraps at its width,
/// grows with content to maxHeight, scrolls beyond, Enter sends,
/// Shift-Enter inserts a newline at the cursor. `onKey` sees ↑ ↓ ↩ ⇥ esc
/// first; return true to swallow.
struct ChatInputTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    var focused: Binding<Bool>? = nil
    var maxHeight: CGFloat = 190
    var onKey: ((ChatKey) -> Bool)? = nil
    var onSend: () -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.verticalScroller?.alphaValue = 0.5
        let tv = ChatFocusTextView(frame: .zero)
        tv.minSize = NSSize(width: 0, height: 0)
        tv.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.containerSize = NSSize(width: 0, height: CGFloat.greatestFiniteMagnitude)
        scroll.documentView = tv
        tv.delegate = context.coordinator
        tv.drawsBackground = false
        tv.isRichText = false
        tv.allowsUndo = true
        tv.font = JBFonts.nsFont(14)
        tv.textColor = NSColor(JB.ink)
        tv.insertionPointColor = NSColor(JB.accent)
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.textContainerInset = NSSize(width: 0, height: 2)
        tv.string = text
        tv.onFocus = { [weak coordinator = context.coordinator] f in
            DispatchQueue.main.async { coordinator?.parent.focused?.wrappedValue = f }
        }
        context.coordinator.textView = tv
        DispatchQueue.main.async {
            tv.window?.makeFirstResponder(tv)
            context.coordinator.remeasure()
        }
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let tv = scroll.documentView as? NSTextView else { return }
        tv.textColor = NSColor(JB.ink)
        tv.insertionPointColor = NSColor(JB.accent)
        if tv.string != text {
            tv.string = text
            tv.setSelectedRange(NSRange(location: (text as NSString).length, length: 0))
            context.coordinator.remeasure()
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ChatInputTextView
        weak var textView: NSTextView?

        init(_ parent: ChatInputTextView) { self.parent = parent }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
            remeasure()
        }

        func remeasure() {
            guard let tv = textView,
                  let layout = tv.layoutManager, let container = tv.textContainer else { return }
            layout.ensureLayout(for: container)
            let used = layout.usedRect(for: container).height + tv.textContainerInset.height * 2
            let clamped = min(max(used + 2, 22), parent.maxHeight)
            if abs(clamped - parent.height) > 0.5 {
                DispatchQueue.main.async { self.parent.height = clamped }
            }
        }

        func textView(_ textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
            let key: ChatKey?
            switch commandSelector {
            case #selector(NSResponder.moveUp(_:)): key = .up
            case #selector(NSResponder.moveDown(_:)): key = .down
            case #selector(NSResponder.insertTab(_:)): key = .tab
            case #selector(NSResponder.cancelOperation(_:)): key = .escape
            case #selector(NSResponder.insertNewline(_:)):
                key = NSApp.currentEvent?.modifierFlags.contains(.shift) == true ? nil : .enter
            default: key = nil
            }
            if let key, parent.onKey?(key) == true { return true }
            if commandSelector == #selector(NSResponder.insertNewline(_:)) {
                if NSApp.currentEvent?.modifierFlags.contains(.shift) == true {
                    return false   // Shift-Enter: default newline at the cursor
                }
                parent.onSend()
                return true        // Enter: send, swallow the newline
            }
            return false
        }
    }
}
