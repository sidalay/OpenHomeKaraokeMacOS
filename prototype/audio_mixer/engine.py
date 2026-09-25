"""
Prototype song audio engine for OpenHomeKaraoke.

Plays a song's instrumental and vocal tracks together and mixes them live, so the
vocal level can move anywhere between "instrumental only" and "vocals only" without
a gap. Key and speed are also live. VLC keeps showing the video (with --no-audio),
and proto.py keeps this engine in step with VLC's clock.

Signal path, per output stride of ~30 ms:

    instrumental ─┐
                  ├─ mix (blend gains) ─ read at pitch ratio p ─ WSOLA ─ volume ─ speakers
    vocal ────────┘   (band-limited)      (pitch x p, tempo x p)  (tempo back to speed)

Reading the song at p times normal rate raises the pitch by p but also speeds it up;
WSOLA (waveform-similarity overlap-add, the same idea as VLC's scaletempo_pitch)
then restores the tempo by stitching ~30 ms slices at their best-matching offsets.
The song is decoded fully into memory, so every read is random access: no FIFOs
between stages, and at key 0 / speed 1 the output is bit-identical to the mix.

Sync: every output stride records which song position it came from, and every audio
callback records when its first sample reaches the speakers (PortAudio's DAC time).
From those, audible_position() says which song position is being heard right now.
"""
import collections, subprocess, threading, time
import numpy as np
import sounddevice as sd

SR = 44100


def decode(path, sr=SR):
	"""Whole file as float32 stereo at `sr`."""
	raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-f', 'f32le', '-ac', '2', '-ar', str(sr), '-'],
	                     capture_output = True, check = True).stdout
	return np.frombuffer(raw, np.float32).reshape(-1, 2)


def blend_gains(blend):
	"""blend -1 = instrumental only, 0 = both, +1 = vocals only -> (instrumental, vocal)."""
	blend = float(np.clip(blend, -1, 1))
	return (1.0, 1.0 + blend) if blend <= 0 else (1.0 - blend, 1.0)


class Engine:
	STRIDE_MS, OVERLAP, SEARCH_MS = 30, 0.2, 14      # WSOLA parameters, as in VLC's scaletempo
	TAPS = 32                                       # windowed-sinc interpolation length
	GAIN_STEP = 0.25                                # max blend-gain change per stride (~120 ms full swing)
	FADE_MS = 8                                     # fades for pause / resume / seek (no clicks)

	def __init__(self, instrumental, vocal, device = None, blocksize = 512, latency = 'low'):
		# file paths, or float32 (n, 2) arrays at SR (tests)
		i = instrumental if isinstance(instrumental, np.ndarray) else decode(instrumental)
		v = vocal if isinstance(vocal, np.ndarray) else decode(vocal)
		n = min(len(i), len(v))
		self.tracks = (np.ascontiguousarray(i[:n]), np.ascontiguousarray(v[:n]))
		self.length = n
		self.duration = n / SR

		# controls: plain attributes, set from any thread, read once per stride
		self.blend = 0.0
		self.semitones = 0.0
		self.speed = 1.0
		self.nudge = 1.0                            # small tempo trim used by the sync loop
		self.volume = 1.0

		self.S = int(SR * self.STRIDE_MS / 1000)
		self.O = int(self.S * self.OVERLAP)
		self.H = int(SR * self.SEARCH_MS / 1000) // 2
		self.xfade = np.linspace(0, 1, self.O, endpoint = False, dtype = np.float32)[:, None]
		self.fade_n = int(SR * self.FADE_MS / 1000)

		self.lock = threading.Lock()
		self._reset_dsp(0.0)
		self.gains = np.array(blend_gains(self.blend), np.float32)
		self.cur_volume = self.volume
		self.paused = True
		self._state = 'paused'                      # playing | fading_out | paused
		self._pending_seek = None
		self._fade_in = 0
		self.consumed = 0                           # output samples handed to PortAudio
		self.timing = None                          # (perf_counter when index plays, output index)
		self.underflows = 0
		self.callback_cost = collections.deque(maxlen = 2000)   # seconds spent per callback
		self.tap = None                             # set to a list to capture the output (tests)

		self.stream = sd.OutputStream(samplerate = SR, channels = 2, dtype = 'float32', device = device,
		                              blocksize = blocksize, latency = latency, callback = self._callback)
		self.output_latency = self.stream.latency

	# ------------------------------------------------------------------ control

	def start(self):
		self.stream.start()

	def close(self):
		self.stream.stop()
		self.stream.close()

	def play(self):
		with self.lock:
			if self._state != 'playing':
				self._state = 'playing'
				self._fade_in = self.fade_n
			self.paused = False

	def pause(self):
		with self.lock:
			if self._state == 'playing':
				self._state = 'fading_out'
			self.paused = True

	def seek(self, seconds):
		"""Jump to a song position (with a short fade out/in, no click)."""
		with self.lock:
			self._pending_seek = float(np.clip(seconds, 0, self.duration))

	def audible_position(self, at = None):
		"""Song position (seconds) being heard at perf_counter() time `at` (default now)."""
		at = time.perf_counter() if at is None else at
		with self.lock:
			if self.timing is None or not self.maps:
				return self.q_nom / SR
			t0, idx0 = self.timing
			k = idx0 + (0 if self._state == 'paused' else (at - t0) * SR)
			for start, q, tempo in reversed(self.maps):
				if start <= k:
					return (q + tempo * (k - start)) / SR
			start, q, tempo = self.maps[0]
			return (q + tempo * (k - start)) / SR

	# ---------------------------------------------------------------------- DSP

	def _reset_dsp(self, seconds):
		self.q_nom = seconds * SR                   # song position (samples) of the next stride
		self.overlap_buf = None                     # natural continuation of the previous slice
		self.fifo = np.zeros((0, 2), np.float32)    # produced, not yet played
		self.produced = getattr(self, 'consumed', 0)
		self.maps = collections.deque(maxlen = 400) # (output index, song position, tempo)

	def _read(self, q0, step, n, gains):
		"""n samples of the mix at song positions q0, q0+step, ... (band-limited)."""
		inst, voc = self.tracks
		if step == 1.0 and q0 == int(q0):
			# integer positions: exact samples (bit-identical at key 0 / speed 1)
			a = int(q0)
			lo, hi = max(a, 0), min(a + n, self.length)
			out = np.zeros((n, 2), np.float32)
			if hi > lo:
				out[lo - a:hi - a] = gains[0] * inst[lo:hi] + gains[1] * voc[lo:hi]
			return out
		pos = q0 + step * np.arange(n)
		base = np.floor(pos).astype(np.int64)
		frac = pos - base
		taps = np.arange(-self.TAPS // 2 + 1, self.TAPS // 2 + 1)
		idx = base[:, None] + taps[None, :]
		dist = frac[:, None] - taps[None, :]
		# Low-pass below the new Nyquist when reading faster (key up) so nothing aliases.
		# 32-tap Blackman-windowed sinc: a 17 kHz tone shifted +7 semitones (above
		# Nyquist) is suppressed by 72 dB, and a 10 kHz tone passes unchanged.
		fc = 0.5 * min(1.0, 1.0 / step) * 0.90
		h = self.TAPS / 2
		window = 0.42 + 0.5 * np.cos(np.pi * dist / h) + 0.08 * np.cos(2 * np.pi * dist / h)
		w = 2 * fc * np.sinc(2 * fc * dist) * window
		w /= w.sum(axis = 1, keepdims = True)
		inside = (idx >= 0) & (idx < self.length)
		idx = np.clip(idx, 0, self.length - 1)
		w = (w * inside).astype(np.float32)
		mix = gains[0] * inst[idx] + gains[1] * voc[idx]           # (n, taps, 2)
		return np.einsum('nt,ntc->nc', w, mix)

	def _stride(self):
		"""Produce the next S output samples."""
		S, O, H = self.S, self.O, self.H
		p = 2.0 ** (self.semitones / 12.0)          # pitch ratio
		tempo = self.speed * self.nudge             # song samples per output sample
		target = np.array(blend_gains(self.blend), np.float32)
		self.gains += np.clip(target - self.gains, -self.GAIN_STEP, self.GAIN_STEP)
		q = self.q_nom
		if self.overlap_buf is None:
			best = H                                # nothing to match yet
		else:
			region = self._read(q - p * H, p, 2 * H + O, self.gains).sum(axis = 1)
			ref = self.overlap_buf.sum(axis = 1)
			corr = np.correlate(region, ref, 'valid')
			energy = np.convolve(region * region, np.ones(O, np.float32), 'valid') + 1e-9
			best = int(np.argmax(corr / np.sqrt(energy)))
		start = q + p * (best - H)
		if p == 1.0 and start != int(start):
			start = float(round(start))
		seg = self._read(start, p, S + O, self.gains)
		out = seg[:S].copy()
		if self.overlap_buf is not None:
			out[:O] = self.overlap_buf * (1 - self.xfade) + seg[:O] * self.xfade
		self.overlap_buf = seg[S:]
		self.maps.append((self.produced, q, tempo))
		self.produced += S
		self.q_nom = q + S * tempo
		return out

	def _take(self, frames):
		while len(self.fifo) < frames:
			self.fifo = np.concatenate([self.fifo, self._stride()])
		block, self.fifo = self.fifo[:frames], self.fifo[frames:]
		return block

	def _callback(self, outdata, frames, time_info, status):
		t_start = time.perf_counter()
		if status.output_underflow:
			self.underflows += 1
		with self.lock:
			first = self.consumed                   # output index of this block's first sample
			block = np.zeros((frames, 2), np.float32)
			fade_out = np.linspace(1, 0, frames, dtype = np.float32)[:, None]
			if self._pending_seek is not None:
				if self._state == 'playing':
					block = self._take(frames) * fade_out   # old position fades out ...
					self.consumed += frames
				self._reset_dsp(self._pending_seek)          # ... new one fades in next block
				self._fade_in = self.fade_n
				self._pending_seek = None
			elif self._state == 'playing':
				block = self._take(frames)
				if self._fade_in:
					n = min(self._fade_in, frames)
					done = self.fade_n - self._fade_in
					block[:n] *= (np.arange(done, done + n, dtype = np.float32) / self.fade_n)[:, None]
					self._fade_in -= n
				self.consumed += frames
			elif self._state == 'fading_out':
				block = self._take(frames) * fade_out
				self.consumed += frames
				self._state = 'paused'
			if self.tap is not None:
				self.tap.append(block.copy())
			v0, v1 = self.cur_volume, float(self.volume)       # volume, smoothed per block
			outdata[:] = block * np.linspace(v0, v1, frames, dtype = np.float32)[:, None]
			self.cur_volume = v1
			# when this block's first sample reaches the speakers (PortAudio DAC time)
			self.timing = (t_start + (time_info.outputBufferDacTime - time_info.currentTime), first)
		self.callback_cost.append(time.perf_counter() - t_start)
