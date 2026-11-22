import EventKit
import SwiftUI

struct CalendarPanel: View {
    @Environment(AppStore.self) private var store
    @State private var events: Loadable<[CalEvent]> = .loading
    @State private var editing: EventDraft?
    @State private var viewMode = "month"     // month | week | agenda
    @State private var anchor = Calendar.current.startOfDay(for: Date())
    @State private var nlBusy = false
    @State private var nlError: String?
    @State private var calendarStatus: IntegrationStatus?

    private var cal: Calendar { Calendar.current }

    private func isoString(_ date: Date) -> String {
        ISO8601DateFormatter().string(from: date)
    }

    private var rangeStart: Date {
        switch viewMode {
        case "month":
            let first = cal.date(from: cal.dateComponents([.year, .month], from: anchor))!
            return cal.date(byAdding: .day, value: -(cal.component(.weekday, from: first) + 5) % 7, to: first)!
        case "week":
            return cal.date(byAdding: .day, value: -((cal.component(.weekday, from: anchor) + 5) % 7), to: anchor)!
        default:
            return anchor
        }
    }

    private var rangeEnd: Date {
        switch viewMode {
        case "month": return cal.date(byAdding: .day, value: 42, to: rangeStart)!
        case "week": return cal.date(byAdding: .day, value: 7, to: rangeStart)!
        default: return cal.date(byAdding: .day, value: 30, to: anchor)!
        }
    }

    var body: some View {
        PanelChrome(title: "Calendar", icon: "calendar") {
            HStack(spacing: 2) {
                ForEach([("month", "Month"), ("week", "Week"), ("agenda", "Agenda")], id: \.0) { key, label in
                    Button(label) { withAnimation(JB.snap) { viewMode = key } }
                        .buttonStyle(JBButtonStyle(kind: viewMode == key ? .secondary : .ghost, compact: true))
                }
            }

            Button { step(-1) } label: { Image(systemName: "chevron.left") }
                .buttonStyle(.jbIcon)
            Text(titleLabel)
                .font(.jb(12, .medium))
                .frame(minWidth: 120)
            Button { step(1) } label: { Image(systemName: "chevron.right") }
                .buttonStyle(.jbIcon)
            Button("Today") { anchor = cal.startOfDay(for: Date()) }
                .buttonStyle(JBButtonStyle(kind: .ghost, compact: true))
            Button {
                editing = EventDraft()
            } label: {
                Image(systemName: "plus")
            }
            .buttonStyle(.jbIcon)
            .help("New event")
            Button {
                Task { await load() }
            } label: {
                Image(systemName: "arrow.clockwise")
            }
            .buttonStyle(.jbIcon)
            .help("Refresh")
        } content: {
            VStack(spacing: 10) {
                quickAddBar

                Group {
                    switch events {
                    case .loading:
                        PanelLoading()
                    case .failed(let e):
                        PanelFailure(title: "Couldn't load events", detail: e)
                    case .loaded(let list):
                        if list.isEmpty, calendarStatus?.configured != true {
                            PanelEmpty(art: .plug, text: "Connect a calendar in Settings")
                        } else {
                            switch viewMode {
                            case "month": monthGrid(list)
                            case "week": weekView(list)
                            default: agendaView(list)
                            }
                        }
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .jbSurface(.panel, radius: JB.rPanel)
            }
        }
        .task(id: "\(anchor.timeIntervalSince1970)-\(viewMode)") { await load() }
        // Stay in sync when macOS Calendar changes outside the app.
        .onReceive(NotificationCenter.default.publisher(for: .EKEventStoreChanged)) { _ in
            Task { await load() }
        }
        .sheet(item: $editing) { draft in
            EventEditorSheet(draft: draft, saveEvent: saveEvent, deleteEvent: deleteEvent) { await load() }
        }
    }

    private var titleLabel: String {
        switch viewMode {
        case "week":
            return "\(rangeStart.formatted(.dateTime.day().month(.abbreviated))) – \(cal.date(byAdding: .day, value: 6, to: rangeStart)!.formatted(.dateTime.day().month(.abbreviated)))"
        default:
            return anchor.formatted(.dateTime.month(.wide).year())
        }
    }

    private func step(_ dir: Int) {
        switch viewMode {
        case "month": anchor = cal.date(byAdding: .month, value: dir, to: anchor)!
        case "week": anchor = cal.date(byAdding: .day, value: 7 * dir, to: anchor)!
        default: anchor = cal.date(byAdding: .day, value: 30 * dir, to: anchor)!
        }
    }

    private func load() async {
        do {
            calendarStatus = try? await APIClient.shared.calendarStatus()
            if calendarStatus?.type == "macos" {
                // Shows the system permission prompt when access is undetermined.
                _ = await MacCalendarBridge.shared.requestFullAccess()
                guard MacCalendarBridge.shared.authorizationAllowsEvents() else {
                    events = .failed(MacCalendarBridge.shared.isDeniedOrRestricted
                        ? "Calendar access is turned off for JoeBro. Enable it in System Settings > Privacy & Security > Calendars."
                        : "Calendar access was not granted.")
                    return
                }
                events = .loaded(macOSEvents(start: rangeStart, end: rangeEnd))
            } else {
                events = .loaded(try await APIClient.shared.calendarEvents(start: rangeStart, end: rangeEnd))
            }
            // Chat anchor deep-link: open that event's editor directly.
            if let target = store.pendingEventID, case .loaded(let list) = events,
               let ev = list.first(where: { $0.uid == target }) {
                store.pendingEventID = nil
                editing = EventDraft(from: ev)
            }
        } catch {
            events = .failed(error.localizedDescription)
        }
    }

    private func macOSEvents(start: Date, end: Date) -> [CalEvent] {
        // Query a BRAND-NEW store every load. The long-lived shared store caches
        // and keeps serving stale data even after reset(), so edits made in the
        // macOS Calendar app never showed up. A fresh store always reads the
        // current on-disk calendar DB. (Authorization is app-wide, so a new
        // instance inherits the granted access with no re-prompt.)
        let store = EKEventStore()
        let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
        return store.events(matching: predicate).map { event in
            CalEvent(
                uid: "macos:" + event.eventIdentifier,
                summary: event.title ?? "Untitled event",
                dtstart: isoString(event.startDate),
                dtend: isoString(event.endDate ?? event.startDate.addingTimeInterval(3600)),
                allDay: event.isAllDay,
                location: event.location,
                description: event.notes,
                calendarId: event.calendar.calendarIdentifier,
                color: nil
            )
        }
    }

    private func saveEvent(_ draft: EventDraft) async throws {
        if calendarStatus?.type == "macos" {
            try saveMacOSEvent(draft)
        } else if let uid = draft.uid {
            try await APIClient.shared.updateEvent(
                uid: uid, summary: draft.summary, start: draft.start, end: draft.end,
                allDay: draft.allDay, location: draft.location, description: draft.notes)
        } else {
            try await APIClient.shared.createEvent(
                summary: draft.summary, start: draft.start, end: draft.end,
                allDay: draft.allDay, location: draft.location, description: draft.notes)
        }
    }

    private func deleteEvent(_ uid: String) async throws {
        if calendarStatus?.type == "macos" {
            try deleteMacOSEvent(uid)
        } else {
            try await APIClient.shared.deleteEvent(uid: uid)
        }
    }

    private func saveMacOSEvent(_ draft: EventDraft) throws {
        guard MacCalendarBridge.shared.authorizationAllowsEvents() else {
            throw NSError(domain: "JoeBroCalendar", code: 1, userInfo: [NSLocalizedDescriptionKey: "macOS Calendar permission is not granted."])
        }
        let store = MacCalendarBridge.shared.store
        let event: EKEvent
        if let uid = draft.uid?.replacingOccurrences(of: "macos:", with: ""),
           let existing = store.event(withIdentifier: uid) {
            event = existing
        } else {
            event = EKEvent(eventStore: store)
            guard let calendar = store.defaultCalendarForNewEvents
                    ?? store.calendars(for: .event).first(where: { $0.allowsContentModifications }) else {
                throw NSError(domain: "JoeBroCalendar", code: 2, userInfo: [NSLocalizedDescriptionKey: "No writable macOS calendar is available."])
            }
            event.calendar = calendar
        }
        event.title = draft.summary
        event.startDate = draft.start
        event.endDate = max(draft.end, draft.start.addingTimeInterval(60))
        event.isAllDay = draft.allDay
        event.location = draft.location.isEmpty ? nil : draft.location
        event.notes = draft.notes.isEmpty ? nil : draft.notes
        try store.save(event, span: .thisEvent, commit: true)
    }

    private func deleteMacOSEvent(_ uid: String) throws {
        guard MacCalendarBridge.shared.authorizationAllowsEvents() else {
            throw NSError(domain: "JoeBroCalendar", code: 1, userInfo: [NSLocalizedDescriptionKey: "macOS Calendar permission is not granted."])
        }
        let eventID = uid.replacingOccurrences(of: "macos:", with: "")
        guard let event = MacCalendarBridge.shared.store.event(withIdentifier: eventID) else { return }
        try MacCalendarBridge.shared.store.remove(event, span: .thisEvent, commit: true)
    }

    // MARK: Natural-language quick add

    private var quickAddBar: some View {
        QuickAddBar(placeholder: "Lunch with Sara friday 1pm",
                    actionIcon: "plus.circle", busy: nlBusy, errorText: nlError) { quickAdd($0) }
    }

    private func quickAdd(_ text: String) {
        guard !text.isEmpty, !nlBusy else { return }
        nlBusy = true
        nlError = nil
        Task {
            defer { nlBusy = false }
            do {
                let r = try await APIClient.shared.quickParseEvent(text: text)
                guard r["ok"]?.boolValue == true, let ev = r["event"] else {
                    nlError = r["error"]?.stringValue ?? "Couldn't parse that"
                    return
                }
                var draft = EventDraft()
                draft.summary = ev["summary"]?.stringValue ?? text
                draft.start = parseISO(ev["dtstart"]?.stringValue ?? "") ?? Date()
                draft.end = parseISO(ev["dtend"]?.stringValue ?? "") ?? draft.start.addingTimeInterval(3600)
                draft.allDay = ev["all_day"]?.boolValue ?? false
                draft.notes = ev["description"]?.stringValue ?? ""
                draft.location = ev["location"]?.stringValue ?? ""
                try await saveEvent(draft)
                await load()
            } catch {
                nlError = error.localizedDescription
            }
        }
    }

    // MARK: Month grid

    private func eventsOn(_ day: Date, _ list: [CalEvent]) -> [CalEvent] {
        // Multi-day events appear on every day they span.
        let dayEnd = cal.date(byAdding: .day, value: 1, to: day)!
        return list.filter { ev in
            guard let s = ev.startDate else { return false }
            let e = ev.endDate ?? s
            return s < dayEnd && e > day
        }
    }

    /// Same spanning rule as `eventsOn`, but parses each event's dates once.
    private func bucketByDay(_ days: [Date], _ list: [CalEvent]) -> [Date: [CalEvent]] {
        let ends = days.map { cal.date(byAdding: .day, value: 1, to: $0) ?? $0 }
        var byDay: [Date: [CalEvent]] = [:]
        for ev in list {
            guard let s = ev.startDate else { continue }
            let e = ev.endDate ?? s
            for (i, day) in days.enumerated() where s < ends[i] && e > day {
                byDay[cal.startOfDay(for: day), default: []].append(ev)
            }
        }
        return byDay
    }

    private func monthGrid(_ list: [CalEvent]) -> some View {
        let days = (0..<42).map { cal.date(byAdding: .day, value: $0, to: rangeStart)! }
        let weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        // Calling eventsOn per cell re-parsed every event's dates 42× per body
        // eval. Bucket the list by day once instead.
        let byDay = bucketByDay(days, list)
        return GeometryReader { geo in
            let padding: CGFloat = 8
            let headerHeight: CGFloat = 26
            let gridWidth = max(1, geo.size.width - padding * 2)
            let gridHeight = max(1, geo.size.height - padding * 2 - headerHeight)
            let cellWidth = gridWidth / 7
            let cellHeight = gridHeight / 6

            ZStack(alignment: .topLeading) {
                HStack(spacing: 0) {
                    ForEach(weekdays, id: \.self) { d in
                        Text(d)
                            .jbEyebrow()
                            .frame(width: cellWidth, height: headerHeight)
                    }
                }

                ZStack(alignment: .topLeading) {
                    Path { path in
                        for col in 0...7 {
                            let x = CGFloat(col) * cellWidth
                            path.move(to: CGPoint(x: x, y: 0))
                            path.addLine(to: CGPoint(x: x, y: cellHeight * 6))
                        }
                        for row in 0...6 {
                            let y = CGFloat(row) * cellHeight
                            path.move(to: CGPoint(x: 0, y: y))
                            path.addLine(to: CGPoint(x: cellWidth * 7, y: y))
                        }
                    }
                    .stroke(JB.line, lineWidth: 1)

                    ForEach(0..<42, id: \.self) { index in
                        let row = index / 7
                        let col = index % 7
                        dayCell(days[index], byDay[cal.startOfDay(for: days[index])] ?? [])
                            .frame(width: cellWidth, height: cellHeight, alignment: .topLeading)
                            .clipped()
                            .position(
                                x: CGFloat(col) * cellWidth + cellWidth / 2,
                                y: CGFloat(row) * cellHeight + cellHeight / 2
                            )
                        }
                    }
                    .frame(width: gridWidth, height: gridHeight, alignment: .topLeading)
                    .offset(y: headerHeight)
            }
            .frame(width: gridWidth, height: headerHeight + gridHeight, alignment: .topLeading)
            .padding(padding)
            .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
    }

    private func dayCell(_ day: Date, _ dayEvents: [CalEvent]) -> some View {
        let isToday = cal.isDateInToday(day)
        let inMonth = cal.isDate(day, equalTo: anchor, toGranularity: .month)
        return VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text("\(cal.component(.day, from: day))")
                    .font(.jb(11, isToday ? .semibold : .regular))
                    .foregroundStyle(isToday ? JB.onAccent : (inMonth ? JB.ink : JB.ink3.opacity(0.7)))
                    .frame(width: 20, height: 20, alignment: .center)
                    .background(isToday ? AnyShapeStyle(JB.accent) : AnyShapeStyle(.clear), in: Circle())
                Spacer(minLength: 0)
            }
            .frame(height: 22, alignment: .topLeading)

            ForEach(dayEvents.prefix(3)) { ev in
                Text(ev.summary)
                    .font(.jb(10, .medium))
                    .foregroundStyle(JB.ink)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1.5)
                    .frame(maxWidth: .infinity, maxHeight: 16, alignment: .leading)
                    .background(JB.accent.opacity(ev.allDay == true || isMultiDay(ev) ? 0.24 : 0.12),
                                in: RoundedRectangle(cornerRadius: 4, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 4, style: .continuous)
                        .strokeBorder(JB.accent.opacity(0.25), lineWidth: 1))
                    .onTapGesture { editing = EventDraft(from: ev) }
            }
            if dayEvents.count > 3 {
                Text("+\(dayEvents.count - 3) more")
                    .font(.jb(9))
                    .lineLimit(1)
                    .foregroundStyle(JB.ink3)
            }
            Spacer(minLength: 0)
        }
        .padding(3)
        .contentShape(Rectangle())
        .onTapGesture(count: 2) {
            var d = EventDraft()
            d.start = cal.date(bySettingHour: 9, minute: 0, second: 0, of: day) ?? day
            d.end = d.start.addingTimeInterval(3600)
            editing = d
        }
    }

    private func isMultiDay(_ ev: CalEvent) -> Bool {
        guard let s = ev.startDate, let e = ev.endDate else { return false }
        return !cal.isDate(s, inSameDayAs: e.addingTimeInterval(-1))
    }

    // MARK: Week view — scrollable time grid

    private let hourHeight: CGFloat = 44
    private let firstHour = 6      // grid starts at 06:00 — no dead midnight band

    private func weekView(_ list: [CalEvent]) -> some View {
        let days = (0..<7).map { cal.date(byAdding: .day, value: $0, to: rangeStart)! }
        return VStack(spacing: 0) {
            // Day headers + all-day strip (top-aligned — center alignment made
            // short headers float mid-row whenever another day had chips)
            HStack(alignment: .top, spacing: 0) {
                Color.clear.frame(width: 44, height: 1)
                ForEach(days, id: \.self) { day in
                    VStack(spacing: 3) {
                        Text(day.formatted(.dateTime.weekday(.abbreviated))).jbEyebrow()
                        Text("\(cal.component(.day, from: day))")
                            .font(.jb(13, cal.isDateInToday(day) ? .semibold : .regular))
                            .foregroundStyle(cal.isDateInToday(day) ? JB.onAccent : JB.ink)
                            .frame(width: 24, height: 24)
                            .background(cal.isDateInToday(day) ? AnyShapeStyle(JB.accent) : AnyShapeStyle(.clear), in: Circle())
                        ForEach(eventsOn(day, list).filter { $0.allDay == true }.prefix(2)) { ev in
                            Text(ev.summary)
                                .font(.jb(9.5, .medium))
                                .foregroundStyle(JB.ink)
                                .lineLimit(1)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 2)
                                .frame(maxWidth: .infinity)
                                .background(JB.accent.opacity(0.22), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
                                .overlay(RoundedRectangle(cornerRadius: 5, style: .continuous)
                                    .strokeBorder(JB.accent.opacity(0.3), lineWidth: 1))
                                .onTapGesture { editing = EventDraft(from: ev) }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .top)
                }
            }
            .padding(.vertical, 8)
            .padding(.trailing, 8)
            Rectangle().fill(JB.line).frame(height: 1)

            // Scrollable 24h grid, opens at 07:00
            ScrollViewReader { proxy in
                ScrollView {
                    HStack(alignment: .top, spacing: 0) {
                        // Hour gutter
                        VStack(spacing: 0) {
                            ForEach(firstHour..<24, id: \.self) { h in
                                Text(String(format: "%02d:00", h))
                                    .font(.jb(9.5))
                                    .foregroundStyle(JB.ink3)
                                    .frame(width: 40, height: hourHeight, alignment: .topTrailing)
                                    // Centre the label ON its grid line, not under it
                                    .offset(y: -6.5)
                                    .id("hour-\(h)")
                            }
                        }
                        .padding(.trailing, 4)

                        ForEach(Array(days.enumerated()), id: \.offset) { _, day in
                            dayColumn(day, list)
                                .frame(maxWidth: .infinity)
                        }
                    }
                    .padding(.trailing, 8)
                }
                .onAppear {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) {
                        proxy.scrollTo("hour-8", anchor: .top)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
    }

    private func dayColumn(_ day: Date, _ list: [CalEvent]) -> some View {
        let timed = eventsOn(day, list)
            .filter { $0.allDay != true }
            .sorted { ($0.startDate ?? .distantPast) < ($1.startDate ?? .distantPast) }
        let gridStart = cal.date(bySettingHour: firstHour, minute: 0, second: 0, of: day) ?? day
        return ZStack(alignment: .top) {
            if cal.isDateInToday(day) {
                JB.accent.opacity(0.05)
            }
            // Hour lines
            VStack(spacing: 0) {
                ForEach(firstHour..<24, id: \.self) { _ in
                    Rectangle()
                        .fill(.clear)
                        .frame(height: hourHeight)
                        .overlay(alignment: .top) {
                            Rectangle().fill(JB.line).frame(height: 1)
                        }
                }
            }
            .overlay(alignment: .leading) {
                Rectangle().fill(JB.line).frame(width: 1)
            }

            // "Now" indicator
            if cal.isDateInToday(day) {
                let mins = Date().timeIntervalSince(gridStart) / 60
                if mins > 0 {
                    Rectangle()
                        .fill(JB.accent)
                        .frame(height: 1.5)
                        .overlay(alignment: .leading) { Circle().fill(JB.accent).frame(width: 6, height: 6).offset(x: -3) }
                        .offset(y: CGFloat(mins) / 60 * hourHeight)
                }
            }

            // Event blocks, clamped to this day's visible band
            ForEach(timed) { ev in
                let s = max(ev.startDate ?? gridStart, gridStart)
                let e = min(ev.endDate ?? s.addingTimeInterval(3600),
                            cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: day))!)
                let y = CGFloat(s.timeIntervalSince(gridStart) / 3600) * hourHeight
                let h = max(CGFloat(e.timeIntervalSince(s) / 3600) * hourHeight, 18)
                VStack(alignment: .leading, spacing: 1) {
                    Text(ev.summary)
                        .font(.jb(10.5, .medium))
                        .foregroundStyle(JB.ink)
                        .lineLimit(h > 34 ? 2 : 1)
                    if h > 30 {
                        Text(s.formatted(date: .omitted, time: .shortened))
                            .font(.jb(9.5))
                            .foregroundStyle(JB.ink2)
                    }
                }
                .padding(.horizontal, 5)
                .padding(.vertical, 3)
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .frame(height: h, alignment: .topLeading)
                .background(JB.accent.opacity(0.14), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(JB.accent.opacity(0.28), lineWidth: 1))
                .overlay(alignment: .leading) {
                    UnevenRoundedRectangle(topLeadingRadius: 6, bottomLeadingRadius: 6)
                        .fill(JB.accent)
                        .frame(width: 2.5)
                }
                .padding(.horizontal, 2)
                .offset(y: y)
                .onTapGesture { editing = EventDraft(from: ev) }
            }
        }
        .frame(height: hourHeight * CGFloat(24 - firstHour), alignment: .top)
        .clipped()
    }

    // MARK: Agenda

    private func agendaView(_ list: [CalEvent]) -> some View {
        let groups = Dictionary(grouping: list) { ev in
            cal.startOfDay(for: ev.startDate ?? Date.distantPast)
        }
        let days = groups.keys.sorted()
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 14) {
                if days.isEmpty {
                    PanelEmpty(art: .done, text: "No events").frame(height: 240)
                }
                ForEach(days, id: \.self) { day in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(dayLabel(day))
                            .font(.jb(11, .medium))
                            .textCase(.uppercase)
                            .tracking(0.6)
                            .foregroundStyle(cal.isDateInToday(day) ? JB.accent : JB.ink3)
                        ForEach(groups[day] ?? []) { ev in
                            agendaRow(ev)
                        }
                    }
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func dayLabel(_ d: Date) -> String {
        if cal.isDateInToday(d) { return "Today" }
        if cal.isDateInTomorrow(d) { return "Tomorrow" }
        return d.formatted(.dateTime.weekday(.wide).day().month())
    }

    private func agendaRow(_ ev: CalEvent) -> some View {
        HStack(spacing: 10) {
            RoundedRectangle(cornerRadius: 1.5)
                .fill(JB.accent)
                .frame(width: 3, height: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text(ev.summary)
                    .font(.jb(13, .medium))
                HStack(spacing: 6) {
                    if ev.allDay == true {
                        Text(isMultiDay(ev) ? "All day · multi-day" : "All day")
                    } else if let s = ev.startDate {
                        Text(s.formatted(date: .omitted, time: .shortened) +
                             (ev.endDate.map { "–" + $0.formatted(date: .omitted, time: .shortened) } ?? ""))
                    }
                    if let loc = ev.location, !loc.isEmpty {
                        Label(loc, systemImage: "mappin").lineLimit(1)
                    }
                }
                .font(.jb(11))
                .foregroundStyle(JB.ink2)
            }
            Spacer()
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .jbHoverRow()
        .contentShape(Rectangle())
        .onTapGesture { editing = EventDraft(from: ev) }
        .contextMenu {
            Button("Edit") { editing = EventDraft(from: ev) }
                    Button("Delete", role: .destructive) {
                        Task {
                            do { try await deleteEvent(ev.uid) }
                            catch { events = .failed(error.localizedDescription); return }
                            await load()
                        }
                    }
        }
    }
}

// MARK: - Editor sheet (unchanged semantics)

struct EventDraft: Identifiable {
    var id = UUID()
    var uid: String?
    var summary = ""
    var start = Date()
    var end = Date().addingTimeInterval(3600)
    var allDay = false
    var location = ""
    var notes = ""

    init() {}

    init(from ev: CalEvent) {
        uid = ev.uid
        summary = ev.summary
        start = ev.startDate ?? Date()
        end = ev.endDate ?? Date().addingTimeInterval(3600)
        allDay = ev.allDay ?? false
        location = ev.location ?? ""
        notes = ev.description ?? ""
    }
}

struct EventEditorSheet: View {
    @State var draft: EventDraft
    let saveEvent: (EventDraft) async throws -> Void
    let deleteEvent: (String) async throws -> Void
    let onSaved: () async -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var saving = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(draft.uid == nil ? "New event" : "Edit event")
                    .font(.jb(14, .semibold))
                Spacer()
                if let uid = draft.uid {
                    Button(role: .destructive) {
                        Task {
                            do { try await deleteEvent(uid) }
                            catch { self.error = error.localizedDescription; return }
                            await onSaved()
                            dismiss()
                        }
                    } label: {
                        Image(systemName: "trash")
                    }
                    .buttonStyle(.jbIcon)
                }
                Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).buttonStyle(.jbGhost)
                Button(draft.uid == nil ? "Add" : "Save") { save() }
                    .buttonStyle(.jbPrimary)
                    .disabled(saving || draft.summary.isEmpty)
            }
            .padding(14)
            Rectangle().fill(JB.line).frame(height: 1)
            Form {
                TextField("Title", text: $draft.summary)
                Toggle("All day", isOn: $draft.allDay).jbToggle()
                DatePicker("Starts", selection: $draft.start,
                           displayedComponents: draft.allDay ? [.date] : [.date, .hourAndMinute])
                DatePicker("Ends", selection: $draft.end,
                           displayedComponents: draft.allDay ? [.date] : [.date, .hourAndMinute])
                TextField("Location", text: $draft.location)
                TextField("Notes", text: $draft.notes, axis: .vertical)
                    .lineLimit(3...6)
                if let error {
                    Text(error).font(.jbCaption).foregroundStyle(JB.danger)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 440, height: 440)
    }

    private func save() {
        saving = true
        Task {
            defer { saving = false }
            do {
                try await saveEvent(draft)
                await onSaved()
                dismiss()
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}
