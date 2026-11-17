#!/usr/bin/env python3
"""Pretends to be the iPhone: scans for the laptop, asks to join, receives audio
and reports stats. With --play it also plays the audio through the given sink."""
import argparse, random, socket, struct, subprocess, time
p = argparse.ArgumentParser()
p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=47830)
p.add_argument("--seconds", type=float, default=5); p.add_argument("--play", metavar="SINK")
p.add_argument("--drop", type=float, default=0.0, help="fraction of received packets to throw away")
p.add_argument("--id", default="fake-phone-1"); p.add_argument("--name", default="Test iPhone")
a = p.parse_args()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(0.2)
dst = (a.host, a.port)
s.sendto(b"PSDISCOVER", dst)
try: print("discover ->", s.recvfrom(512)[0])
except socket.timeout: print("discover -> no reply")
player = subprocess.Popen(["pacat", "-d", a.play, "--raw", "--format=s16le", "--rate=48000",
                           "--channels=2", "--latency-msec=60"], stdin=subprocess.PIPE) if a.play else None
joined = False; replies = []
start = last = time.monotonic() - 2
pkts = dropped = 0; seen = set(); peak = 0
while time.monotonic() - start < a.seconds + 2:
    if time.monotonic() - last > 1:
        msg = f"PSHELLO\n{a.id}" if joined else f"PSJOIN\n{a.id}\n{a.name}"
        s.sendto(msg.encode(), dst); last = time.monotonic()
    try: data, _ = s.recvfrom(2048)
    except socket.timeout: continue
    if data[:4] != b"PSA1":
        if not replies or replies[-1] != data: replies.append(data)
        if data == b"PSOK": joined = True
        if data in (b"PSDENY", b"PSSTOP"): break
        continue
    if random.random() < a.drop: dropped += 1; continue
    seq = struct.unpack(">I", data[4:8])[0]; pcm = data[8:]
    pkts += 1
    if seq in seen: continue
    seen.add(seq)
    peak = max(peak, max(abs(v) for v in struct.unpack(f"<{len(pcm)//2}h", pcm)))
    if player: player.stdin.write(pcm)
s.sendto(b"PSBYE", dst)
lost = (max(seen) - min(seen) + 1 - len(seen)) if seen else 0
print(f"replies={[r.decode() for r in replies]} received={pkts} thrown_away={dropped} unique={len(seen)} (~{len(seen)*5/1000:.1f}s) unrecoverable={lost} peak={peak}")
