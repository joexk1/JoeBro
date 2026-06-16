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
                AsyncImage(url: APIClient.shared.baseURL.appendingPathComponent("api/upload/\(att.id)")) { phase in
                    if case .success(let img) = phase {
                        img.resizable().scaledToFit()
                    } else if case .failure = phase {
                        Label(att.name, systemImage: "photo")
                            .font(.system(size: 11))
                    } else {
                        ProgressView().controlSize(.small)
                    }
                }
                .frame(maxWidth: 320, maxHeight: 240)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .contentShape(RoundedRectangle(cornerRadius: 12))
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
                            ProgressView().controlSize(.mini)
                        } else {
                            Image(systemName: icon(for: att.mime))
                                .font(.system(size: 11))
                        }
                        Text(att.name)
                            .font(.system(size: 11.5, weight: .medium))
                            .lineLimit(1)
                    }
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                }
                .buttonStyle(.borderless)
                .joeGlassCapsule()
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
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                Spacer()
                Button {
                    NSWorkspace.shared.open(url)
                } label: {
                    Image(systemName: "arrow.up.forward.app")
                }
                .buttonStyle(.borderless)
                .help("Open in default app")
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut(.cancelAction)
            }
            .padding(12)
            Divider()
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
