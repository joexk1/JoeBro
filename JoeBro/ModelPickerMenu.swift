import SwiftUI

/// Endpoint-grouped model menu content — one look everywhere a model
/// gets picked (composer, deep research, etc).
struct ModelMenuItems: View {
    let models: [ModelChoice]
    let selectedID: String?
    let onPick: (ModelChoice) -> Void

    private var grouped: [(endpoint: String, models: [ModelChoice])] { Self.grouped(models) }

    /// Endpoint-grouped, local machines first — shared by the menu flavour
    /// (composer) and the native-Picker flavour (every settings-style picker).
    static func grouped(_ models: [ModelChoice]) -> [(endpoint: String, models: [ModelChoice])] {
        var order: [String] = []
        var buckets: [String: [ModelChoice]] = [:]
        for m in models {
            if buckets[m.endpointName] == nil { order.append(m.endpointName) }
            buckets[m.endpointName, default: []].append(m)
        }
        // Local machines first — that's what gets used most.
        order.sort { a, b in
            let la = buckets[a]?.first?.category == "local"
            let lb = buckets[b]?.first?.category == "local"
            if la != lb { return la }
            return a < b
        }
        return order.map { ($0, buckets[$0] ?? []) }
    }

    var body: some View {
        ForEach(grouped, id: \.endpoint) { group in
            Section(group.endpoint) {
                ForEach(group.models) { model in
                    Button {
                        onPick(model)
                    } label: {
                        if selectedID == model.id {
                            Label(model.displayWithTag, systemImage: "checkmark")
                        } else {
                            Text(model.displayWithTag)
                        }
                    }
                }
            }
        }
        if grouped.isEmpty {
            Text("Add a local endpoint in Settings")
        }
    }
}

/// The one model picker. The composer uses the chat model; other places
/// (Deep Research, …) pass their own selection so they look identical.
struct ModelPickerMenu: View {
    @Environment(AppStore.self) private var store
    var selection: ModelChoice?? = nil          // nil = follow the chat model
    var onPick: ((ModelChoice) -> Void)? = nil

    private var current: ModelChoice? { selection ?? store.selectedModel }

    var body: some View {
        Menu {
            ModelMenuItems(models: store.models,
                           selectedID: current?.id,
                           onPick: { m in if let onPick { onPick(m) } else { store.pick(m) } })
            Divider()
            Button("Refresh models") {
                Task { await store.loadModels() }
            }
        } label: {
            HStack(spacing: 5) {
                if let m = current {
                    ProviderLogoView(model: m.modelID, size: 11)
                }
                Text(current?.displayWithTag ?? "Select model")
                    .font(.jb(11.5))
                    .foregroundStyle(JB.ink2)
                    .lineLimit(1)
                Image(systemName: "chevron.down")
                    .font(.jb(8, .semibold))
                    .foregroundStyle(JB.ink3)
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 4)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
    }
}
/// Endpoint-grouped items for a native Picker — the standard pop-up look used
/// by every settings-style model picker (Message Bot, Advisor, default model,
/// research). The composer keeps its richer ModelPickerMenu.
struct ModelPickerItems: View {
    let models: [ModelChoice]

    var body: some View {
        ForEach(ModelMenuItems.grouped(models), id: \.endpoint) { group in
            Section(group.endpoint) {
                ForEach(group.models) { m in
                    Text(m.displayWithTag).tag(m.id)
                }
            }
        }
    }
}
