import Foundation
import Network
import SwiftUI
import UIKit

struct Laptop: Identifiable, Hashable {
    var id: String { name }
    let name: String
    let detail: String
    let endpoint: NWEndpoint
}

final class AppModel: ObservableObject {
    @Published private(set) var laptops: [Laptop] = []
    @Published private(set) var scanning = false
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
    @Published var phoneName: String {
        didSet { UserDefaults.standard.set(phoneName, forKey: "phoneName") }
    }

    let deviceId: String
    private let player = AudioPlayer()
    private lazy var client = StreamClient(ring: player.ring)
    private var browser: NWBrowser?
    private var bonjourFound: [String: NWEndpoint] = [:]   // name -> endpoint
    private var scanFound: [LANScanner.Found] = []
    private var statsTimer: Timer?
    private var scanTimer: Timer?
    private let scanQueue = DispatchQueue(label: "phonespeaker.scan")

    init() {
        let d = UserDefaults.standard
        let saved = d.double(forKey: "latencyMs")
        latencyMs = saved > 0 ? saved : 40
        manualAddress = d.string(forKey: "manualAddress") ?? ""
        phoneName = d.string(forKey: "phoneName") ?? UIDevice.current.name
        if let id = d.string(forKey: "deviceId") {
            deviceId = id
        } else {
            deviceId = UUID().uuidString
            d.set(deviceId, forKey: "deviceId")
        }
        player.ring.targetFrames = Int(latencyMs * 48)
        client.onEvent = { [weak self] event in
            DispatchQueue.main.async { self?.handle(event) }
        }
        startBrowsing()
        startAutoScan()
    }

    // MARK: discovery

    /// Re-scan every few seconds while not connected, so laptops appear on their own.
    func startAutoScan() {
        scanTimer?.invalidate()
        scan()
        scanTimer = Timer.scheduledTimer(withTimeInterval: 4, repeats: true) { [weak self] _ in
            guard let self, self.connectedTo == nil else { return }
            self.scan()
        }
    }

    func scan(completion: (() -> Void)? = nil) {
        guard !scanning else { completion?(); return }
        scanning = true
        scanQueue.async { [weak self] in
            let found = LANScanner.scan()
            DispatchQueue.main.async {
                guard let self else { return }
                self.scanFound = found
                self.scanning = false
                self.rebuildList()
                completion?()
            }
        }
    }

    @MainActor
    func scanNow() async {
        await withCheckedContinuation { cont in
            scan { cont.resume() }
        }
    }

    private func startBrowsing() {
        browser?.cancel()
        let b = NWBrowser(for: .bonjour(type: "_phonespeaker._udp", domain: nil), using: .udp)
        b.browseResultsChangedHandler = { [weak self] results, _ in
            var found: [String: NWEndpoint] = [:]
            for r in results {
                if case let .service(name, _, _, _) = r.endpoint {
                    found[name.replacingOccurrences(of: "PhoneSpeaker on ", with: "")] = r.endpoint
                }
            }
            DispatchQueue.main.async {
                self?.bonjourFound = found
                self?.rebuildList()
            }
        }
        b.start(queue: .main)
        browser = b
    }

    private func rebuildList() {
        var byName: [String: Laptop] = [:]
        for (name, endpoint) in bonjourFound {
            byName[name] = Laptop(name: name, detail: "Found on Wi-Fi", endpoint: endpoint)
        }
        // A direct address from the scan is the most reliable way to reach it.
        for f in scanFound {
            byName[f.name] = Laptop(name: f.name, detail: f.ip,
                                    endpoint: .hostPort(host: NWEndpoint.Host(f.ip), port: StreamClient.port))
        }
        laptops = byName.values.sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
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
        let name = phoneName.trimmingCharacters(in: .whitespaces)
        client.connect(to: endpoint, deviceId: deviceId, phoneName: name.isEmpty ? "iPhone" : name)
        connectedTo = label
        isPlaying = false
        status = "Asking \(label) to connect…"
        statsTimer?.invalidate()
        statsTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.updateStats()
        }
    }

    func disconnect(message: String = "Not connected") {
        client.disconnect()
        player.stop()
        statsTimer?.invalidate()
        statsTimer = nil
        connectedTo = nil
        isPlaying = false
        status = message
        stats = ""
        scan()
    }

    private func handle(_ event: StreamClient.Event) {
        guard let name = connectedTo else { return }
        switch event {
        case .awaitingApproval:
            status = "Tap Allow on \(name) to connect"
        case .approved:
            status = "Connected to \(name)"
        case .receiving:
            isPlaying = true
            status = "Playing sound from \(name)"
        case .waiting:
            isPlaying = false
            status = "Connected to \(name), waiting for sound…"
        case .denied:
            disconnect(message: "\(name) didn't allow this iPhone")
        case .laptopStopped:
            disconnect(message: "\(name) disconnected this iPhone")
        case .failed(let reason):
            isPlaying = false
            status = "Can't reach \(name) (\(reason)). Retrying…"
        }
    }

    private func updateStats() {
        guard isPlaying else { stats = ""; return }
        let buffered = player.ring.queuedFrames / 48
        let auto = player.ring.autoExtraMs
        stats = "Delay ≈ \(buffered + player.outputLatencyMs) ms" + (auto > 0 ? " (+\(auto) auto)" : "")
            + " · lost \(client.lost) · dropouts \(player.ring.underruns)"
    }
}
