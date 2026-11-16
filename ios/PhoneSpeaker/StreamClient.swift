import Foundation
import Network

/// Talks to the PhoneSpeaker app on the laptop over UDP port 47830.
/// Sends "PSHELLO" every second; receives "PSA1" + u32 seq + PCM packets.
final class StreamClient {
    static let port: NWEndpoint.Port = 47830
    private static let audioMagic = Data("PSA1".utf8)
    private static let stopMagic = Data("PSSTOP".utf8)

    enum Event { case receiving, waiting, laptopStopped, failed(String) }

    var onEvent: ((Event) -> Void)?

    private let ring: PCMRing
    private let queue = DispatchQueue(label: "phonespeaker.stream", qos: .userInteractive)
    private var connection: NWConnection?
    private var timer: DispatchSourceTimer?
    private var endpoint: NWEndpoint?
    private var expectedSeq: UInt32?
    private var lastPacket = Date.distantPast
    private var receiving = false

    private(set) var packets = 0
    private(set) var lost = 0

    init(ring: PCMRing) { self.ring = ring }

    func connect(to endpoint: NWEndpoint) {
        queue.async {
            self.teardown(sayBye: true)
            self.endpoint = endpoint
            self.packets = 0
            self.lost = 0
            self.open()
            let t = DispatchSource.makeTimerSource(queue: self.queue)
            t.schedule(deadline: .now(), repeating: 1.0)
            t.setEventHandler { [weak self] in self?.tick() }
            t.resume()
            self.timer = t
        }
    }

    func disconnect() {
        queue.async {
            self.teardown(sayBye: true)
            self.endpoint = nil
        }
    }

    private func open() {
        guard let endpoint else { return }
        let params = NWParameters.udp
        params.serviceClass = .interactiveVoice
        let c = NWConnection(to: endpoint, using: params)
        c.stateUpdateHandler = { [weak self, weak c] state in
            guard let self, let c, c === self.connection else { return }
            switch state {
            case .ready:
                self.sendHello()
            case .failed(let err), .waiting(let err):
                self.onEvent?(.failed(err.localizedDescription))
                // Try again on the next timer tick.
                c.cancel()
                self.connection = nil
            default:
                break
            }
        }
        connection = c
        expectedSeq = nil
        receiving = false
        c.start(queue: queue)
        receive(on: c)
    }

    private func teardown(sayBye: Bool) {
        timer?.cancel()
        timer = nil
        if let c = connection {
            if sayBye { c.send(content: Data("PSBYE".utf8), completion: .idempotent) }
            c.cancel()
        }
        connection = nil
        receiving = false
    }

    private func tick() {
        if connection == nil { open() }
        sendHello()
        if receiving && Date().timeIntervalSince(lastPacket) > 2 {
            receiving = false
            onEvent?(.waiting)
        }
    }

    private func sendHello() {
        connection?.send(content: Data("PSHELLO".utf8), completion: .idempotent)
    }

    private func receive(on c: NWConnection) {
        c.receiveMessage { [weak self, weak c] data, _, _, error in
            guard let self, let c, c === self.connection else { return }
            if let data { self.handle(data) }
            if error == nil {
                self.receive(on: c)
            } else {
                // e.g. laptop app not running yet: reopen on the next timer tick.
                c.cancel()
                self.connection = nil
            }
        }
    }

    private func handle(_ data: Data) {
        if data.starts(with: Self.stopMagic) {
            receiving = false
            onEvent?(.laptopStopped)
            return
        }
        guard data.count > 8, data.starts(with: Self.audioMagic) else { return }
        let seq = data.withUnsafeBytes { UInt32(bigEndian: $0.loadUnaligned(fromByteOffset: 4, as: UInt32.self)) }
        if let expected = expectedSeq {
            let gap = seq &- expected
            if gap > 0x8000_0000 { return }           // late / duplicate packet: drop it
            if gap > 0 { lost += Int(min(gap, 1000)) }
        }
        expectedSeq = seq &+ 1
        packets += 1
        lastPacket = Date()
        ring.write(data, offset: 8)
        if !receiving {
            receiving = true
            onEvent?(.receiving)
        }
    }
}
