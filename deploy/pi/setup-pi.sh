#!/usr/bin/env bash
#
# Provision a Raspberry Pi 5 as a standalone OpenHomeKaraoke appliance.
#
#   ./deploy/pi/setup-pi.sh
#
# Idempotent: safe to re-run after a failure or to pick up new requirements.
# Read deploy/pi/README.md first — it covers the OS image and the two raspi-config
# settings (X11 and desktop autologin) that this script does NOT change for you.

set -euo pipefail

PROJECT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SONGS_DIR="${SONGS_DIR:-$HOME/pikaraoke-songs}"
cd "$PROJECT_DIR"

say() { printf '\n\033[1m*** %s\033[0m\n' "$1"; }

# ---------------------------------------------------------------- sanity checks
if [ "$(uname -m)" != "aarch64" ]; then
	echo "setup-pi.sh: expected a 64-bit Raspberry Pi OS (aarch64), got $(uname -m)." >&2
	echo "The 32-bit image has no PyTorch wheels; reflash with the 64-bit image." >&2
	exit 1
fi

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
if [ "$PYVER" != "3.11" ]; then
	echo "setup-pi.sh: warning: Python $PYVER detected." >&2
	echo "  requirements.txt pins flask==2.3.1, which does not import on Python 3.13+." >&2
	echo "  Raspberry Pi OS Bookworm (Python 3.11) is the tested combination." >&2
	echo "  Continuing in 5s; Ctrl-C to abort." >&2
	sleep 5
fi

# ------------------------------------------------------------- system packages
say "Installing system packages"
sudo apt-get update
sudo apt-get install -y \
	vlc ffmpeg tmux socat git \
	python3-venv python3-dev python3-pip \
	libsdl2-ttf-dev libjpeg-dev libsndfile1 \
	x11-utils fonts-noto-cjk
# x11-utils  -> xwininfo, used by screencapture.sh to size the capture
# socat      -> serves the HTTP stream in screencapture.sh
# fonts-noto-cjk -> non-Latin song titles on the splash screen

# ---------------------------------------------------------------------- python
say "Creating the virtualenv"
if [ ! -x .venv/bin/python ]; then
	python3 -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip wheel

say "Installing PyTorch (CPU build)"
# The Pi has no CUDA and no Metal, so the vocal splitter runs on the CPU. Installing
# from the CPU index avoids pulling the ~2GB CUDA wheels on architectures that have them.
.venv/bin/python -m pip install --index-url https://download.pytorch.org/whl/cpu torch

say "Installing Python requirements"
.venv/bin/python -m pip install -r requirements.txt

# ----------------------------------------------------------------- song folders
say "Creating song folders under $SONGS_DIR"
mkdir -p "$SONGS_DIR/nonvocal" "$SONGS_DIR/vocal"
# The vocal splitter only processes a library when these two subfolders exist
# (see get_next_file in vocal_splitter.py). Delete one to disable that half.

# --------------------------------------------------------------------- service
say "Installing the systemd user service"
mkdir -p "$HOME/.config/systemd/user"
sed "s#%h/OpenHomeKaraoke#$PROJECT_DIR#g" deploy/pi/openhomekaraoke.service \
	> "$HOME/.config/systemd/user/openhomekaraoke.service"
systemctl --user daemon-reload
systemctl --user enable openhomekaraoke.service

# Let the user service survive logout / start before an interactive login.
sudo loginctl enable-linger "$USER"

say "Done"
cat <<EOF

Next steps (not done automatically, they change how the Pi boots):

  sudo raspi-config
    Advanced Options > Wayland  > X11          # VLC's --video-on-top and the screen
                                               # streamer both need X11
    System Options   > Boot/Auto Login > Desktop Autologin

  sudo reboot

After the reboot the karaoke screen should come up on the TV by itself.

  systemctl --user status openhomekaraoke     # is it running
  journalctl --user -u openhomekaraoke -f     # logs
  tmux attach -t PiKaraoke                    # the app's own panes
  systemctl --user restart openhomekaraoke    # restart

Songs live in $SONGS_DIR .
EOF
