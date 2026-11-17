import AVFoundation
import os

/// Thread-safe ring buffer of interleaved stereo Int16 samples with a simple
/// jitter buffer: it waits until `targetFrames` are queued before playing,
/// and drops the oldest audio if the queue grows too far past the target.
final class PCMRing {
    private let capacity: Int            // in samples (always even)
    private var storage: [Int16]
    private var readIndex = 0
    private var writeIndex = 0
    private var count = 0                // samples queued
    private var priming = true
    private var _targetFrames = 1920     // 40 ms at 48 kHz
    private let lock: UnsafeMutablePointer<os_unfair_lock>

    private var extraFrames = 0          // added automatically after dropouts
    private var cleanFrames = 0          // frames played since the last dropout
    private var driftCounter = 0

    private(set) var underruns = 0

    /// Extra buffering added automatically because of dropouts, in ms.
    var autoExtraMs: Int {
        os_unfair_lock_lock(lock); defer { os_unfair_lock_unlock(lock) }
        return extraFrames / 48
    }

    init(seconds: Double = 2.0) {
        capacity = Int(48_000 * seconds) * 2
        storage = [Int16](repeating: 0, count: capacity)
        lock = .allocate(capacity: 1)
        lock.initialize(to: os_unfair_lock())
    }

    deinit { lock.deallocate() }

    var targetFrames: Int {
        get { os_unfair_lock_lock(lock); defer { os_unfair_lock_unlock(lock) }; return _targetFrames }
        set { os_unfair_lock_lock(lock); _targetFrames = newValue; os_unfair_lock_unlock(lock) }
    }

    var queuedFrames: Int {
        os_unfair_lock_lock(lock); defer { os_unfair_lock_unlock(lock) }
        return count / 2
    }

    func reset() {
        os_unfair_lock_lock(lock)
        readIndex = 0; writeIndex = 0; count = 0; priming = true
        extraFrames = 0; cleanFrames = 0; driftCounter = 0
        os_unfair_lock_unlock(lock)
    }

    /// Append little-endian Int16 samples taken from `data` starting at `offset`.
    func write(_ data: Data, offset: Int) {
        let sampleCount = (data.count - offset) / 2
        guard sampleCount > 0 else { return }
        data.withUnsafeBytes { raw in
            os_unfair_lock_lock(lock)
            for i in 0..<sampleCount {
                let v = raw.loadUnaligned(fromByteOffset: offset + i * 2, as: UInt16.self)
                if count == capacity {            // full: forget the oldest sample
                    readIndex = (readIndex + 1) % capacity
                    count -= 1
                }
                storage[writeIndex] = Int16(bitPattern: UInt16(littleEndian: v))
                writeIndex = (writeIndex + 1) % capacity
                count += 1
            }
            os_unfair_lock_unlock(lock)
        }
    }

    /// Called on the real-time audio thread.
    ///
    /// - Keeps about `targetFrames` (+ automatic extra) queued. If the queue drifts
    ///   more than a quarter of the target away (Wi-Fi bursts, or the two devices' clocks running at
    ///   slightly different speeds), it plays one frame in 200 twice or skips one,
    ///   which is inaudible, instead of jumping.
    /// - If the queue runs dry it goes quiet until half the target is back (not the
    ///   whole target, so the gap is short) and adds 20 ms to the target so it
    ///   doesn't keep happening. That extra shrinks again after a clean minute.
    func render(frames: Int, left: UnsafeMutablePointer<Float>, right: UnsafeMutablePointer<Float>?) {
        os_unfair_lock_lock(lock)
        let target = _targetFrames + extraFrames
        if count / 2 > target + 12_000 {     // way too much queued (long stall): jump ahead
            let drop = (count / 2 - target) * 2
            readIndex = (readIndex + drop) % capacity
            count -= drop
        }
        if priming && count / 2 >= max(target / 2, 480) { priming = false }
        let band = max(240, target / 4)   // stay within 5 ms (or a quarter of the target)
        let scale: Float = 1.0 / 32768.0
        for f in 0..<frames {
            if priming || count < 2 {
                if !priming {
                    priming = true
                    underruns += 1
                    extraFrames = min(extraFrames + 960, 9_600)
                    cleanFrames = 0
                }
                left[f] = 0
                right?[f] = 0
                continue
            }
            driftCounter += 1
            var advance = true
            if driftCounter >= 200 {
                let queued = count / 2
                if queued > target + band && count >= 4 {
                    readIndex = (readIndex + 2) % capacity   // skip one frame
                    count -= 2
                    driftCounter = 0
                } else if queued < target - band {
                    advance = false                          // play this frame twice
                    driftCounter = 0
                }
            }
            let l = storage[readIndex]
            let r = storage[readIndex + 1]
            if advance {
                readIndex = (readIndex + 2) % capacity
                count -= 2
            }
            left[f] = Float(l) * scale
            right?[f] = Float(r) * scale
        }
        cleanFrames += frames
        if cleanFrames > 48_000 * 60 && extraFrames > 0 {
            extraFrames = max(0, extraFrames - 480)
            cleanFrames = 0
        }
        os_unfair_lock_unlock(lock)
    }
}

/// Plays whatever is in the ring buffer through the iPhone's current output.
final class AudioPlayer {
    let ring = PCMRing()
    private let engine = AVAudioEngine()
    private var source: AVAudioSourceNode?
    private var observers: [NSObjectProtocol] = []

    init() {
        let format = AVAudioFormat(standardFormatWithSampleRate: 48_000, channels: 2)!
        let ring = self.ring
        let node = AVAudioSourceNode(format: format) { _, _, frameCount, audioBufferList -> OSStatus in
            let buffers = UnsafeMutableAudioBufferListPointer(audioBufferList)
            guard buffers.count > 0,
                  let left = buffers[0].mData?.assumingMemoryBound(to: Float.self) else { return noErr }
            let right = buffers.count > 1 ? buffers[1].mData?.assumingMemoryBound(to: Float.self) : nil
            ring.render(frames: Int(frameCount), left: left, right: right)
            return noErr
        }
        source = node
        engine.attach(node)
        engine.connect(node, to: engine.mainMixerNode, format: format)

        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: AVAudioSession.interruptionNotification,
                                        object: nil, queue: .main) { [weak self] note in
            guard let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  AVAudioSession.InterruptionType(rawValue: raw) == .ended else { return }
            self?.restartIfNeeded()
        })
        observers.append(nc.addObserver(forName: .AVAudioEngineConfigurationChange,
                                        object: engine, queue: .main) { [weak self] _ in
            self?.restartIfNeeded()
        })
    }

    private var wantsToRun = false

    func start() throws {
        wantsToRun = true
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playback, mode: .default, options: [])
        try? session.setPreferredSampleRate(48_000)
        try? session.setPreferredIOBufferDuration(0.005)
        try session.setActive(true)
        ring.reset()
        if !engine.isRunning { try engine.start() }
    }

    func stop() {
        wantsToRun = false
        engine.stop()
        ring.reset()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }

    private func restartIfNeeded() {
        guard wantsToRun else { return }
        try? AVAudioSession.sharedInstance().setActive(true)
        ring.reset()
        if !engine.isRunning { try? engine.start() }
    }

    /// Extra delay added by the iPhone's own output (Bluetooth headphones etc.).
    var outputLatencyMs: Int {
        let s = AVAudioSession.sharedInstance()
        return Int((s.outputLatency + s.ioBufferDuration) * 1000)
    }
}
