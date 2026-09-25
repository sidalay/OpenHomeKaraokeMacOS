# Audio mixer prototype

A standalone test of "option 2": VLC shows only the video, and our own audio engine
plays the song. The engine mixes the instrumental and vocal tracks live, so the vocal
level is a **continuous slider with no gaps**. Key and speed are live too, and a sync
loop keeps the audio locked to VLC's video.

**This is now part of the app** as `lib/audio_engine.py` (with the original recording at the
slider's centre, and the Vocals slider on the Home screen). This folder is kept as the
standalone test bench it was developed and measured with.

## Try it

Stop OpenHomeKaraoke first (`exitkaraoke`), since the prototype starts its own VLC. Then:

```bash
cd ~/Dev/OpenHomeKaraoke/OpenHomeKaraoke
.venv/bin/python prototype/audio_mixer/proto.py "$HOME/pikaraoke-songs/Laufey - From The Start (Lyrics)---rzwiGUh_rw8.mp4"
```

Open **http://localhost:5055**, or `http://<this Mac's IP>:5055` from your phone. The
VLC window shows the video paused; press **Play** on the page. Ctrl-C quits.

Any song with DNN split tracks works (`nonvocal/` and `vocal/` next to it). Pick one
whose video has singing: a video titled "Karaoke Version" is usually instrumental
already, so its vocal track is silent. The prototype warns about that when it starts.

Things worth trying by ear:

- **Vocals slider:** drag it slowly and quickly, end to end. There should never be a
  gap or a click.
- **Key and Speed:** change them while singing along.
- **Sync:** watch the lyrics against the music. If the audio seems early or late,
  move **Audio delay** until it feels right, and note the value. A consistent offset is
  easy to build in.

`--selftest` runs a silent, scripted version and prints the sync numbers below.
`--device` picks an audio output other than the default.

## Requirements

- `sounddevice` for audio output. It's installed in this Mac's `.venv`. Elsewhere:
  `pip install sounddevice`, and on a Raspberry Pi also `sudo apt install libportaudio2`.
- Nothing else new: the pitch/tempo processing is plain numpy.

## Measured

`test_dsp.py` (offline, synthetic tones, all pass):

| Check | Result |
|---|---|
| Key 0 / speed 1 vs the plain mix | identical to within float rounding (−141 dB) |
| Key +3, −4, +7 | within 0.06 cents of the exact pitch, tempo unchanged |
| Speed ×0.8, ×1.25, key +2 with ×1.1 | pitch within 0.14 cents, song advances at exactly the set speed |
| Aliasing (17 kHz tone at +7) | suppressed by 72 dB |
| Vocals slider swept end to end | no clicks: largest sample step equals a clean mix's |
| CPU on an M5 Max | 0.1% (key 0), 2.2% (speed ×1.2), 5.9% (key +3) of one core |

`proto.py --selftest`, 3 runs on two songs (Laufey AV1, Madonna VP9). Audio compared
with VLC's video clock after each event; ranges cover all runs:

| Phase | Median | Worst | In sync within 20 ms after |
|---|---|---|---|
| Start | 2.5–9.2 ms | 16 ms | 0.7–1.2 s |
| While moving the vocals slider / key | 0.3–29.7 ms | 32 ms | immediately |
| Pause 2 s, resume | 2.8–7.0 ms | 19 ms | 0.7–2.0 s |
| Seek to 1:30 | 2.8–14.5 ms | 40 ms | 2.2–2.6 s |
| Speed ×1.2, then back to ×1.0 | 3.5–10 ms | 21 ms | 0.6–2.0 s |

The spread is mostly the VLC clock estimate's own noise (±10–30 ms); the vocals
slider doesn't touch timing at all. For comparison, audio and video feel in sync
within roughly ±40 ms.

Audio dropouts: **0**. The only silences in the output were the song's own intro
and the deliberate 2 s pause.

## How it works

`engine.py`: both tracks are decoded into memory. For every 30 ms of output, the mix
is read at the pitch ratio (a 32-tap windowed-sinc interpolator, which also keeps
key-up shifts from aliasing). Then WSOLA, the method VLC's pitch filter uses, restores
the tempo by overlapping short slices at their best-matching offsets. At key 0 and
speed 1 it reduces to the plain mix. Each output block records when it will reach the
speakers, so the engine always knows which song position is being heard.

`proto.py`: VLC runs with `--no-audio` and a small extra page (`vlc_clock.json`)
that reports its clock in microseconds. The sync loop compares that with the engine
and trims the engine's tempo by up to 2%, which doesn't change the pitch, so it's
inaudible. Differences over 150 ms (40 ms just after a seek, resume or speed change)
jump instead, with a short fade.

## Findings along the way

- **Spotify's pedalboard pitch shifter** holds back ~1.04 s of audio when streaming,
  so every key change would be heard a second late. That's why the pitch/tempo code
  is our own.
- **VLC only refreshes its clock every ~250 ms**, so one reading can be up to half a
  second stale. Each refresh is exact when it happens, though, so over a 2 s window
  the reading furthest ahead of the wall clock is the freshest (about ±10 ms).
- **VLC's speed setting isn't exact:** ×1.2 runs at about ×1.2033, and ×0.8 at
  ×0.7981. The sync loop measures the real speed and learns any remaining difference.
- **VLC's macOS window ignores `--start-paused`**, so the prototype pauses it again
  itself until you press Play.
- This Mac's router returned SERVFAIL for `files.pythonhosted.org`, so `pip install`
  of new packages failed. The wheels were fetched through Cloudflare DNS and
  checked against PyPI's SHA-256.

## Not done yet: what integration would take

- **Player:** VLC gets `--no-audio`, and one engine per song replaces the combined-file
  track switching. Pause, seek, volume, audio delay, key and speed are routed to the
  engine. Songs without split tracks play their original audio through the engine,
  with the slider disabled.
- **Web UI:** the vocals slider replaces the Music/Mixed/Voice switch (saved per song
  like the other play settings).
- **Volume normalization** and **stereo (non-DNN) mode** need adapting.
- **TV streaming** (`screencapture.sh`) captures the system audio, so it should keep
  working, but that's untested.
- **Memory:** a 4-minute song is about 85 MB per track in memory (float32). That's fine
  on a Mac mini. A Pi might want int16.
- **Raspberry Pi:** untested. CPU is low on a Mac; a Pi is several times slower, and
  the filter could be made cheaper there if needed.
