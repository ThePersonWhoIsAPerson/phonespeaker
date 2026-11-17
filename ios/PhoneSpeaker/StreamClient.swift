import Foundation
import Network

/// Talks to the PhoneSpeaker app on the laptop over UDP port 47830.
/// 1. Sends "PSJOIN\n<id>\n<name>" every second until the laptop answers
///    PSOK (allowed) or PSDENY. PSWAIT means the laptop is asking its user.
/// 2. Then sends "PSHELLO\n<id>" every second and receives
///    "PSA1" + u32 seq + PCM audio packets.
final class StreamClient {
    static let port: NWEndpoint.Port = 47830
    private static let audioMagic = Data("PSA1".utf8)

    enum Event { case awaitingApproval, approved, receiving, waiting, denied, laptopStopped, failed(String) }

    var onEvent: ((Event) -> Void)?

    private let ring: PCMRing
    private let queue = DispatchQueue(label: "phonespeaker.stream", qos: .userInteractive)
    private var connection: NWConnection?
    private var timer: DispatchSourceTimer?
    private var endpoint: NWEndpoint?
    private var deviceId = ""
    private var phoneName = ""
    private var joined = false
    private var toldWaiting = false
    private var nextSeq: UInt32?
    private var pending: [UInt32: Data] = [:]   // seq -> PCM, waiting to be played in order
    private var lastPCM: Data?
    private static let reorderWindow: UInt32 = 3   // wait up to 3 packets (15 ms) for a late one
    private var lastPacket = Date.distantPast
    private var receiving = false

    private(set) var packets = 0
    private(set) var lost = 0

    init(ring: PCMRing) { self.ring = ring }

    func connect(to endpoint: NWEndpoint, deviceId: String, phoneName: String) {
        queue.async {
            self.teardown(sayBye: true)
            self.endpoint = endpoint
            self.deviceId = deviceId
            self.phoneName = phoneName
            self.joined = false
            self.toldWaiting = false
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
                self.sendKeepAlive()
            case .failed(let err), .waiting(let err):
                self.onEvent?(.failed(err.localizedDescription))
                c.cancel()
                self.connection = nil      // reopened on the next tick
            default:
                break
            }
        }
        connection = c
        nextSeq = nil
        pending.removeAll()
        lastPCM = nil
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
        joined = false
    }

    private func tick() {
        if connection == nil { open() }
        sendKeepAlive()
        if receiving && Date().timeIntervalSince(lastPacket) > 2 {
            receiving = false
            onEvent?(.waiting)
        }
    }

    private func sendKeepAlive() {
        let text = joined ? "PSHELLO\n\(deviceId)" : "PSJOIN\n\(deviceId)\n\(phoneName)"
        connection?.send(content: Data(text.utf8), completion: .idempotent)
    }

    private func receive(on c: NWConnection) {
        c.receiveMessage { [weak self, weak c] data, _, _, error in
            guard let self, let c, c === self.connection else { return }
            if let data { self.handle(data) }
            if error == nil {
                self.receive(on: c)
            } else {
                c.cancel()
                self.connection = nil
            }
        }
    }

    private func handle(_ data: Data) {
        if data.starts(with: Self.audioMagic), data.count > 8 {
            handleAudio(data)
            return
        }
        switch String(decoding: data.prefix(16), as: UTF8.self) {
        case "PSWAIT":
            if !toldWaiting && !joined {
                toldWaiting = true
                onEvent?(.awaitingApproval)
            }
        case "PSOK":
            if !joined {
                joined = true
                onEvent?(.approved)
            }
        case "PSDENY":
            teardown(sayBye: false)
            endpoint = nil
            onEvent?(.denied)
        case "PSSTOP":
            teardown(sayBye: false)
            endpoint = nil
            onEvent?(.laptopStopped)
        default:
            break
        }
    }

    private func handleAudio(_ data: Data) {
        let seq = data.withUnsafeBytes { UInt32(bigEndian: $0.loadUnaligned(fromByteOffset: 4, as: UInt32.self)) }
        let pcm = data.subdata(in: 8..<data.count)
        lastPacket = Date()
        if !receiving {
            receiving = true
            onEvent?(.receiving)
        }
        if let next = nextSeq {
            let ahead = seq &- next
            if ahead >= 0x8000_0000 {
                // Already played (usually the duplicate copy). A big jump back means
                // the laptop restarted its stream: follow it.
                if next &- seq < 400 { return }
                pending.removeAll()
                nextSeq = seq
            } else if ahead > 400 {
                pending.removeAll()      // long stall: start again from here
                nextSeq = seq
            }
        } else {
            nextSeq = seq
        }
        if pending[seq] == nil {
            pending[seq] = pcm
            packets += 1
        }
        flush()
    }

    /// Hand packets to the player in order. A packet that is still missing once
    /// newer ones are `reorderWindow` ahead is lost: play a quieter copy of the
    /// previous one instead of a click.
    private func flush() {
        guard var next = nextSeq else { return }
        while true {
            if let pcm = pending.removeValue(forKey: next) {
                ring.write(pcm, offset: 0)
                lastPCM = pcm
            } else if let newest = pending.keys.map({ $0 &- next }).max(), newest >= Self.reorderWindow {
                lost += 1
                if let last = lastPCM { ring.write(Self.softened(last), offset: 0) }
            } else {
                break
            }
            next = next &+ 1
        }
        nextSeq = next
    }

    private static func softened(_ pcm: Data) -> Data {
        var out = pcm
        out.withUnsafeMutableBytes { raw in
            for i in 0..<(raw.count / 2) {
                let v = Int16(littleEndian: raw.loadUnaligned(fromByteOffset: i * 2, as: Int16.self))
                raw.storeBytes(of: (v / 2).littleEndian, toByteOffset: i * 2, as: Int16.self)
            }
        }
        return out
    }
}
