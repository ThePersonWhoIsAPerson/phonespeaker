# PhoneSpeaker

Play your laptop's sound on your iPhone. While it's on, the laptop speakers stay silent.

## Laptop (CachyOS / any PipeWire Linux)

    ./linux/install.sh

Open **PhoneSpeaker** from the app menu. While it runs (closing the window keeps
it running in the background; quit from the ☰ menu), iPhones on the same network
can find the laptop and ask to join. You get an **Allow / Deny** prompt; tick
**Always allow** to skip it next time. Sound moves to the iPhone when it joins and
comes back to the laptop when it disconnects. Turn on **Start when I log in** so
it's always ready.

## iPhone

1. Download `PhoneSpeaker.ipa` from this repo's **Releases → latest** page
   (built automatically by GitHub Actions on every push).
2. Sideload it with SideStore or AltStore.
3. Open it and allow **Local Network** access. Laptops running PhoneSpeaker appear
   in **Nearby laptops** on their own. Tap one, then accept on the laptop.

Both devices must be on the same Wi-Fi, or the laptop on the iPhone's Personal
Hotspot. The iPhone finds laptops with Bonjour and by asking every address on its
local networks, so it works on hotspots where Bonjour doesn't.

## How it works

The laptop app creates a virtual output called "iPhone (PhoneSpeaker)", makes it
the default, and streams it as uncompressed 48 kHz stereo PCM over UDP port 47830
(about 1.5 Mbit/s). The iPhone app buffers 30–300 ms (adjustable) and plays it,
including in the background and with the screen locked.

`tools/fake_phone.py` pretends to be the iPhone for testing on the laptop.
