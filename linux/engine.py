"""PhoneSpeaker engine.

While the laptop app is open it listens on UDP port 47830 so iPhones can find it
and ask to join. When the first approved iPhone joins, it creates a virtual
PipeWire output "iPhone (PhoneSpeaker)", makes it the default (laptop speakers
go silent) and streams it. When the last iPhone leaves, sound returns to the
laptop.

Protocol (UDP, text fields separated by newlines):
  phone  -> laptop : PSDISCOVER                    (scan; sent to every address)
  laptop -> phone  : PSHERE\\n<laptop name>
  phone  -> laptop : PSJOIN\\n<device id>\\n<phone name>   (repeated until answered)
  laptop -> phone  : PSWAIT  (asking the user)  | PSOK | PSDENY
  phone  -> laptop : PSHELLO\\n<device id>       every second while joined
  phone  -> laptop : PSBYE                       when the phone disconnects
  laptop -> phone  : PSSTOP                      laptop ended the session
  laptop -> phone  : b"PSA1" + u32 sequence (big endian) + PCM
                     PCM = 48 kHz, stereo, signed 16-bit little endian,
                     240 frames (5 ms, 960 bytes) per packet.
                     Every packet is sent twice, the copy 10 ms later, so a
                     packet lost on Wi-Fi can be recovered from its copy.
"""

from __future__ import annotations

import json
import os
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
REDUNDANCY_DELAY = 2   # packets between a packet and its duplicate
DENY_COOLDOWN = 30.0
PENDING_TIMEOUT = 60.0
SERVICE_TYPE = "_phonespeaker._udp"
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "phonespeaker")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def laptop_name() -> str:
    try:
        name = subprocess.run(["hostnamectl", "--pretty"], capture_output=True, text=True).stdout.strip()
        if name:
            return name
    except OSError:
        pass
    return socket.gethostname()


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


def _move_all_to(sink: str) -> None:
    for line in _pactl("list", "short", "sink-inputs").splitlines():
        if line:
            subprocess.run(["pactl", "move-sink-input", line.split("\t")[0], sink], capture_output=True)


def _sink_index(name: str) -> str | None:
    for line in _pactl("list", "short", "sinks").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] == name:
            return parts[0]
    return None


def _move_strays_to(sink: str) -> bool:
    """Pull streams that remembered another output (e.g. speakers) onto `sink`.
    Returns False if `sink` no longer exists."""
    target = _sink_index(sink)
    if target is None:
        return False
    for line in _pactl("list", "short", "sink-inputs").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[1] != target:
            subprocess.run(["pactl", "move-sink-input", parts[0], sink], capture_output=True)
    return True


def cleanup_stale_sink() -> None:
    """Remove a virtual output left behind by a crash."""
    try:
        for mid in _null_sink_modules():
            subprocess.run(["pactl", "unload-module", mid], capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        pass


def local_ips() -> list[str]:
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
    device_id: str
    name: str
    last_seen: float = field(default_factory=time.monotonic)


@dataclass
class JoinRequest:
    device_id: str
    name: str
    addr: tuple[str, int]
    asked_at: float = field(default_factory=time.monotonic)


class Engine:
    """Callbacks run on background threads; the UI must hop to its own thread."""

    def __init__(self,
                 on_change: Callable[[], None] | None = None,
                 on_join_request: Callable[[JoinRequest], None] | None = None,
                 on_request_gone: Callable[[str], None] | None = None) -> None:
        self.on_change = on_change or (lambda: None)
        self.on_join_request = on_join_request or (lambda req: None)
        self.on_request_gone = on_request_gone or (lambda device_id: None)
        self.name = laptop_name()
        self.listening = False
        self.audio_on = False
        self.error: str | None = None
        self.clients: dict[tuple[str, int], Client] = {}
        self.pending: dict[str, JoinRequest] = {}
        self.trusted: dict[str, str] = {}          # device id -> phone name (remembered)
        self._session_allowed: set[str] = set()
        self._denied_until: dict[str, float] = {}
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._sock: socket.socket | None = None
        self._parec: subprocess.Popen | None = None
        self._avahi: subprocess.Popen | None = None
        self._prev_sink: str | None = None
        self._module_id: str | None = None
        self._threads: list[threading.Thread] = []
        self._audio_thread: threading.Thread | None = None
        self._load_config()

    # ---------- settings ----------
    def _load_config(self) -> None:
        try:
            with open(CONFIG_FILE) as f:
                self.trusted = dict(json.load(f).get("trusted", {}))
        except (OSError, ValueError):
            self.trusted = {}

    def _save_config(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"trusted": self.trusted}, f, indent=2)
        os.replace(tmp, CONFIG_FILE)

    # ---------- lifecycle ----------
    def listen(self) -> None:
        if self.listening:
            return
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", PORT))
            self._sock.settimeout(0.5)
        except OSError as exc:
            self.error = f"Can't open port {PORT}: {exc}"
            self._sock = None
            self.on_change()
            return
        cleanup_stale_sink()   # only now: we own the port, so no other copy is running
        if shutil.which("avahi-publish"):
            self._avahi = subprocess.Popen(
                ["avahi-publish", "-s", f"PhoneSpeaker on {self.name}", SERVICE_TYPE, str(PORT)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        self.listening = True
        self._threads = [
            threading.Thread(target=self._recv_loop, daemon=True),
            threading.Thread(target=self._housekeeping_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        self.on_change()

    def shutdown(self) -> None:
        with self._lock:
            for c in self.clients.values():
                self._send(b"PSSTOP", c.addr)
            self.clients.clear()
        self.listening = False
        self._wake.set()
        for t in self._threads:
            t.join(2)
        self._threads = []
        self._stop_audio()
        if self._avahi:
            self._avahi.terminate()
            self._avahi = None
        if self._sock:
            self._sock.close()
            self._sock = None

    # ---------- user actions (called from the UI) ----------
    def answer(self, device_id: str, allow: bool, remember: bool) -> None:
        with self._lock:
            req = self.pending.pop(device_id, None)
            if not req:
                return
            if allow:
                self._session_allowed.add(device_id)
                if remember:
                    self.trusted[device_id] = req.name
                    self._save_config()
                self.clients[req.addr] = Client(req.addr, device_id, req.name)
                self._send(b"PSOK", req.addr)
            else:
                self._denied_until[device_id] = time.monotonic() + DENY_COOLDOWN
                self._send(b"PSDENY", req.addr)
        self._wake.set()
        self.on_change()

    def kick(self, addr: tuple[str, int]) -> None:
        with self._lock:
            c = self.clients.pop(addr, None)
            if c:
                self._session_allowed.discard(c.device_id)
                self._send(b"PSSTOP", addr)
        self._wake.set()
        self.on_change()

    def forget(self, device_id: str) -> None:
        with self._lock:
            self.trusted.pop(device_id, None)
            self._session_allowed.discard(device_id)
            self._save_config()
        self.on_change()

    # ---------- network ----------
    def _send(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            if self._sock:
                self._sock.sendto(data, addr)
        except OSError:
            pass

    def _allowed(self, device_id: str) -> bool:
        return device_id in self.trusted or device_id in self._session_allowed

    def _recv_loop(self) -> None:
        while self.listening and self._sock:
            try:
                data, addr = self._sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                self._handle(data, addr)
            except Exception:  # noqa: BLE001 - never let one bad packet kill the listener
                pass

    def _handle(self, data: bytes, addr: tuple[str, int]) -> None:
        fields = data.decode("utf-8", "replace").split("\n")
        kind = fields[0]
        changed = False
        new_request: JoinRequest | None = None
        with self._lock:
            if kind == "PSDISCOVER":
                self._send(f"PSHERE\n{self.name}".encode(), addr)
            elif kind == "PSJOIN" and len(fields) >= 3:
                device_id, name = fields[1][:64], fields[2][:64] or "iPhone"
                if self._allowed(device_id):
                    if addr not in self.clients:
                        self.clients[addr] = Client(addr, device_id, name)
                        changed = True
                    self.clients[addr].last_seen = time.monotonic()
                    self._send(b"PSOK", addr)
                elif self._denied_until.get(device_id, 0) > time.monotonic():
                    self._send(b"PSDENY", addr)
                else:
                    req = self.pending.get(device_id)
                    if req:
                        req.addr = addr
                    else:
                        new_request = self.pending[device_id] = JoinRequest(device_id, name, addr)
                        changed = True
                    self._send(b"PSWAIT", addr)
            elif kind == "PSHELLO":
                device_id = fields[1] if len(fields) > 1 else ""
                if addr in self.clients:
                    self.clients[addr].last_seen = time.monotonic()
                elif device_id and self._allowed(device_id):
                    # Same phone, new address (e.g. Wi-Fi reconnect).
                    name = self.trusted.get(device_id, "iPhone")
                    for a, c in list(self.clients.items()):
                        if c.device_id == device_id:
                            name = c.name
                            del self.clients[a]
                    self.clients[addr] = Client(addr, device_id, name)
                    changed = True
                else:
                    self._send(b"PSDENY", addr)
            elif kind == "PSBYE":
                c = self.clients.pop(addr, None)
                if c:
                    changed = True
                for did, req in list(self.pending.items()):
                    if req.addr == addr:
                        del self.pending[did]
                        self.on_request_gone(did)
                        changed = True
        if new_request:
            self.on_join_request(new_request)
        if changed:
            self._wake.set()
            self.on_change()

    def _housekeeping_loop(self) -> None:
        last_sweep = 0.0
        empty_since: float | None = None
        while self.listening:
            self._wake.wait(0.5)
            self._wake.clear()
            if not self.listening:
                break
            now = time.monotonic()
            changed = False
            gone: list[str] = []
            with self._lock:
                for a, c in list(self.clients.items()):
                    if now - c.last_seen > CLIENT_TIMEOUT:
                        del self.clients[a]
                        changed = True
                for did, req in list(self.pending.items()):
                    if now - req.asked_at > PENDING_TIMEOUT:
                        del self.pending[did]
                        gone.append(did)
                        changed = True
                have_clients = bool(self.clients)
            for did in gone:
                self.on_request_gone(did)

            if self.audio_on and self._parec and self._parec.poll() is not None:
                self._stop_audio()          # capture died: restart it below
                changed = True
            if have_clients:
                empty_since = None
                if not self.audio_on:
                    self._start_audio()
                    changed = True
            elif self.audio_on:
                # Small grace period so a quick reconnect doesn't flip speakers on/off.
                empty_since = empty_since or now
                if now - empty_since > 2.0:
                    self._stop_audio()
                    empty_since = None
                    changed = True

            if self.audio_on and now - last_sweep > 1.0:
                last_sweep = now
                try:
                    if not _move_strays_to(SINK_NAME):
                        # Our output vanished (e.g. PipeWire restarted): rebuild it.
                        self._stop_audio()
                        self._start_audio()
                        changed = True
                except (OSError, subprocess.CalledProcessError):
                    pass
            if changed:
                self.on_change()

    # ---------- audio ----------
    def _start_audio(self) -> None:
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
            self._parec = subprocess.Popen(
                ["parec", "-d", f"{SINK_NAME}.monitor", "--raw",
                 "--format=s16le", f"--rate={RATE}", f"--channels={CHANNELS}",
                 "--latency-msec=5", "--client-name=PhoneSpeaker"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            )
        except Exception as exc:  # noqa: BLE001 - surface any setup failure to the UI
            self.error = f"Couldn't set up audio: {exc}"
            self._stop_audio()
            return
        self.audio_on = True
        self._audio_thread = threading.Thread(target=self._send_loop, args=(self._parec,), daemon=True)
        self._audio_thread.start()

    def _stop_audio(self) -> None:
        self.audio_on = False
        if self._parec:
            self._parec.terminate()
            try:
                self._parec.wait(2)
            except subprocess.TimeoutExpired:
                self._parec.kill()
            self._parec = None
        if self._audio_thread:
            self._audio_thread.join(1)
            self._audio_thread = None
        try:
            if self._prev_sink and self._prev_sink != SINK_NAME:
                _pactl("set-default-sink", self._prev_sink)
                _move_all_to(self._prev_sink)
        except (OSError, subprocess.CalledProcessError):
            pass
        self._prev_sink = None
        if self._module_id:
            subprocess.run(["pactl", "unload-module", self._module_id], capture_output=True)
            self._module_id = None
        cleanup_stale_sink()

    def _send_loop(self, proc: subprocess.Popen) -> None:
        seq = 0
        assert proc.stdout
        buf = bytearray()
        history: list[bytes] = []
        while self.audio_on:
            chunk = proc.stdout.read(BYTES_PER_PACKET - len(buf))
            if not chunk:
                if self.audio_on:
                    self.error = "Audio capture stopped unexpectedly"
                    self.on_change()
                return
            buf += chunk
            if len(buf) < BYTES_PER_PACKET:
                continue
            packet = MAGIC + struct.pack(">I", seq) + bytes(buf)
            buf.clear()
            seq = (seq + 1) & 0xFFFFFFFF
            # Send each packet twice: now, and again two packets (10 ms) later.
            # If Wi-Fi drops one copy, the iPhone uses the other.
            history.append(packet)
            resend = history.pop(0) if len(history) > REDUNDANCY_DELAY else None
            with self._lock:
                targets = [c.addr for c in self.clients.values()]
            for addr in targets:
                self._send(packet, addr)
                if resend:
                    self._send(resend, addr)
