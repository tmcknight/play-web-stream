#!/usr/bin/env bash
# Install the web app on this machine. Run it from the checkout on the machine that
# will serve the streams (see "Put it on the right machine" in the README).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Chromium's shared libraries need root; nothing else does.
.venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium

# The unit makes this the one writable path on a read-only filesystem, and it will
# not start if a ReadWritePaths= entry is missing, so create it first.
mkdir -p "$HOME/.config/play-web-stream"
# 700 to match what airplay.py creates. makedirs(exist_ok=True) will not tighten an
# existing directory.
chmod 700 "$HOME/.config/play-web-stream"

# __HOME__ instead of systemd's %h, which in a system unit expands to root's home
# whatever User= says. The credentials live in the user's home.
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$DIR|g" -e "s|__HOME__|$HOME|g" \
    deploy/play-web-stream.service \
  | sudo tee /etc/systemd/system/play-web-stream.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now play-web-stream

echo
echo "up at http://$(hostname -I | awk '{print $1}'):8786/"
echo "proxies take 8787 and up; open 8786-8806/tcp to the LAN only."
