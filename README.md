# PhoneSpeaker

Play your laptop's sound on your iPhone. While it's on, the laptop speakers stay silent.

## Laptop (CachyOS / any PipeWire Linux)

    ./linux/install.sh

Open **PhoneSpeaker** from the app menu and press **Send sound to iPhone**.
Press **Stop** (or close the window) to get sound back on the laptop.

## iPhone

1. Download `PhoneSpeaker.ipa` from this repo's **Releases → latest** page
   (built automatically by GitHub Actions on every push).
2. Sideload it with SideStore or AltStore.
3. Open it, allow **Local Network** access, and tap your laptop in the list
   (or type the address shown in the laptop window).

Both devices must be on the same Wi-Fi.

## How it works

The laptop app creates a virtual output called "iPhone (PhoneSpeaker)", makes it
the default, and streams it as uncompressed 48 kHz stereo PCM over UDP port 47830
(about 1.5 Mbit/s). The iPhone app buffers 30–300 ms (adjustable) and plays it,
including in the background and with the screen locked.

`tools/fake_phone.py` pretends to be the iPhone for testing on the laptop.
