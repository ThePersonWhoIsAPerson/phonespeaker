import Darwin
import Foundation

/// Finds PhoneSpeaker laptops by sending a small "PSDISCOVER" packet to every
/// address on the iPhone's local networks (Wi-Fi and Personal Hotspot) and
/// collecting the "PSHERE\n<name>" replies. Works where Bonjour doesn't,
/// e.g. when the laptop is on this iPhone's hotspot.
enum LANScanner {
    struct Found: Hashable {
        let ip: String
        let name: String
    }

    private struct Subnet {
        let base: UInt32   // host byte order
        let count: UInt32
        let own: UInt32
    }

    private static func localSubnets() -> [Subnet] {
        var result: [Subnet] = []
        var ifap: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&ifap) == 0, let first = ifap else { return [] }
        defer { freeifaddrs(ifap) }
        var cursor: UnsafeMutablePointer<ifaddrs>? = first
        while let ifa = cursor {
            cursor = ifa.pointee.ifa_next
            let flags = Int32(ifa.pointee.ifa_flags)
            guard (flags & IFF_UP) != 0, (flags & IFF_LOOPBACK) == 0,
                  let sa = ifa.pointee.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET),
                  let nm = ifa.pointee.ifa_netmask else { continue }
            let name = String(cString: ifa.pointee.ifa_name)
            // en* = Wi-Fi / USB, bridge* = Personal Hotspot. Skip cellular (pdp_ip*).
            guard name.hasPrefix("en") || name.hasPrefix("bridge") else { continue }
            let addr = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                UInt32(bigEndian: $0.pointee.sin_addr.s_addr)
            }
            var mask = nm.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                UInt32(bigEndian: $0.pointee.sin_addr.s_addr)
            }
            if mask < 0xFFFF_FC00 { mask = 0xFFFF_FF00 }   // huge network: scan our own /24 only
            result.append(Subnet(base: addr & mask, count: ~mask &+ 1, own: addr))
        }
        return result
    }

    /// Blocking; call off the main thread. Takes about `timeout` seconds.
    static func scan(port: UInt16 = 47830, timeout: TimeInterval = 1.2) -> [Found] {
        var targets: [UInt32] = []
        for s in localSubnets() where s.count > 2 {
            for i in 1..<(s.count - 1) {
                let ip = s.base &+ i
                if ip != s.own { targets.append(ip) }
            }
        }
        guard !targets.isEmpty else { return [] }

        let fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP)
        guard fd >= 0 else { return [] }
        defer { close(fd) }
        var tv = timeval(tv_sec: 0, tv_usec: 100_000)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        let message = Array("PSDISCOVER".utf8)
        var found: [String: String] = [:]

        func sendAll() {
            for (n, ip) in targets.enumerated() {
                var addr = sockaddr_in()
                addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
                addr.sin_family = sa_family_t(AF_INET)
                addr.sin_port = port.bigEndian
                addr.sin_addr = in_addr(s_addr: ip.bigEndian)
                _ = withUnsafePointer(to: addr) { ptr in
                    ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        sendto(fd, message, message.count, 0, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
                    }
                }
                if n % 32 == 31 { usleep(2_000) }   // don't flood the Wi-Fi
            }
        }

        func collect(for seconds: TimeInterval) {
            let deadline = Date().addingTimeInterval(seconds)
            var buffer = [UInt8](repeating: 0, count: 512)
            while Date() < deadline {
                var from = sockaddr_in()
                var len = socklen_t(MemoryLayout<sockaddr_in>.size)
                let n = withUnsafeMutablePointer(to: &from) { ptr in
                    ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        recvfrom(fd, &buffer, buffer.count, 0, $0, &len)
                    }
                }
                guard n > 0 else { continue }
                let text = String(decoding: buffer[0..<n], as: UTF8.self)
                let parts = text.components(separatedBy: "\n")
                guard parts.first == "PSHERE" else { continue }
                let ip = String(cString: inet_ntoa(from.sin_addr))
                let name = parts.count > 1 && !parts[1].isEmpty ? parts[1] : ip
                found[ip] = name
            }
        }

        // Two rounds, in case a packet gets lost on busy Wi-Fi.
        sendAll()
        collect(for: timeout / 2)
        sendAll()
        collect(for: timeout / 2)
        return found.map { Found(ip: $0.key, name: $0.value) }
    }
}
