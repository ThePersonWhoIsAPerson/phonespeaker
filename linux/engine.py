"""PhoneSpeaker streaming engine.

Creates a virtual PipeWire output called "iPhone", makes it the default output
(so nothing plays through the laptop speakers), captures what plays into it and
streams it as raw PCM over UDP to any iPhone that has said hello.

Protocol (UDP, port 47830):
  phone  -> laptop : b"PSHELLO"  every second while listening (keep-alive)
  phone  -> laptop : b"PSBYE"    when the phone disconnects
  laptop -> phone  : b"PSA1" + u32 sequence (big endian) + PCM
                     PCM = 48 kHz, stereo, signed 16-bit little endian,
                     240 frames (5 ms, 960 bytes) per packet
"""

from __future__ import annotations

import shutil
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

PORT = 47830
SINK_NAME = "phonespeaker"
SINK_LABEL = "iPhone (PhoneSpeaker)"
RATE = 48000
CHANNELS = 2
FRAMES_PER_PACKET = 240
BYTES_PER_PACKET = FRAMES_PER_PACKET * CHANNELS * 2
MAGIC = b"PSA1"
CLIENT_TIMEOUT = 5.0
SERVICE_TYPE = "_phonespeaker._udp"


def _pactl(*args: str) -> str:
    return subprocess.run(
        ["pactl", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _null_sink_modules() -> list[str]:
    ids = []
    for line in _pactl("list", "short", "modules").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] == "module-null-sink" and f"sink_name={SINK_NAME}" in parts[2]:
            ids.append(parts[0])
    return ids


def _sink_inputs() -> list[str]:
    return [l.split("\t")[0] for l in _pactl("list", "short", "sink-inputs").splitlines() if l]


def _move_all_to(sink: str) -> None:
    for idx in _sink_inputs():
        subprocess.run(["pactl", "move-sink-input", idx, sink], capture_output=True)


def _sink_index(name: str) -> str | None:
    for line in _pactl("list", "short", "sinks").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


def _move_strays_to(sink: str) -> None:
    """Pull streams that remembered another output (e.g. speakers) onto `sink`."""
    target = _sink_index(sink)
    if target is None:
        return
    for line in _pactl("list", "short", "sink-inputs").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] != target:
            subprocess.run(["pactl", "move-sink-input", parts[0], sink], capture_output=True)


def local_ips() -> list[str]:
    """Best-effort list of this laptop's LAN IPv4 addresses."""
    ips: list[str] = []
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[1] != "lo":
                ips.append(parts[3].split("/")[0])
    except OSError:
        pass
    return ips


@dataclass
class Client:
    addr: tuple[str, int]
    last_seen: float = field(default_factory=time.monotonic)


class Engine:
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self.on_change = on_change or (lambda: None)
        self.running = False
        self.error: str | None = None
        self.clients: dict[tuple[str, int], Client] = {}
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._parec: subprocess.Popen | None = None
        self._avahi: subprocess.Popen | None = None
        self._prev_sink: str | None = None
        self._module_id: str | None = None
        self._threads: list[threading.Thread] = []
        self.cleanup_stale()

    # ---------- audio device handling ----------
    @staticmethod
    def cleanup_stale() -> None:
        """Remove a virtual sink left behind by a crash."""
        try:
            for mid in _null_sink_modules():
                subprocess.run(["pactl", "unload-module", mid], capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            pass

    def start(self) -> None:
        if self.running:
            return
        self.error = None
        try:
            self._prev_sink = _pactl("get-default-sink")
            self._module_id = _pactl(
                "load-module", "module-null-sink",
                f"sink_name={SINK_NAME}",
                f"rate={RATE}", f"channels={CHANNELS}",
                f"sink_properties=device.description='{SINK_LABEL}'",
            )
            _pactl("set-default-sink", SINK_NAME)
            _move_all_to(SINK_NAME)

            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", PORT))
            self._sock.settimeout(0.5)

            self._parec = subprocess.Popen(
                ["parec", "-d", f"{SINK_NAME}.monitor", "--raw",
                 "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}",
                 "--latency-msec=10", "--client-name=PhoneSpeaker"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
            self._publish()
        except Exception as exc:  # noqa: BLE001 - surface any setup failure to the UI
            self.error = str(exc)
            self.stop()
            self.on_change()
            return

        self.running = True
        self._threads = [
            threading.Thread(target=self._send_loop, daemon=True),
            threading.Thread(target=self._recv_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        self.on_change()

    def stop(self) -> None:
        was_running = self.running
        self.running = False
        if self._parec:
            self._parec.terminate()
            try:
                self._parec.wait(2)
            except subprocess.TimeoutExpired:
                self._parec.kill()
            self._parec = None
        if self._avahi:
            self._avahi.terminate()
            self._avahi = None
        for t in self._threads:
            t.join(1)
        self._threads = []
        if self._sock:
            for c in list(self.clients.values()):
                try:
                    self._sock.sendto(b"PSSTOP", c.addr)
                except OSError:
                    pass
            self._sock.close()
            self._sock = None
        with self._lock:
            self.clients.clear()
        try:
            if self._prev_sink and self._prev_sink != SINK_NAME:
                _pactl("set-default-sink", self._prev_sink)
                _move_all_to(self._prev_sink)
        except (OSError, subprocess.CalledProcessError):
            pass
        if self._module_id:
            subprocess.run(["pactl", "unload-module", self._module_id], capture_output=True)
            self._module_id = None
        self.cleanup_stale()
        if was_running:
            self.on_change()

    def _publish(self) -> None:
        if shutil.which("avahi-publish"):
            name = f"PhoneSpeaker on {socket.gethostname()}"
            self._avahi = subprocess.Popen(
                ["avahi-publish", "-s", name, SERVICE_TYPE, str(PORT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

    # ---------- network ----------
    def _recv_loop(self) -> None:
        last_sweep = 0.0
        while self.running and self._sock:
            if time.monotonic() - last_sweep > 1.0:
                last_sweep = time.monotonic()
                try:
                    _move_strays_to(SINK_NAME)
                except (OSError, subprocess.CalledProcessError):
                    pass
            try:
                data, addr = self._sock.recvfrom(64)
            except socket.timeout:
                data, addr = b"", None
            except OSError:
                break
            changed = False
            now = time.monotonic()
            with self._lock:
                if addr and data.startswith(b"PSHELLO"):
                    if addr not in self.clients:
                        changed = True
                        self.clients[addr] = Client(addr)
                    self.clients[addr].last_seen = now
                elif addr and data.startswith(b"PSBYE"):
                    changed = self.clients.pop(addr, None) is not None
                for a, c in list(self.clients.items()):
                    if now - c.last_seen > CLIENT_TIMEOUT:
                        del self.clients[a]
                        changed = True
            if changed:
                self.on_change()

    def _send_loop(self) -> None:
        seq = 0
        proc = self._parec
        assert proc and proc.stdout
        buf = bytearray()
        while self.running:
            chunk = proc.stdout.read(BYTES_PER_PACKET - len(buf))
            if not chunk:
                if self.running:
                    self.error = "Audio capture stopped unexpectedly"
                    threading.Thread(target=self.stop, daemon=True).start()
                return
            buf += chunk
            if len(buf) < BYTES_PER_PACKET:
                continue
            packet = MAGIC + struct.pack(">I", seq) + bytes(buf)
            buf.clear()
            seq = (seq + 1) & 0xFFFFFFFF
            with self._lock:
                targets = [c.addr for c in self.clients.values()]
            for addr in targets:
                try:
                    if self._sock:
                        self._sock.sendto(packet, addr)
                except OSError:
                    pass
