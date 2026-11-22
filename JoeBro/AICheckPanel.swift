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
                    withAnimation(JB.smooth) {
                        text = ""
                        result = nil
                        error = nil
                    }
                }
                .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
                .disabled(text.isEmpty && result == nil)
                JBChip(text: "ZeroGPT")
            }
            .frame(height: 18, alignment: .center)
        } content: {
            HStack(spacing: 12) {
                // Input
                VStack(spacing: 10) {
                    TextEditor(text: $text)
                        .font(.jb(13))
                        .scrollContentBackground(.hidden)
                        .padding(10)
                        .overlay(alignment: .topLeading) {
                            if text.isEmpty {
                                Text("Paste text, 20 words or more")
                                    .font(.jb(13))
                                    .foregroundStyle(JB.ink3)
                                    .padding(.top, 14)
                                    .padding(.leading, 16)
                                    .allowsHitTesting(false)
                            }
                        }
                    HStack {
                        Text("\(text.split(separator: " ").count) words")
                            .font(.jbMono(11))
                            .foregroundStyle(JB.ink3)
                        Spacer()
                        Button {
                            check()
                        } label: {
                            if checking { JBSpinner(size: 12, tint: JB.onAccent) }
                            else { Text("Check") }
                        }
                        .buttonStyle(.jbPrimary)
                        .disabled(checking || text.split(separator: " ").count < 20)
                    }
                    .padding(.horizontal, 12)
                    .padding(.bottom, 10)
                }
                .jbSurface(.panel, radius: JB.rPanel)

                // Result
                VStack(spacing: 14) {
                    if let error {
                        PanelFailure(title: "Check failed", detail: error)
                    } else if let r = result {
                        gauge(r)
                        if let flagged = r.flaggedSentences, !flagged.isEmpty {
                            ScrollView {
                                VStack(alignment: .leading, spacing: 6) {
                                    JBSectionHeader("Flagged", count: flagged.count)
                                    ForEach(Array(flagged.enumerated()), id: \.offset) { _, s in
                                        Text(s)
                                            .font(.jb(12))
                                            .padding(8)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                            .background(JB.danger.opacity(0.08), in: RoundedRectangle(cornerRadius: JB.rControl, style: .continuous))
                                            .overlay(RoundedRectangle(cornerRadius: JB.rControl, style: .continuous).strokeBorder(JB.danger.opacity(0.2), lineWidth: 1))
                                    }
                                }
                                .padding(.horizontal, 14)
                            }
                        } else {
                            Text("No sentences flagged")
                                .font(.jbMono(11))
                                .foregroundStyle(JB.ink3)
                        }
                        Spacer()
                    } else {
                        PanelEmpty(art: .search, text: "No result yet")
                    }
                }
                .frame(width: 300)
                .padding(.top, 14)
                .jbSurface(.panel, radius: JB.rPanel)
            }
        }
    }

    private func gauge(_ r: AICheckResult) -> some View {
        VStack(spacing: 6) {
            ZStack {
                Circle()
                    .stroke(JB.line, lineWidth: 6)
                Circle()
                    .trim(from: 0, to: r.aiPercent / 100)
                    .stroke(scoreColor(r.aiPercent), style: StrokeStyle(lineWidth: 6, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                VStack(spacing: 0) {
                    Text("\(Int(r.aiPercent))%")
                        .font(.jbMono(26, .medium))
                    Text("AI").jbEyebrow()
                }
            }
            .frame(width: 110, height: 110)
            if let words = r.totalWords, let ai = r.aiWords {
                Text("\(ai) of \(words) words read as AI")
                    .font(.jbMono(11))
                    .foregroundStyle(JB.ink2)
            }
        }
    }

    private func scoreColor(_ pct: Double) -> Color {
        switch pct {
        case ..<25: return JB.live
        case ..<60: return JB.warn
        default: return JB.danger
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
