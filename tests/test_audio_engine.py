"""Offline checks of lib/audio_engine.py (no sound is played).

    .venv/bin/python tests/test_audio_engine.py
"""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from lib import audio_engine as A
SR = A.SR
ok = True

def check(name, cond, detail):
	global ok
	ok &= bool(cond)
	print(f"{'PASS' if cond else 'FAIL'}  {name}: {detail}")

def tone(f, sec, amp = 0.4):
	t = np.arange(int(SR * sec)) / SR
	return np.stack([amp * np.sin(2 * np.pi * f * t)] * 2, 1).astype(np.float32)

def engine(original, inst = None, voc = None):
	e = A.Engine(original, device = False)
	if inst is not None:
		e.add_split(inst, voc)
	return e

def peak_hz(x):
	x = x[:, 0]; n = 1 << 18
	spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), n))
	k = int(np.argmax(spec)); a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
	return (k + 0.5 * (a - c) / (a - 2 * b + c)) * SR / n

def level(x, f):
	return 2 * np.abs(np.fft.rfft(x[:, 0] * np.hanning(len(x)))[int(round(f * len(x) / SR))]) / (len(x) / 2)

cents = lambda f, ref: 1200 * np.log2(f / ref)

# Stand-ins for a song: instrumental 440 Hz, vocal 660 Hz; the "original" is their sum
# plus a 550 Hz tone the split tracks lack, so it is identifiable in the output.
inst, voc = tone(440, 8), tone(660, 8, 0.3)
orig = inst + voc + tone(550, 8, 0.2)

# 1. positions of the vocal slider
e = engine(orig, inst, voc)
for blend, want, name in ((0, orig, 'centre plays the original recording'),
                          (-1, inst, 'far left plays the instrumental only'),
                          (1, voc, 'far right plays the vocals only')):
	e.blend = blend; e.coefs[:] = A.mix_coefficients(blend, True); e._reset_dsp(1.0)
	y = e.render(3); err = np.abs(y - want[SR:SR + len(y)]).max()
	check(name, err < 1e-5, f"max difference {err:.1e}")
e.blend = -0.5; e.coefs[:] = A.mix_coefficients(-0.5, True); e._reset_dsp(1.0); y = e.render(3)
check("halfway left: instrumental + half the vocals, no original",
      abs(level(y, 440) - 0.4) < 0.01 and abs(level(y, 660) - 0.15) < 0.01 and level(y, 550) < 0.005,
      f"440 Hz {level(y, 440):.3f} (want 0.40), 660 Hz {level(y, 660):.3f} (want 0.15), 550 Hz {level(y, 550):.3f} (want 0)")

# 2. no split tracks yet: the original plays whatever the slider says
e = engine(orig); e.blend = 1.0; y = e.render(3)
check("without split tracks the slider is ignored", np.abs(y - orig[:len(y)]).max() < 1e-5, "plays the original")

# 3. sweeping the whole slider (through the centre crossfade) never clicks
e = engine(orig, inst, voc); e.blend = -1; e.coefs[:] = A.mix_coefficients(-1, True)
out = [e.render(0.5)]
for b in np.linspace(-1, 1, 80):
	e.blend = b; out.append(e._stride())
out.append(e.render(0.5)); y = np.concatenate(out)[:, 0]
jump = np.abs(np.diff(y)).max(); natural = np.abs(np.diff(orig[:, 0])).max()
check("full slider sweep has no clicks", jump <= natural * 1.05, f"largest sample step {jump:.4f} (the original's own: {natural:.4f})")

# 4. split tracks arriving mid-song (the splitter finished) fade in without a jump
e = engine(orig); e.blend = -1.0
a = e.render(1.0); e.add_split(inst, voc); b = e.render(1.0)
y = np.concatenate([a, b])[:, 0]
check("split tracks added while playing: no click", np.abs(np.diff(y)).max() <= natural * 1.05,
      f"largest sample step {np.abs(np.diff(y)).max():.4f}; after: 660 Hz {level(b[-SR // 2:], 660):.3f} (want 0)")

# 5. alignment: split tracks delayed by 300 samples are put back in place
late = lambda t: np.concatenate([np.zeros((300, 2), np.float32), t])[:len(t)]
n = SR * 40
rng = np.random.RandomState(0)
music = (rng.randn(n, 2) * 0.1).astype(np.float32); vocals = (rng.randn(n, 2) * 0.05).astype(np.float32)
i2, v2, lag, match = A.align(music + vocals, late(music), late(vocals))
check("alignment finds a 300-sample offset", lag == -300 and match > 0.99, f"shift {lag}, match {match:.3f}")

# 6. key and speed (pitch exact, tempo right)
for semis, speed in ((3, 1.0), (-4, 1.0), (0, 1.25), (2, 1.1)):
	e = engine(tone(440, 12)); e.semitones, e.speed = semis, speed
	y = e.render(6); f = peak_hz(y[SR:SR * 5]); want = 440 * 2 ** (semis / 12)
	check(f"key {semis:+d}, speed x{speed}", abs(cents(f, want)) < 2 and abs(e.q_nom / SR - 6 * speed) < 0.05,
	      f"{f:.2f} Hz (off {cents(f, want):+.2f} cents), song advanced {e.q_nom / SR:.3f} s in ~6 s")

# 7. no aliasing when shifting up
e = engine(tone(17000, 6)); e.semitones = 7
y = e.render(4)[SR:SR * 3]
lv = 20 * np.log10(np.sqrt((y ** 2).mean()) / (0.4 / np.sqrt(2)) + 1e-12)
check("no aliasing (17 kHz tone at +7)", lv < -60, f"{lv:.1f} dB left")

# 8. CPU: original + both split tracks mixed (the costliest case, mid-slider)
music = (np.random.RandomState(1).randn(SR * 240, 2) * 0.1).astype(np.float32)
for label, semis, blend in (("centre, key 0", 0, 0.0), ("slider at -0.5, key 0", 0, -0.5), ("slider at -0.1 (all 3 tracks), key +3", 3, -0.1)):
	e = engine(music, music * 0.7, music * 0.3); e.semitones, e.blend = semis, blend; e.render(1)
	t = time.perf_counter(); [e._stride() for _ in range(150)]; per = (time.perf_counter() - t) / 150
	print(f"INFO  CPU {label}: {per * 1000:.2f} ms per 30 ms of audio = {per / 0.030 * 100:.1f}% of one core")
print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
sys.exit(0 if ok else 1)
