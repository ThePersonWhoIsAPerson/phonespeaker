import SwiftUI

@main
struct PhoneSpeakerApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView().environmentObject(model)
        }
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var showManual = false

    var body: some View {
        NavigationStack {
            List {
                Section {
                    HStack(spacing: 14) {
                        Image(systemName: model.isPlaying ? "speaker.wave.3.fill" : "speaker.slash")
                            .font(.title)
                            .foregroundStyle(model.isPlaying ? Color.accentColor : Color.secondary)
                            .frame(width: 40)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(model.status).font(.headline)
                            if !model.stats.isEmpty {
                                Text(model.stats).font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(.vertical, 6)
                    if model.connectedTo != nil {
                        Button(role: .destructive) { model.disconnect() } label: {
                            Label(model.isPlaying ? "Disconnect" : "Cancel", systemImage: "xmark.circle")
                        }
                    }
                }

                Section {
                    if model.laptops.isEmpty {
                        HStack(spacing: 12) {
                            ProgressView()
                            Text("Looking for laptops… Open PhoneSpeaker on your laptop and keep both on the same Wi-Fi or hotspot.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                    }
                    ForEach(model.laptops) { laptop in
                        Button { model.connect(laptop) } label: {
                            HStack {
                                Image(systemName: "laptopcomputer")
                                    .font(.title3)
                                    .frame(width: 32)
                                VStack(alignment: .leading) {
                                    Text(laptop.name).foregroundStyle(.primary)
                                    Text(laptop.detail).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                if model.connectedTo == laptop.name {
                                    Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                                } else {
                                    Text("Connect").font(.callout).foregroundStyle(Color.accentColor)
                                }
                            }
                        }
                        .disabled(model.connectedTo != nil)
                    }
                } header: {
                    HStack {
                        Text("Nearby laptops")
                        Spacer()
                        if model.scanning && !model.laptops.isEmpty { ProgressView().controlSize(.mini) }
                    }
                } footer: {
                    Text("Tapping a laptop asks it to connect. Accept on the laptop. Pull down to search again.")
                }

                Section {
                    TextField("Name shown on the laptop", text: $model.phoneName)
                        .textInputAutocapitalization(.words)
                        .autocorrectionDisabled()
                } header: {
                    Text("This iPhone's name")
                }

                Section {
                    VStack(alignment: .leading) {
                        Text("Buffer: \(Int(model.latencyMs)) ms")
                        Slider(value: $model.latencyMs, in: 10...300, step: 5)
                    }
                } header: {
                    Text("Delay")
                } footer: {
                    Text("Lower means less delay. If the sound cuts out, the app adds a little on its own (shown as “+auto”). Bluetooth headphones add their own delay on top.")
                }

                Section {
                    DisclosureGroup("Can't find your laptop?", isExpanded: $showManual) {
                        HStack {
                            TextField("Address, e.g. 192.168.1.20", text: $model.manualAddress)
                                .keyboardType(.numbersAndPunctuation)
                                .textInputAutocapitalization(.never)
                                .autocorrectionDisabled()
                            Button("Connect") { model.connectManual() }
                                .disabled(model.manualAddress.isEmpty || model.connectedTo != nil)
                        }
                        Text("The address is under “Laptop address” in the PhoneSpeaker window on your laptop. Also check Settings → PhoneSpeaker → Local Network is on.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .refreshable { await model.scanNow() }
            .navigationTitle("PhoneSpeaker")
        }
    }
}
