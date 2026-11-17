#!/usr/bin/env bash
# Installs PhoneSpeaker for the current user (no root needed).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
dest="$HOME/.local/share/phonespeaker"
mkdir -p "$dest" "$HOME/.local/bin" "$HOME/.local/share/applications"
install -m644 "$here/engine.py" "$dest/engine.py"
install -m755 "$here/phonespeaker.py" "$dest/phonespeaker.py"
ln -sf "$dest/phonespeaker.py" "$HOME/.local/bin/phonespeaker"
sed "s|@EXEC@|$dest/phonespeaker.py|" "$here/phonespeaker.desktop" \
  > "$HOME/.local/share/applications/io.github.phonespeaker.PhoneSpeaker.desktop"
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
# Needed packages (already present on most CachyOS installs)
missing=()
for p in python-gobject gtk4 libadwaita libpulse avahi; do pacman -Q "$p" &>/dev/null || missing+=("$p"); done
if ((${#missing[@]})); then echo "Install these first: sudo pacman -S ${missing[*]}"; fi
if systemctl is-active --quiet ufw; then
  echo "Your firewall (ufw) is on. Let iPhones reach PhoneSpeaker with:"
  echo "  sudo ufw allow 47830/udp comment PhoneSpeaker"
elif systemctl is-active --quiet firewalld; then
  echo "Your firewall (firewalld) is on. Let iPhones reach PhoneSpeaker with:"
  echo "  sudo firewall-cmd --permanent --add-port=47830/udp && sudo firewall-cmd --reload"
fi
echo "Installed. Find 'PhoneSpeaker' in your app menu."
