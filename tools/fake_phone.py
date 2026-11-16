#!/usr/bin/env python3
"""Pretends to be the iPhone: says hello, receives the stream and reports stats.
With --play it also plays the received audio through the given real sink."""
import argparse, socket, struct, subprocess, time
p = argparse.ArgumentParser()
p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=47830)
p.add_argument("--seconds", type=float, default=5); p.add_argument("--play", metavar="SINK")
a = p.parse_args()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(0.2)
player = subprocess.Popen(["pacat", "-d", a.play, "--raw", "--format=s16le", "--rate=48000",
                           "--channels=2", "--latency-msec=60"], stdin=subprocess.PIPE) if a.play else None
start = last_hello = time.monotonic() - 2
pkts = lost = 0; prev = None; peak = 0
while time.monotonic() - start < a.seconds + 2:
    if time.monotonic() - last_hello > 1:
        s.sendto(b"PSHELLO", (a.host, a.port)); last_hello = time.monotonic()
    try: data, _ = s.recvfrom(2048)
    except socket.timeout: continue
    if data[:4] != b"PSA1": print("non-audio packet:", data[:8]); continue
    seq = struct.unpack(">I", data[4:8])[0]; pcm = data[8:]
    if prev is not None and seq != prev + 1: lost += seq - prev - 1
    prev = seq; pkts += 1
    peak = max(peak, max(abs(v) for v in struct.unpack(f"<{len(pcm)//2}h", pcm)))
    if player: player.stdin.write(pcm)
s.sendto(b"PSBYE", (a.host, a.port))
print(f"packets={pkts} (~{pkts*5/1000:.1f}s audio) lost={lost} size={len(pcm) if pkts else 0}B peak={peak}")
