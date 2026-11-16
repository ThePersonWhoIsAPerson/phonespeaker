#!/usr/bin/env bash
set -eu
rm -rf "$HOME/.local/share/phonespeaker" "$HOME/.local/bin/phonespeaker" \
  "$HOME/.local/share/applications/io.github.phonespeaker.PhoneSpeaker.desktop"
echo "PhoneSpeaker removed."
