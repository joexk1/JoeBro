import SwiftUI
import Quartz

/// Chips shown on messages; click downloads and opens a QuickLook preview —
/// PDFs, images, Office docs, source files all render natively.
struct AttachmentChipsView: View {
    let attachments: [Attachment]
    @State private var previewURL: URL?
    @State private var loadingID: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Images render inline, right in the bubble
            ForEach(attachments.filter { $0.mime.hasPrefix("image/") }) { att in
                AuthedUploadImage(att: att)
                .frame(maxWidth: 320, maxHeight: 240)
                .clipShape(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
                .contentShape(RoundedRectangle(cornerRadius: JB.rCard))
                .onTapGesture { open(att) }
            }
            chipRow
        }
        .sheet(item: Binding(
            get: { previewURL.map(PreviewItem.init) },
            set: { previewURL = $0?.url }
        )) { item in
            QuickLookSheet(url: item.url)
        }
    }

    private var chipRow: some View {
        HStack(spacing: 6) {
            ForEach(attachments.filter { !$0.mime.hasPrefix("image/") }) { att in
                Button {
                    open(att)
                } label: {
                    HStack(spacing: 5) {
                        if loadingID == att.id {
                            JBSpinner(size: 10)
                        } else {
                            Image(systemName: icon(for: att.mime))
                                .font(.jb(11))
                                .foregroundStyle(JB.ink3)
                        }
                        Text(att.name)
                            .font(.jbMono(11))
                            .foregroundStyle(JB.ink2)
                            .lineLimit(1)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 4)
                }
                .buttonStyle(.plain)
                .jbSurface(.raised, radius: 6)
                .jbHoverRow(radius: 6)
                .help("Preview \(att.name)")
            }
        }
    }

    private func open(_ att: Attachment) {
        guard loadingID == nil else { return }
        loadingID = att.id
        Task {
            defer { loadingID = nil }
            if let url = try? await APIClient.shared.downloadAttachment(id: att.id, name: att.name) {
                previewURL = url
            }
        }
    }

    private func icon(for mime: String) -> String {
        if mime.hasPrefix("image/") { return "photo" }
        if mime.contains("pdf") { return "doc.richtext" }
        if mime.hasPrefix("audio/") { return "waveform" }
        if mime.hasPrefix("video/") { return "film" }
        if mime.contains("zip") || mime.contains("tar") { return "archivebox" }
        return "doc.text"
    }
}

private struct PreviewItem: Identifiable {
    let url: URL
    var id: String { url.path }
}

struct QuickLookSheet: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(url.lastPathComponent)
                    .font(.jbMono(12))
                    .foregroundStyle(JB.ink)
                    .lineLimit(1)
                Spacer()
                Button {
                    NSWorkspace.shared.open(url)
                } label: {
                    Image(systemName: "arrow.up.forward.app")
                }
                .buttonStyle(JBIconButtonStyle(size: 26))
                .help("Open in default app")
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(JBIconButtonStyle(size: 26))
                .keyboardShortcut(.cancelAction)
            }
            .padding(12)
            Rectangle().fill(JB.line).frame(height: 1)
            QuickLookPreview(url: url)
        }
        .frame(minWidth: 640, minHeight: 520)
    }
}

/// QLPreviewView wrapper — the system renderer for every document type
/// macOS knows about (PDF, docx, xlsx, images, video, source code…).
struct QuickLookPreview: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> QLPreviewView {
        let view = QLPreviewView(frame: .zero, style: .normal) ?? QLPreviewView()
        view.previewItem = url as NSURL
        return view
    }

    func updateNSView(_ view: QLPreviewView, context: Context) {
        view.previewItem = url as NSURL
    }
}

/// Inline image for an uploaded attachment, fetched with the backend token.
private struct AuthedUploadImage: View {
    let att: Attachment
    @State private var image: NSImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image).resizable().scaledToFit()
            } else if failed {
                Label(att.name, systemImage: "photo").font(.jbMono(11)).foregroundStyle(JB.ink3)
            } else {
                JBSpinner()
            }
        }
        .task(id: att.id) {
            if let d = try? await APIClient.shared.uploadData(id: att.id), let i = NSImage(data: d) {
                image = i
            } else { failed = true }
        }
    }
}
