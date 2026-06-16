import SwiftUI
import AppKit

/// The chat input: hand-wrapped NSTextView (the canonical macOS approach —
/// SwiftUI's vertical TextField mis-wraps when focused). Wraps at its width,
/// grows with content to maxHeight, scrolls beyond, Enter sends,
/// Shift-Enter inserts a newline at the cursor.
struct ChatInputTextView: NSViewRepresentable {
    @Binding var text: String
    @Binding var height: CGFloat
    var maxHeight: CGFloat = 190
    var onSend: () -> Void

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSTextView.scrollableTextView()
        let tv = scroll.documentView as! NSTextView
        scroll.drawsBackground = false
        scroll.hasVerticalScroller = true
        scroll.verticalScroller?.alphaValue = 0.5
        tv.delegate = context.coordinator
        tv.drawsBackground = false
        tv.isRichText = false
        tv.allowsUndo = true
        tv.font = .systemFont(ofSize: 14)
        tv.textColor = .labelColor
        tv.insertionPointColor = .labelColor
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.textContainerInset = NSSize(width: 0, height: 2)
        tv.string = text
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
        if tv.string != text {
            tv.string = text
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
