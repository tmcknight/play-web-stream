#!/usr/bin/env bash
# Install the web app on this machine. Run it on the box that will serve the
# streams, from the checkout -- see "Put it on the right machine" in the README.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Chromium's own shared libraries need root; everything else does not.
.venv/bin/playwright install chromium
sudo .venv/bin/playwright install-deps chromium

# The unit holds this writable against an otherwise read-only filesystem, and a
# ReadWritePaths= that names nothing refuses to start -- so it exists before then.
mkdir -p "$HOME/.config/play-web-stream"
# 700, because airplay.py creates it that way when it gets there first and
# makedirs(exist_ok=True) will not tighten a directory that already exists.
chmod 700 "$HOME/.config/play-web-stream"

# __HOME__ rather than systemd's %h: in a system unit that expands to root's home
# whatever User= says, which is not where this app keeps its credentials.
sed -e "s|__USER__|$USER|g" -e "s|__DIR__|$DIR|g" -e "s|__HOME__|$HOME|g" \
    deploy/play-web-stream.service \
  | sudo tee /etc/systemd/system/play-web-stream.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now play-web-stream

echo
echo "up at http://$(hostname -I | awk '{print $1}'):8786/"
echo "proxies take 8787 and up; open 8786-8806/tcp to the LAN only."
