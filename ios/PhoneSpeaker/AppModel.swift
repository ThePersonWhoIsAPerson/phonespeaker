import Foundation
import Network
import SwiftUI

struct Laptop: Identifiable, Hashable {
    let id: String
    let name: String
    let endpoint: NWEndpoint
}

final class AppModel: ObservableObject {
    @Published var laptops: [Laptop] = []
    @Published var connectedTo: String?
    @Published var status = "Not connected"
    @Published var isPlaying = false
    @Published var stats = ""
    @Published var latencyMs: Double {
        didSet {
            player.ring.targetFrames = Int(latencyMs * 48)
            UserDefaults.standard.set(latencyMs, forKey: "latencyMs")
        }
    }
    @Published var manualAddress: String {
        didSet { UserDefaults.standard.set(manualAddress, forKey: "manualAddress") }
    }

    private let player = AudioPlayer()
    private lazy var client = StreamClient(ring: player.ring)
    private var browser: NWBrowser?
    private var statsTimer: Timer?

    init() {
        let saved = UserDefaults.standard.double(forKey: "latencyMs")
        latencyMs = saved > 0 ? saved : 80
        manualAddress = UserDefaults.standard.string(forKey: "manualAddress") ?? ""
        player.ring.targetFrames = Int(latencyMs * 48)
        client.onEvent = { [weak self] event in
            DispatchQueue.main.async { self?.handle(event) }
        }
        startBrowsing()
    }

    // MARK: discovery

    func startBrowsing() {
        browser?.cancel()
        let b = NWBrowser(for: .bonjour(type: "_phonespeaker._udp", domain: nil), using: .udp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            let found: [Laptop] = results.compactMap { r in
                guard case let .service(name, _, _, _) = r.endpoint else { return nil }
                let pretty = name.replacingOccurrences(of: "PhoneSpeaker on ", with: "")
                return Laptop(id: name, name: pretty, endpoint: r.endpoint)
            }
            DispatchQueue.main.async {
                // A laptop can show up once per network interface; keep one of each.
                var seen = Set<String>()
                self?.laptops = found.filter { seen.insert($0.id).inserted }.sorted { $0.name < $1.name }
            }
        }
        b.start(queue: .main)
        browser = b
    }

    // MARK: connection

    func connect(_ laptop: Laptop) { connect(endpoint: laptop.endpoint, label: laptop.name) }

    func connectManual() {
        let host = manualAddress.trimmingCharacters(in: .whitespaces)
        guard !host.isEmpty else { return }
        connect(endpoint: .hostPort(host: NWEndpoint.Host(host), port: StreamClient.port), label: host)
    }

    private func connect(endpoint: NWEndpoint, label: String) {
        do {
            try player.start()
        } catch {
            status = "Couldn't start audio: \(error.localizedDescription)"
            return
        }
        client.connect(to: endpoint)
        connectedTo = label
        isPlaying = false
        status = "Connecting to \(label)…"
        statsTimer?.invalidate()
        statsTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.updateStats()
        }
    }

    func disconnect() {
        client.disconnect()
        player.stop()
        statsTimer?.invalidate()
        statsTimer = nil
        connectedTo = nil
        isPlaying = false
        status = "Not connected"
        stats = ""
    }

    private func handle(_ event: StreamClient.Event) {
        guard let name = connectedTo else { return }
        switch event {
        case .receiving:
            isPlaying = true
            status = "Playing sound from \(name)"
        case .waiting:
            isPlaying = false
            status = "Waiting for \(name)… Is “Send sound to iPhone” on?"
        case .laptopStopped:
            isPlaying = false
            status = "\(name) stopped sending. Press “Send sound to iPhone” on the laptop."
        case .failed(let reason):
            isPlaying = false
            status = "Can't reach \(name) (\(reason)). Retrying…"
        }
    }

    private func updateStats() {
        let buffered = player.ring.queuedFrames / 48
        stats = "Delay ≈ \(buffered + player.outputLatencyMs) ms · lost \(client.lost) · dropouts \(player.ring.underruns)"
    }
}
