import SwiftUI

/// Flashcard deck editor + study view. The doc's `content` is always the
/// plain Q/A text form (mirrors jb_apkg.py) — the AI co-edits decks through
/// the usual document tools and .apkg round-trips through the backend
/// converter on open/save.
///
/// Edit mode: a scrollable, editable list of every card.
/// View mode: Anki-style click-through — flip the card, then grade it
/// "still learning" (comes back a few cards later, same session) or
/// "learned" (persisted into the deck as review state).
struct Flashcard: Identifiable, Equatable {
    let id = UUID()
    var front: String
    var back: String
    var learned: Bool
}

enum FlashcardText {
    /// Same lenient rules as the backend parser: `Q:` opens a card, `A:` its
    /// answer, further lines continue the open field, `S: learned` marks it.
    static func parse(_ text: String) -> [Flashcard] {
        var cards: [Flashcard] = []
        var front: [String] = [], back: [String] = []
        var learned = false
        var field: Character? = nil

        func flush() {
            let f = front.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            let b = back.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !f.isEmpty || !b.isEmpty {
                cards.append(Flashcard(front: f, back: b, learned: learned))
            }
            front = []; back = []; learned = false; field = nil
        }

        for line in text.components(separatedBy: "\n") {
            let stripped = line.trimmingCharacters(in: .whitespaces)
            let upper = stripped.uppercased()
            if upper.hasPrefix("Q:") || upper.hasPrefix("A:") || upper.hasPrefix("S:") {
                let value = String(stripped.dropFirst(2)).trimmingCharacters(in: .whitespaces)
                switch upper.first! {
                case "Q": flush(); front = [value]; field = "q"
                case "A": back = [value]; field = "a"
                default: learned = value.lowercased() == "learned"; field = nil
                }
            } else if field == "q" {
                front.append(line)
            } else if field == "a" {
                back.append(line)
            }
        }
        flush()
        return cards
    }

    static func serialize(_ cards: [Flashcard]) -> String {
        let blocks = cards.map { c in
            "Q: " + c.front + "\nA: " + c.back + (c.learned ? "\nS: learned" : "")
        }
        return blocks.isEmpty ? "" : blocks.joined(separator: "\n\n") + "\n"
    }
}

struct FlashcardsEditor: View {
    @Environment(AppStore.self) private var store
    let index: Int
    let study: Bool

    @State private var cards: [Flashcard] = []
    @State private var loadedFrom = ""
    // Study session — the queue holds card ids; "still learning" re-inserts
    // a few positions down (Anki's learning-step feel), "learned" removes.
    @State private var queue: [UUID] = []
    @State private var revealed = false
    @State private var sessionLearned = 0
    @State private var sessionAgain = 0
    @State private var sessionStarted = false
    @FocusState private var focused: Bool
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var doc: EditorDoc? { store.openDocs.indices.contains(index) ? store.openDocs[index] : nil }
    private var stillToLearn: Int { cards.filter { !$0.learned }.count }

    var body: some View {
        Group {
            if study {
                studyView
            } else {
                editList
            }
        }
        .onAppear {
            reloadIfNeeded()
            if study { startSession() }
        }
        .onChange(of: doc?.content ?? "") { reloadIfNeeded() }
        .onChange(of: study) { _, nowStudying in
            if nowStudying { startSession() }
        }
    }

    // MARK: - Shared state plumbing

    private func reloadIfNeeded() {
        let content = doc?.content ?? ""
        if content == loadedFrom { return }   // our own edit echoing back
        cards = FlashcardText.parse(content)
        loadedFrom = content
        if study { startSession() }           // deck changed under us — fresh queue
    }

    private func commit() {
        let text = FlashcardText.serialize(cards)
        guard text != loadedFrom, store.openDocs.indices.contains(index) else { return }
        loadedFrom = text
        store.openDocs[index].content = text
        store.openDocs[index].dirty = true
        store.scheduleAutosave(store.openDocs[index].id)
    }

    // MARK: - Study (view mode)

    private func startSession(all: Bool = false) {
        queue = cards.filter { all || !$0.learned }.map(\.id)
        revealed = false
        sessionLearned = 0
        sessionAgain = 0
        sessionStarted = !queue.isEmpty
    }

    private var currentCard: Flashcard? {
        guard let id = queue.first else { return nil }
        return cards.first { $0.id == id }
    }

    private func gradeStillLearning() {
        guard !queue.isEmpty else { return }
        sessionAgain += 1
        let id = queue.removeFirst()
        // Re-show after ~5 other cards — close enough to retry, far enough to
        // actually recall rather than echo.
        queue.insert(id, at: min(5, queue.count))
        revealed = false
    }

    private func gradeLearned() {
        guard let id = queue.first else { return }
        if let i = cards.firstIndex(where: { $0.id == id }) {
            cards[i].learned = true
            commit()   // registration persists into the .apkg immediately
        }
        sessionLearned += 1
        queue.removeFirst()
        revealed = false
    }

    private var studyView: some View {
        VStack(spacing: 0) {
            if let card = currentCard {
                studyHeader
                Spacer(minLength: 8)
                cardFace(card)
                    .onTapGesture { flip() }
                Spacer(minLength: 8)
                gradeBar
            } else if cards.isEmpty {
                ContentUnavailableView("No cards yet", systemImage: "rectangle.on.rectangle.slash")
            } else {
                sessionSummary
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .contentShape(Rectangle())
        .focusable()
        .focusEffectDisabled()
        .focused($focused)
        .onAppear { focused = true }
        .onKeyPress(.space) { flip(); return .handled }
        .onKeyPress(.leftArrow) { step(-1); return .handled }
        .onKeyPress(.rightArrow) { step(1); return .handled }
        .onKeyPress(.return) { studyKeyAdvance(); return .handled }
        .onKeyPress(characters: CharacterSet(charactersIn: "1")) { _ in
            if revealed { gradeStillLearning() }
            return .handled
        }
        .onKeyPress(characters: CharacterSet(charactersIn: "2")) { _ in
            if revealed { gradeLearned() }
            return .handled
        }
    }

    private func flip() {
        guard currentCard != nil else { return }
        withAnimation(reduceMotion ? nil : JB.smooth) { revealed.toggle() }
    }

    /// ← / →: browse the queue without grading (next sends the card to the back).
    private func step(_ dir: Int) {
        guard queue.count > 1 else { return }
        if dir > 0 { queue.append(queue.removeFirst()) } else { queue.insert(queue.removeLast(), at: 0) }
        withAnimation(reduceMotion ? nil : JB.snap) { revealed = false }
    }

    /// Return: reveal first, then count as "learned" — Anki's default flow.
    private func studyKeyAdvance() {
        guard currentCard != nil else { return }
        if revealed {
            gradeLearned()
        } else {
            withAnimation(reduceMotion ? nil : JB.smooth) { revealed = true }
        }
    }

    private var studyHeader: some View {
        let total = max(sessionLearned + queue.count, 1)
        return VStack(spacing: 10) {
            HStack(spacing: 10) {
                JBDotNumber(value: "\(queue.count)", dot: 2, tint: JB.accent)
                Text("/ \(total)").font(.jb(11)).foregroundStyle(JB.ink3)
                if sessionLearned > 0 {
                    Text("+\(sessionLearned)").font(.jb(11)).foregroundStyle(JB.live)
                }
                if sessionAgain > 0 {
                    Text("↺\(sessionAgain)").font(.jb(11)).foregroundStyle(JB.warn)
                }
                Spacer()
                Button {
                    queue.shuffle()
                    revealed = false
                } label: {
                    Image(systemName: "shuffle")
                }
                .buttonStyle(JBIconButtonStyle(size: 24))
                .help("Shuffle")
            }
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(JB.line)
                    Capsule().fill(JB.accent)
                        .frame(width: g.size.width * CGFloat(sessionLearned) / CGFloat(total))
                        .animation(JB.smooth, value: sessionLearned)
                }
            }
            .frame(height: 2)
        }
    }

    private func cardFace(_ card: Flashcard) -> some View {
        // One card, front/back on a 3D flip. The back face is pre-mirrored so
        // it reads correctly once the container turns 180°.
        ZStack {
            face(label: "Question", text: card.front)
                .opacity(revealed ? 0 : 1)
            face(label: "Answer", text: card.back)
                .rotation3DEffect(.degrees(180), axis: (x: 0, y: 1, z: 0))
                .opacity(revealed ? 1 : 0)
        }
        .rotation3DEffect(.degrees(revealed && !reduceMotion ? 180 : 0), axis: (x: 0, y: 1, z: 0), perspective: 0.5)
        .frame(maxWidth: 560, minHeight: 240)
    }

    private func face(label: String, text: String) -> some View {
        VStack(spacing: 12) {
            Text(label).jbEyebrow()
            ScrollView {
                Text(text.isEmpty ? "(blank)" : text)
                    .font(.jb(20, .medium))
                    .tracking(-0.2)
                    .foregroundStyle(JB.ink)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
                    .textSelection(.enabled)
            }
            .frame(maxHeight: 260)
        }
        .padding(24)
        .frame(maxWidth: .infinity, minHeight: 240)
        .background(JB.raised, in: RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: JB.rPanel, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
    }

    @ViewBuilder
    private var gradeBar: some View {
        if revealed {
            HStack(spacing: 10) {
                Button {
                    gradeStillLearning()
                } label: {
                    Label("Still learning", systemImage: "arrow.counterclockwise")
                        .font(.jb(12.5, .medium))
                        .frame(minWidth: 110)
                }
                .buttonStyle(.jbSecondary)
                .help("Show again in a few cards (1)")
                Button {
                    gradeLearned()
                } label: {
                    Label("Learned", systemImage: "checkmark")
                        .font(.jb(12.5, .medium))
                        .frame(minWidth: 110)
                }
                .buttonStyle(.jbPrimary)
                .help("Mark learned (2 or Return)")
            }
            .padding(.bottom, 6)
        } else {
            // Hold the height so the card doesn't jump when the buttons appear.
            Color.clear.frame(height: 34).padding(.bottom, 6)
        }
    }

    private var sessionSummary: some View {
        VStack(spacing: 14) {
            JBDotNumber(value: "\(sessionLearned)", dot: 3, tint: JB.live)
            if sessionStarted {
                Text(stillToLearn == 0 ? "All \(cards.count) learned" : "Session done")
                    .font(.jb(17, .semibold))
                Text("\(sessionLearned) learned this round" +
                     (sessionAgain > 0 ? " · \(sessionAgain) retries" : "") +
                     (stillToLearn > 0 ? " · \(stillToLearn) still to learn" : ""))
                    .font(.jb(11.5))
                    .foregroundStyle(JB.ink3)
            } else {
                Text("All \(cards.count) learned")
                    .font(.jb(17, .semibold))
            }
            HStack(spacing: 10) {
                if stillToLearn > 0 {
                    Button {
                        startSession()
                    } label: {
                        Label("Continue learning (\(stillToLearn))", systemImage: "play.fill")
                            .font(.jb(12.5, .medium))
                    }
                    .buttonStyle(.jbPrimary)
                    Button {
                        startSession(all: true)
                    } label: {
                        Label("Study all \(cards.count)", systemImage: "arrow.clockwise")
                            .font(.jb(12.5, .medium))
                    }
                    .buttonStyle(.jbSecondary)
                } else {
                    Button {
                        startSession(all: true)
                    } label: {
                        Label("Study again", systemImage: "arrow.clockwise")
                            .font(.jb(12.5, .medium))
                    }
                    .buttonStyle(.jbPrimary)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Edit mode (scrollable list)

    private var editList: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Button {
                    cards.append(Flashcard(front: "", back: "", learned: false))
                    commit()
                } label: {
                    Label("Card", systemImage: "plus")
                }
                Spacer()
                Text("\(cards.count - stillToLearn)/\(cards.count)")
                    .font(.jb(11))
                    .foregroundStyle(JB.ink3)
                    .help("Learned")
            }
            .buttonStyle(.jbGhost)
            .controlSize(.small)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            Rectangle().fill(JB.line).frame(height: 1)
            if cards.isEmpty {
                ContentUnavailableView("No cards yet", systemImage: "rectangle.on.rectangle.slash")
            } else {
                ScrollView {
                    LazyVStack(spacing: 10) {
                        ForEach(cards.indices, id: \.self) { i in
                            editRow(i)
                        }
                    }
                    .padding(12)
                }
            }
        }
    }

    private func editRow(_ i: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("\(i + 1)")
                    .font(.jb(10, .medium))
                    .foregroundStyle(JB.ink3)
                Spacer()
                Button {
                    cards[i].learned.toggle()
                    commit()
                } label: {
                    Label(cards[i].learned ? "Learned" : "Learning",
                          systemImage: cards[i].learned ? "checkmark.seal.fill" : "seal")
                        .font(.jb(10.5))
                        .foregroundStyle(cards[i].learned ? JB.live : JB.ink3)
                }
                .buttonStyle(.borderless)
                .help(cards[i].learned ? "Mark as still learning" : "Mark as learned")
                Button {
                    cards.remove(at: i)
                    commit()
                } label: {
                    Image(systemName: "trash")
                        .font(.jb(10.5))
                        .foregroundStyle(JB.ink3)
                }
                .buttonStyle(.borderless)
            }
            TextField("Question", text: cardBinding(i, \.front), axis: .vertical)
                .textFieldStyle(.plain)
                .font(.jb(13, .medium))
                .lineLimit(1...6)
            Rectangle().fill(JB.line).frame(height: 1)
            TextField("Answer", text: cardBinding(i, \.back), axis: .vertical)
                .textFieldStyle(.plain)
                .font(.jb(12.5))
                .foregroundStyle(JB.ink2)
                .lineLimit(1...8)
        }
        .padding(12)
        .background(JB.raised, in: RoundedRectangle(cornerRadius: JB.rCard, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: JB.rCard, style: .continuous).strokeBorder(JB.line, lineWidth: 1))
    }

    private func cardBinding(_ i: Int, _ keyPath: WritableKeyPath<Flashcard, String>) -> Binding<String> {
        Binding(
            get: { cards.indices.contains(i) ? cards[i][keyPath: keyPath] : "" },
            set: { newValue in
                guard cards.indices.contains(i) else { return }
                cards[i][keyPath: keyPath] = newValue
                commit()
            }
        )
    }
}
