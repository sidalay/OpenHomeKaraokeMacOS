"""Offline checks of the engine's signal processing (no audio is played)."""
import os, sys, time
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine as E
SR = E.SR

def tone(f, sec, amp = 0.4):
	t = np.arange(int(SR * sec)) / SR
	return np.stack([amp * np.sin(2 * np.pi * f * t)] * 2, 1).astype(np.float32)

def run(eng, sec):
	"""Render at least `sec` seconds through the DSP without a sound card. Whole strides
	only: cutting a stride short would glue two non-adjacent samples together."""
	out, n = [], 0
	while n < SR * sec:
		out.append(eng._stride()); n += len(out[-1])
	return np.concatenate(out)

def peak_hz(x):
	x = x[:, 0]; n = 1 << 18
	spec = np.abs(np.fft.rfft(x * np.hanning(len(x)), n))
	k = int(np.argmax(spec)); a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
	return (k + 0.5 * (a - c) / (a - 2 * b + c)) * SR / n            # parabolic peak interpolation

def cents(f, ref): return 1200 * np.log2(f / ref)

def engine(inst, voc):
	e = E.Engine(inst, voc); e.stream.close(); return e    # no stream needed offline

ok = True
def check(name, cond, detail):
	global ok; ok &= bool(cond); print(f"{'PASS' if cond else 'FAIL'}  {name}: {detail}")

# 1. identity: key 0, speed 1, blend 0 -> exactly instrumental + vocal
inst, voc = tone(440, 6), tone(660, 6, 0.3)
e = engine(inst, voc); y = run(e, 5)
err = np.abs(y - (inst + voc)[:len(y)]).max()
check("key 0 / speed 1 is identical to the mix", err < 1e-6, f"max difference {err:.1e} ({20 * np.log10(err / 0.7 + 1e-30):.0f} dB, float rounding in the crossfade)")

# 2. key changes: pitch moves by the right amount, tempo does not
for semis in (3, -4, 7):
	e = engine(tone(440, 8), tone(440, 8, 0)); e.semitones = semis
	y = run(e, 6); f = peak_hz(y[SR:SR * 5]); want = 440 * 2 ** (semis / 12)
	check(f"key {semis:+d}", abs(cents(f, want)) < 2 and abs(e.q_nom / SR - 6) < 0.05,
	      f"{f:.2f} Hz (want {want:.2f}, off {cents(f, want):+.2f} cents), song advanced {e.q_nom / SR:.3f} s in 6 s")

# 3. speed changes: tempo moves, pitch stays
for speed in (0.8, 1.25):
	e = engine(tone(440, 10), tone(440, 10, 0)); e.speed = speed
	y = run(e, 6); f = peak_hz(y[SR:SR * 5])
	check(f"speed x{speed}", abs(cents(f, 440)) < 2 and abs(e.q_nom / SR - 6 * speed) < 0.05,
	      f"{f:.2f} Hz (off {cents(f, 440):+.2f} cents), song advanced {e.q_nom / SR:.3f} s in 6 s (want {6 * speed:.1f})")

# 4. key + speed together
e = engine(tone(440, 10), tone(440, 10, 0)); e.semitones, e.speed = 2, 1.1
y = run(e, 6); f = peak_hz(y[SR:SR * 5]); want = 440 * 2 ** (2 / 12)
check("key +2 and speed x1.1", abs(cents(f, want)) < 2 and abs(e.q_nom / SR - 6.6) < 0.05,
      f"{f:.2f} Hz (off {cents(f, want):+.2f} cents), advanced {e.q_nom / SR:.3f} s (want 6.6)")

# 5. no aliasing when shifting up: a 17 kHz tone at +7 semitones would land at 25.5 kHz (above
#    Nyquist); unfiltered it would fold back to ~18.6 kHz. It must be removed instead.
e = engine(tone(17000, 6), tone(17000, 6, 0)); e.semitones = 7
y = run(e, 4)[SR:SR * 3]; ref = tone(17000, 2)
level = 20 * np.log10(np.sqrt((y ** 2).mean()) / np.sqrt((ref ** 2).mean()) + 1e-12)
check("no aliasing (17 kHz tone at +7)", level < -60, f"what is left: {level:.1f} dB relative to the input")

# 6. blend: sweep from instrumental-only to vocals-only over 2 s, no clicks
inst, voc = tone(440, 6), tone(660, 6, 0.4)
e = engine(inst, voc); e.blend = -1.0; e.gains[:] = E.blend_gains(-1.0)
out = [run(e, 0.5)]
for b in np.linspace(-1, 1, 67):                            # one step per 30 ms stride
	e.blend = b; out.append(e._stride())
out.append(run(e, 0.5)); y = np.concatenate(out)[:, 0]
jump = np.abs(np.diff(y)).max(); natural = np.abs(np.diff((inst + voc)[:, 0])).max()
lvl = lambda seg, f: 2 * np.abs(np.fft.rfft(seg * np.hanning(len(seg)))[int(f * len(seg) / SR)]) / (len(seg) / 2)
start, end = y[:SR // 4], y[-SR // 4:]
check("blend sweep has no clicks", jump <= natural * 1.05, f"largest sample step {jump:.4f} (a clean mix of both tones: {natural:.4f})")
check("blend ends", lvl(start, 660) < 0.01 and lvl(end, 440) < 0.01,
      f"start: vocal tone {lvl(start, 660):.3f}, instrumental {lvl(start, 440):.3f} | end: vocal {lvl(end, 660):.3f}, instrumental {lvl(end, 440):.3f}")

# 7. CPU per stride (30 ms of audio), on real-length data
inst = (np.random.RandomState(1).randn(SR * 240, 2) * 0.1).astype(np.float32)
for label, semis, speed in (("key 0, speed 1", 0, 1.0), ("key +3", 3, 1.0), ("speed x1.2", 0, 1.2)):
	e = engine(inst, inst); e.semitones, e.speed = semis, speed; run(e, 1)
	t = time.perf_counter(); n = 200
	for _ in range(n): e._stride()
	per = (time.perf_counter() - t) / n
	print(f"INFO  CPU {label}: {per * 1000:.2f} ms per 30 ms of audio = {per / 0.030 * 100:.1f}% of one core")
print("\nALL PASSED" if ok else "\nSOME CHECKS FAILED")
