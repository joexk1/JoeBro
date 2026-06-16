import SwiftUI

/// ZeroGPT-backed AI detector — paste text, get an AI-likelihood score
/// plus the specific sentences that tripped the detector.
struct AICheckPanel: View {
    @State private var text = ""
    @State private var result: AICheckResult?
    @State private var checking = false
    @State private var error: String?

    var body: some View {
        PanelChrome(title: "AI Check", icon: "checkmark.shield") {
            HStack(alignment: .center, spacing: 14) {
                Button("Clear") {
                    withAnimation(.spring(duration: 0.3)) {
                        text = ""
                        result = nil
                        error = nil
                    }
                }
                .buttonStyle(.borderless)
                .font(.system(size: 12, weight: .medium))
                .disabled(text.isEmpty && result == nil)
                Text("ZeroGPT")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.tertiary)
            }
            .frame(height: 18, alignment: .center)
        } content: {
            HStack(spacing: 12) {
                // Input
                VStack(spacing: 10) {
                    TextEditor(text: $text)
                        .font(.system(size: 13))
                        .scrollContentBackground(.hidden)
                        .padding(10)
                        .overlay(alignment: .topLeading) {
                            if text.isEmpty {
                                Text("Paste text to check (at least ~20 words)…")
                                    .font(.system(size: 13))
                                    .foregroundStyle(.tertiary)
                                    .padding(.top, 14)
                                    .padding(.leading, 16)
                                    .allowsHitTesting(false)
                            }
                        }
                    HStack {
                        Text("\(text.split(separator: " ").count) words")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                        Spacer()
                        Button {
                            check()
                        } label: {
                            if checking { ProgressView().controlSize(.small) }
                            else { Label("Check", systemImage: "wand.and.rays") }
                        }
                        .buttonStyle(.glassProminent)
                        .tint(Color.accentColor)
                        .disabled(checking || text.split(separator: " ").count < 20)
                    }
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
                }
                .joeGlassRect(16)

                // Result
                VStack(spacing: 14) {
                    if let error {
                        ContentUnavailableView("Check failed", systemImage: "exclamationmark.shield", description: Text(error))
                    } else if let r = result {
                        gauge(r)
                        if let flagged = r.flaggedSentences, !flagged.isEmpty {
                            ScrollView {
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("Flagged sentences")
                                        .font(.system(size: 11, weight: .semibold))
                                        .foregroundStyle(.secondary)
                                    ForEach(Array(flagged.enumerated()), id: \.offset) { _, s in
                                        Text(s)
                                            .font(.system(size: 12))
                                            .padding(8)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                            .background(Color.red.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                                    }
                                }
                                .padding(.horizontal, 14)
                            }
                        } else {
                            Text("No sentences flagged")
                                .font(.system(size: 12))
                                .foregroundStyle(.tertiary)
                        }
                        Spacer()
                    } else {
                        ContentUnavailableView("No result yet", systemImage: "checkmark.shield",
                                               description: Text("Run a check to see the AI-likelihood score."))
                    }
                }
                .frame(width: 300)
                .padding(.top, 14)
                .joeGlassRect(16)
            }
        }
    }

    private func gauge(_ r: AICheckResult) -> some View {
        VStack(spacing: 6) {
            ZStack {
                Circle()
                    .stroke(.quaternary, lineWidth: 10)
                Circle()
                    .trim(from: 0, to: r.aiPercent / 100)
                    .stroke(scoreColor(r.aiPercent), style: StrokeStyle(lineWidth: 10, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                VStack(spacing: 0) {
                    Text("\(Int(r.aiPercent))%")
                        .font(.system(size: 26, weight: .bold, design: .rounded))
                    Text("AI")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 110, height: 110)
            if let words = r.totalWords, let ai = r.aiWords {
                Text("\(ai) of \(words) words read as AI")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func scoreColor(_ pct: Double) -> Color {
        switch pct {
        case ..<25: return .green
        case ..<60: return .orange
        default: return .red
        }
    }

    private func check() {
        checking = true
        error = nil
        result = nil
        Task {
            defer { checking = false }
            do {
                result = try await APIClient.shared.aiCheck(text: text)
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
