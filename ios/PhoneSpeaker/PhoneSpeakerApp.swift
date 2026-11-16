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
                            Label("Disconnect", systemImage: "xmark.circle")
                        }
                    }
                }

                Section {
                    if model.laptops.isEmpty {
                        Text("Searching… Open PhoneSpeaker on your laptop and make sure both are on the same Wi-Fi.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(model.laptops) { laptop in
                        Button { model.connect(laptop) } label: {
                            HStack {
                                Label(laptop.name, systemImage: "laptopcomputer")
                                Spacer()
                                if model.connectedTo == laptop.name {
                                    Image(systemName: "checkmark").foregroundStyle(Color.accentColor)
                                }
                            }
                        }
                    }
                } header: {
                    Text("Laptops on this Wi-Fi")
                }

                Section {
                    HStack {
                        TextField("Laptop address, e.g. 192.168.1.20", text: $model.manualAddress)
                            .keyboardType(.numbersAndPunctuation)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                        Button("Connect") { model.connectManual() }
                            .disabled(model.manualAddress.isEmpty)
                    }
                } header: {
                    Text("Connect by address")
                } footer: {
                    Text("The address is shown in the PhoneSpeaker window on your laptop.")
                }

                Section {
                    VStack(alignment: .leading) {
                        Text("Buffer: \(Int(model.latencyMs)) ms")
                        Slider(value: $model.latencyMs, in: 30...300, step: 10)
                    }
                } header: {
                    Text("Delay")
                } footer: {
                    Text("Lower means less delay. Raise it if the sound crackles or cuts out.")
                }
            }
            .navigationTitle("PhoneSpeaker")
        }
    }
}
