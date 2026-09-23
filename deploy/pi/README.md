# Raspberry Pi appliance

Run OpenHomeKaraoke as a standalone box: plug it into the TV, power it on, and the
karaoke screen comes up by itself. No MacBook in the loop, no keyboard needed — guests
scan the QR code and drive everything from their phones.

**Status: drafted, not yet tested on real hardware.** Every step below is derived from
how this codebase actually behaves (see *Why these choices* at the end), but nobody has
run it on a Pi yet. Expect to fix a package name or two on the first pass.

## Hardware

| Part | Recommendation | Note |
|---|---|---|
| Board | **Pi 5, 4GB** | 8GB only helps if you also run the vocal splitter |
| Storage | NVMe HAT + 256GB SSD, or a good A2 microSD | Songs are ~50–150MB each; SD cards die under constant writes |
| Power | Official 27W USB-C PSU | Underpowering a Pi 5 causes USB/HDMI flakiness |
| Cooling | Active cooler | The vocal splitter pins all 4 cores for long stretches |
| Audio | HDMI to the TV, or a USB DAC/interface for a mixer | Pi 5 has no analogue jack |

Roughly $120–190 all in.

## OS image

Use **Raspberry Pi OS Bookworm, 64-bit, Desktop** (not Lite, not Trixie):

- **64-bit** — PyTorch publishes no wheels for 32-bit ARM, so the vocal splitter cannot
  install at all on the 32-bit image.
- **Bookworm** ships Python 3.11. `requirements.txt` pins `flask==2.3.1`, which does not
  import on Python 3.13 (Trixie's version) — the same trap `openkaraoke` warns about on
  macOS. Bookworm avoids it without touching the pins.
- **Desktop, not Lite** — the app needs a graphical session: pygame renders the splash
  screen and VLC plays onto it.

## Install

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/sidalay/OpenHomeKaraokeMacOS.git ~/OpenHomeKaraoke
cd ~/OpenHomeKaraoke
./deploy/pi/setup-pi.sh
```

Then set the two things the script deliberately leaves to you, because they change how
the Pi boots:

```bash
sudo raspi-config
```

- `Advanced Options` → `Wayland` → **X11**
- `System Options` → `Boot / Auto Login` → **Desktop Autologin**

```bash
sudo reboot
```

The TV should show the splash screen with the QR code after boot.

### Day-to-day

```bash
systemctl --user status openhomekaraoke      # running?
journalctl --user -u openhomekaraoke -f      # logs
tmux attach -t PiKaraoke                     # the app's own panes (Ctrl-B D to detach)
systemctl --user restart openhomekaraoke
```

## What works, and what to expect

**Fine on a Pi 5:** 1080p playback, the web UI, queueing, YouTube downloads, pitch/tempo
shifting, the QR splash screen, and streaming the screen to a smart-TV browser.

**Slower: DNN vocal splitting.** This is the one real compromise. The splitter is
GPU-accelerated on a Mac (Metal/MPS) and there is no GPU path on a Pi, so it falls back to
4 CPU cores. Measured on an M5 Max, a 3m09s song takes 5.1s on the GPU and 16.7s on the
CPU, and 78% of the CPU time is the neural net itself. Scaling that to a Pi 5's four
Cortex-A76 cores gives an *estimate* of roughly 2–5 minutes per song, about realtime:
fine for a song queued a couple of places back, too slow to backfill a big library in
one evening. Three ways to live with it:

1. **Let it run overnight.** It is a background queue, not something you wait on. A
   50-song library would take a few hours once; after that only new songs need it.
2. **Pre-split on the MacBook** and copy `nonvocal/` and `vocal/` into the Pi's song
   folder. The splitter skips anything already present.
3. **Turn DNN splitting off** in the web UI and use the classic stereo-subtraction mode,
   which is nearly free. Quality is worse, but it is instant.

Splitting cannot simply be pointed at another machine: `vocal_splitter.py` talks to
`http://localhost:5000` and reads and writes files by local path, so it has to run beside
the song library (or over a shared mount).

**Unchanged: voice search needs a GPU server either way.** `run_asr` posts to the
`--cloud` endpoint; Whisper never runs locally, on the Pi or on the Mac. If you don't
pass `--cloud`, voice search is simply unavailable — same as today.

## Why these choices

Notes for whoever changes this later, from reading the code rather than guessing:

- **The service runs `run.sh`, not `app.py`.** `Karaoke.streamer_restart` and
  `streamer_stop` drive the screen streamer by sending keys to **tmux pane
  `PiKaraoke:0.3`** (`karaoke.py`). Launch `app.py` on its own and the web UI's streamer
  buttons silently do nothing, because that pane does not exist. `run.sh` builds the
  layout those calls assume.
- **`OHK_NO_ATTACH`** was added to `run.sh` for this: it otherwise ends with
  `tmux a -t PiKaraoke`, which cannot work from a systemd unit with no terminal.
- **X11 rather than Wayland.** VLC is launched with `--video-on-top` and
  `--no-embedded-video` so it floats above the pygame splash; that hint is an X11 one and
  is not honoured by labwc/wayfire. `screencapture.sh` also uses `ffmpeg -f x11grab` plus
  `xwininfo` on Linux, so TV streaming needs X11 too.
- **A user unit, not a system one.** It needs `DISPLAY` and the logged-in graphical
  session; `enable-linger` lets it come up without an interactive login.
- **`nonvocal/` and `vocal/` must exist.** `get_next_file` in `vocal_splitter.py` returns
  nothing at all unless at least one of them is present — that is the on/off switch.

## If you would rather not deal with any of this

A used Apple Silicon Mac mini runs the code you already have, with GPU (Metal/MPS) vocal
splitting, and needs none of the X11/Wayland or PyTorch-wheel care. See
[deploy/macos/](../macos/) for a from-scratch Mac mini setup, including autostart.
