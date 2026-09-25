"""
Song audio engine: VLC shows the video (started with --no-audio) and this plays the
song's audio, so the vocal level, key and speed all change live without a gap.

Tracks: the song's original audio, plus the splitter's instrumental and vocal tracks
when they exist. The vocal slider (blend) runs from -1 (instrumental only) through 0
(the original recording) to +1 (vocals only). Near the centre the original crossfades
into the split tracks, which by then add up to almost the same thing, so the move from
"the real recording" to "split tracks at any level" is seamless.

Signal path, per output stride of ~30 ms:

    original ─────┐
    instrumental ─┼─ mix ─ read at pitch ratio p ─ WSOLA ──────────── volume ─ speakers
    vocal ────────┘         (band-limited)        (tempo back to speed)

Reading the song p times faster raises the pitch by p but also speeds it up; WSOLA
(waveform-similarity overlap-add, the method of VLC's scaletempo_pitch) restores the
tempo by stitching ~30 ms slices at their best-matching offsets. The whole song is in
memory, so every read is random access, and at key 0 / speed 1 the output is the mix.

AVSync keeps the engine on VLC's clock: small differences are removed by trimming the
engine's tempo by up to 2% (WSOLA keeps the pitch, so it is inaudible), large ones
(a seek) by jumping with a short fade.

Developed and measured as prototype/audio_mixer (see its README for the numbers).
"""
import collections, logging, subprocess, threading, time
import numpy as np

try:
	import sounddevice as sd
except Exception as e:                    # missing module, or PortAudio not installed
	sd = None
	_sd_error = e

SR = 44100
CENTRE_WIDTH = 0.15                       # blend range around 0 where the original fades in


def available():
	"""(True, '') if the engine can run here, else (False, reason)."""
	if sd is None:
		return False, f'sounddevice is not available ({_sd_error})'
	try:
		sd.query_devices(kind = 'output')
		return True, ''
	except Exception as e:
		return False, f'no audio output device ({e})'


def decode(path, sr = SR):
	"""Whole file's first audio stream as float32 stereo at `sr` (video is ignored)."""
	raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-map', '0:a:0', '-vn', '-f', 'f32le', '-ac', '2', '-ar', str(sr), '-'],
	                     capture_output = True, check = True).stdout
	return np.frombuffer(raw, np.float32).reshape(-1, 2)


def align(original, instrumental, vocal, max_lag = 0.5):
	"""Shift the split tracks so they line up with the original sample for sample.

	The splitter re-encodes its output, which can move it by an encoder delay; blending
	misaligned copies would sound hollow (comb filtering). Returns the aligned tracks,
	the shift in samples and how well instrumental + vocal matches the original (0..1).
	"""
	n = min(len(original), len(instrumental), len(vocal))
	a, b = max(0, n // 2 - SR * 15), min(n, n // 2 + SR * 15)            # 30 s from the middle
	x = original[a:b].mean(axis = 1)
	y = (instrumental[a:b] + vocal[a:b]).mean(axis = 1)
	size = 1 << int(np.ceil(np.log2(2 * len(x))))
	corr = np.fft.irfft(np.fft.rfft(x, size) * np.conj(np.fft.rfft(y, size)), size)
	m = int(SR * max_lag)
	lags = np.concatenate([np.arange(0, m + 1), np.arange(-m, 0)])
	vals = np.concatenate([corr[:m + 1], corr[-m:]])
	lag = int(lags[np.argmax(vals)])                  # split must move by `lag` samples
	match = float(np.max(vals) / (np.sqrt(np.dot(x, x) * np.dot(y, y)) + 1e-12))

	def shift(t):
		if lag > 0:
			t = np.concatenate([np.zeros((lag, 2), np.float32), t])
		elif lag < 0:
			t = t[-lag:]
		t = t[:len(original)]
		if len(t) < len(original):
			t = np.concatenate([t, np.zeros((len(original) - len(t), 2), np.float32)])
		return np.ascontiguousarray(t)
	return shift(instrumental), shift(vocal), lag, match


def blend_gains(blend):
	"""blend -1 = instrumental only, 0 = both, +1 = vocals only -> (instrumental, vocal)."""
	blend = float(np.clip(blend, -1, 1))
	return (1.0, 1.0 + blend) if blend <= 0 else (1.0 - blend, 1.0)


def mix_coefficients(blend, has_split):
	"""(original, instrumental, vocal) gains for a blend position."""
	if not has_split:
		return (1.0, 0.0, 0.0)
	g_inst, g_voc = blend_gains(blend)
	w_orig = max(0.0, 1.0 - abs(float(blend)) / CENTRE_WIDTH)
	return (w_orig, (1 - w_orig) * g_inst, (1 - w_orig) * g_voc)


class Engine:
	STRIDE_MS, OVERLAP, SEARCH_MS = 30, 0.2, 14      # WSOLA parameters, as in VLC's scaletempo
	TAPS = 32                                       # windowed-sinc interpolation length
	GAIN_STEP = 0.25                                # max mix-gain change per stride (~120 ms full swing)
	FADE_MS = 8                                     # fades for pause / resume / seek (no clicks)

	def __init__(self, original, device = None, blocksize = 512, latency = 'low'):
		# a file path, or a float32 (n, 2) array at SR (tests)
		self.original = original if isinstance(original, np.ndarray) else decode(original)
		self.length = len(self.original)
		self.duration = self.length / SR
		self.split = None                           # (instrumental, vocal), same length as original
		self.split_info = None

		# controls: plain attributes, set from any thread, read once per stride
		self.blend = 0.0
		self.semitones = 0.0
		self.speed = 1.0
		self.nudge = 1.0                            # small tempo trim used by AVSync
		self.volume = 1.0

		self.S = int(SR * self.STRIDE_MS / 1000)
		self.O = int(self.S * self.OVERLAP)
		self.H = int(SR * self.SEARCH_MS / 1000) // 2
		self.xfade = np.linspace(0, 1, self.O, endpoint = False, dtype = np.float32)[:, None]
		self.fade_n = int(SR * self.FADE_MS / 1000)
		taps = np.arange(-self.TAPS // 2 + 1, self.TAPS // 2 + 1)
		self._taps = taps

		self.lock = threading.Lock()
		self.consumed = 0                           # output samples handed to PortAudio
		self._reset_dsp(0.0)
		self.coefs = np.array(mix_coefficients(self.blend, False), np.float32)
		self.cur_volume = self.volume
		self._state = 'paused'                      # playing | fading_out | paused
		self._pending_seek = None
		self._fade_in = 0
		self.timing = None                          # (perf_counter when index plays, output index)
		self.underflows = 0
		self.callback_cost = collections.deque(maxlen = 2000)
		self.tap = None                             # set to a list to capture the output (tests)
		self.stream = None
		self.output_latency = 0.0
		if device is not False:                     # device=False: no sound card (offline tests)
			# CoreAudio sometimes refuses a new stream for a moment (PortAudio -9986), e.g. right
			# after the previous song's stream closed on a quick skip; try again before giving up
			for attempt in range(3):
				try:
					self.stream = sd.OutputStream(samplerate = SR, channels = 2, dtype = 'float32', device = device,
					                              blocksize = blocksize, latency = latency, callback = self._callback)
					break
				except sd.PortAudioError as e:
					if attempt == 2:
						raise
					logging.info(f"Audio output not ready ({e}); retrying")
					time.sleep(0.3)
			self.output_latency = self.stream.latency

	# ----------------------------------------------------------------- tracks

	@property
	def has_split(self):
		return self.split is not None

	def add_split(self, instrumental, vocal):
		"""Add the splitter's tracks (paths or arrays). Safe while playing: the mix fades in."""
		i = instrumental if isinstance(instrumental, np.ndarray) else decode(instrumental)
		v = vocal if isinstance(vocal, np.ndarray) else decode(vocal)
		i, v, lag, match = align(self.original, i, v)
		self.split_info = {'lag_ms': round(lag / SR * 1000, 1), 'match': round(match, 3)}
		if match < 0.5:
			logging.warning(f"Split tracks match the original poorly ({match:.2f}); the vocal slider may sound off")
		with self.lock:
			self.split = (i, v)

	def remove_split(self):
		with self.lock:
			self.split = None

	# ---------------------------------------------------------------- control

	def start(self):
		if self.stream:
			self.stream.start()

	def close(self):
		if self.stream:
			try:
				self.stream.stop()
				self.stream.close()
			except Exception:
				pass
			self.stream = None

	@property
	def playing(self):
		return self._state == 'playing'

	def play(self):
		with self.lock:
			if self._state != 'playing':
				self._state = 'playing'
				self._fade_in = self.fade_n

	def pause(self):
		with self.lock:
			if self._state == 'playing':
				self._state = 'fading_out'

	def seek(self, seconds):
		"""Jump to a song position (short fade out/in, no click)."""
		with self.lock:
			self._pending_seek = float(np.clip(seconds, 0, self.duration))

	def audible_position(self, at = None):
		"""Song position (seconds) being heard at perf_counter() time `at` (default now)."""
		at = time.perf_counter() if at is None else at
		with self.lock:
			if self._pending_seek is not None:
				return self._pending_seek
			if self.timing is None or not self.maps:
				return self.q_nom / SR
			t0, idx0 = self.timing
			k = idx0 + (0 if self._state == 'paused' else (at - t0) * SR)
			for start, q, tempo in reversed(self.maps):
				if start <= k:
					return (q + tempo * (k - start)) / SR
			start, q, tempo = self.maps[0]
			return (q + tempo * (k - start)) / SR

	# -------------------------------------------------------------------- DSP

	def _reset_dsp(self, seconds):
		self.q_nom = seconds * SR                   # song position (samples) of the next stride
		self.overlap_buf = None                     # natural continuation of the previous slice
		self.fifo = np.zeros((0, 2), np.float32)    # produced, not yet played
		self.produced = self.consumed
		self.maps = collections.deque(maxlen = 400) # (output index, song position, tempo)

	def _tracks(self, coefs):
		tracks = [(coefs[0], self.original)]
		if self.split is not None:
			tracks += [(coefs[1], self.split[0]), (coefs[2], self.split[1])]
		return [(c, t) for c, t in tracks if abs(c) > 1e-4]

	def _read(self, q0, step, n, coefs):
		"""n samples of the mix at song positions q0, q0+step, ... (band-limited)."""
		tracks = self._tracks(coefs)
		out = np.zeros((n, 2), np.float32)
		if step == 1.0 and q0 == int(q0):
			# integer positions: exact samples (the mix itself at key 0 / speed 1)
			a = int(q0)
			lo, hi = max(a, 0), min(a + n, self.length)
			for c, t in tracks:
				if hi > lo:
					out[lo - a:hi - a] += c * t[lo:hi]
			return out
		pos = q0 + step * np.arange(n)
		base = np.floor(pos).astype(np.int64)
		idx = base[:, None] + self._taps[None, :]
		dist = (pos - base)[:, None] - self._taps[None, :]
		# Low-pass below the new Nyquist when reading faster (key up) so nothing aliases.
		# 32-tap Blackman-windowed sinc: a 17 kHz tone shifted +7 semitones is suppressed
		# by 72 dB, and a 10 kHz tone passes unchanged.
		fc = 0.5 * min(1.0, 1.0 / step) * 0.90
		h = self.TAPS / 2
		window = 0.42 + 0.5 * np.cos(np.pi * dist / h) + 0.08 * np.cos(2 * np.pi * dist / h)
		w = 2 * fc * np.sinc(2 * fc * dist) * window
		w /= w.sum(axis = 1, keepdims = True)
		inside = (idx >= 0) & (idx < self.length)
		idx = np.clip(idx, 0, self.length - 1)
		w = (w * inside).astype(np.float32)
		for c, t in tracks:
			out += np.einsum('nt,ntc->nc', w, t[idx]) * c
		return out

	def _stride(self):
		"""Produce the next S output samples."""
		S, O, H = self.S, self.O, self.H
		p = 2.0 ** (self.semitones / 12.0)          # pitch ratio
		tempo = self.speed * self.nudge             # song samples per output sample
		target = np.array(mix_coefficients(self.blend, self.split is not None), np.float32)
		self.coefs += np.clip(target - self.coefs, -self.GAIN_STEP, self.GAIN_STEP)
		q = self.q_nom
		if self.overlap_buf is None:
			best = H                                # nothing to match yet
		else:
			region = self._read(q - p * H, p, 2 * H + O, self.coefs).sum(axis = 1)
			ref = self.overlap_buf.sum(axis = 1)
			corr = np.correlate(region, ref, 'valid')
			energy = np.convolve(region * region, np.ones(O, np.float32), 'valid') + 1e-9
			best = int(np.argmax(corr / np.sqrt(energy)))
		start = q + p * (best - H)
		if p == 1.0 and start != int(start):
			start = float(round(start))
		seg = self._read(start, p, S + O, self.coefs)
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

	def render(self, seconds):
		"""Offline: at least `seconds` of output, whole strides (tests; no sound card)."""
		out, n = [], 0
		while n < SR * seconds:
			out.append(self._stride())
			n += len(out[-1])
		return np.concatenate(out)

	def _callback(self, outdata, frames, time_info, status):
		t_start = time.perf_counter()
		if status.output_underflow:
			self.underflows += 1
		try:
			with self.lock:
				first = self.consumed               # output index of this block's first sample
				block = np.zeros((frames, 2), np.float32)
				fade_out = np.linspace(1, 0, frames, dtype = np.float32)[:, None]
				if self._pending_seek is not None:
					if self._state == 'playing':
						block = self._take(frames) * fade_out     # old position fades out ...
						self.consumed += frames
					self._reset_dsp(self._pending_seek)            # ... new one fades in next block
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
				v0, v1 = self.cur_volume, float(self.volume)   # smoothed per block
				np.clip(block * np.linspace(v0, v1, frames, dtype = np.float32)[:, None], -1, 1, out = outdata)
				self.cur_volume = v1
				# when this block's first sample reaches the speakers (PortAudio DAC time)
				self.timing = (t_start + (time_info.outputBufferDacTime - time_info.currentTime), first)
		except Exception as e:                      # never let an exception kill the stream
			outdata.fill(0)
			logging.error(f"Audio engine callback failed: {e}")
		self.callback_cost.append(time.perf_counter() - t_start)


class ClockModel:
	"""VLC's playback time as slope * wall + offset, from its coarsely refreshed readings.

	VLC refreshes its clock only every ~250 ms, so one reading can be up to ~0.5 s stale;
	but each refresh is exact when it happens, so over a 2 s window the reading furthest
	ahead of the wall clock is the freshest. The slope is VLC's measured speed: its rate
	setting is not exact (x1.2 runs at about x1.2033), so once there are enough fresh
	readings (the first after each refresh) it is fitted; until then the setting is used.
	"""
	WINDOW = 2.0
	FIT_WINDOW = 8.0
	MIN_FIT = 3.0

	def __init__(self):
		self.samples = collections.deque()          # (wall, vlc time)
		self.fresh = collections.deque()
		self.rate = 1.0
		self.slope = 1.0
		self.state = None
		self.resets = 0
		self.moving = False                         # VLC's clock is really advancing

	def reset(self):
		self.samples.clear()
		self.fresh.clear()
		self.slope = self.rate
		self.moving = False
		self.resets += 1

	def add(self, t0, vlc_time, state, rate):
		if state != self.state or abs(rate - self.rate) > 1e-3:
			self.reset()                            # pause, resume, speed change: new line
		elif self.samples and state == 'playing':
			# a seek VLC did on its own: stale readings may trail the prediction, never lead
			ahead = vlc_time - self.estimate(t0)
			if ahead > 0.15 or ahead < -1.0:
				self.reset()
		self.state, self.rate = state, rate
		# VLC says "playing" while it is still buffering (at a song's start, after a seek) and
		# its clock then sits still for a while; a seek also makes it jump. It is moving once
		# it takes a small step forward: start the model afresh from that reading, dropping
		# the stuck ones, which would otherwise make it look ahead of where it is.
		if not self.moving and state == 'playing' and self.samples:
			step = vlc_time - self.samples[-1][1]
			if 0 < step <= 0.6 * max(rate, 1.0):
				self.moving = True
				self.samples.clear()
				self.fresh.clear()
		if not self.samples:
			self.slope = rate
		if self.samples and vlc_time != self.samples[-1][1]:
			self.fresh.append((t0, vlc_time))
			while self.fresh and self.fresh[0][0] < t0 - self.FIT_WINDOW:
				self.fresh.popleft()
			if state == 'playing' and len(self.fresh) > 6 and self.fresh[-1][0] - self.fresh[0][0] >= self.MIN_FIT:
				w, v = np.array(self.fresh).T
				self.slope = float(np.polyfit(w - w[0], v, 1)[0])
		self.samples.append((t0, vlc_time))
		while self.samples and self.samples[0][0] < t0 - self.WINDOW:
			self.samples.popleft()

	def time_at(self, value):
		"""Wall time at which VLC's clock was (or will be) at `value`, while it is moving."""
		if not self.samples or not self.moving:
			return None
		offset = max(v - self.slope * t for t, v in self.samples)
		return (value - offset) / self.slope

	def span(self):
		return self.samples[-1][0] - self.samples[0][0] if self.samples else 0

	def estimate(self, at):
		if not self.samples:
			return None
		if self.state != 'playing':
			return self.samples[-1][1]
		offset = max(v - self.slope * t for t, v in self.samples)
		return self.slope * at + offset


class AVSync(threading.Thread):
	"""Keeps an Engine on VLC's clock. `clock()` returns (perf_counter at request, reply
	with "state", "time_us", "rate") or None. The engine plays while VLC plays."""
	POLL = 0.04
	SEEK_THRESHOLD = 0.15    # s: larger differences jump, smaller ones are trimmed away
	SETTLE_TIME = 3.0        # s after a resume / seek / speed change ...
	SETTLE_THRESHOLD = 0.08  # ... during which differences over 80 ms jump right away
	TRIM_ON, TRIM_OFF = 0.04, 0.015  # s: trim the tempo beyond 40 ms (about where A/V offsets get noticeable), stop within 15 ms
	GAIN = 0.25              # tempo trim per second of drift (0.1 s -> 2.5%)
	INTEGRAL = 0.05          # learns a steady speed difference, so no offset remains
	MAX_NUDGE = 0.02
	# When VLC's clock (re)starts, how long after the moment we know of does it really move?
	#   'start':   after VLC first says "playing" (it is still buffering): 0.22-0.28 s measured
	#   'restart': after a seek to 0 is sent: VLC resumes from the first frame almost at once
	# Learned from each event, separately, and shared by the next songs. (A seek elsewhere
	# is not predicted: VLC decodes on from the previous keyframe first, which takes a
	# varying time, so the audio waits until VLC's clock is seen moving instead.)
	delays = {'start': 0.25, 'restart': 0.03}
	LEARN_AFTER = 1.5        # s of real clock readings before a delay is measured

	def __init__(self, engine, clock):
		super().__init__(daemon = True, name = 'AVSync')
		self.engine = engine
		self.clock = clock
		self.model = ClockModel()
		self.audio_delay = 0.0                      # s; positive = audio later than video
		self.drift = None
		self.integral = 0.0
		self.last_seek = 0.0
		self.settle_until = 0.0
		self.model_resets = 0
		self.started = False                        # engine placed on VLC's timeline yet?
		self.running = True
		self.failures = 0
		self.born = time.perf_counter()
		self.trimming = False
		self.first_playing_seen = False
		self.plan = None                            # the audio's planned (re)start, see _plan()
		self.plan_timer = None
		self.plan_pending = False
		self.moving_since = None
		self.paused_seek = None                     # position of a seek made while paused
		self.no_settle = False                      # a speed change: trim, don't jump
		self.seen_underflows = 0

	def stop(self):
		self.running = False
		if self.plan_timer:
			self.plan_timer.cancel()

	def seek(self, seconds, sent_at = None):
		"""Call when telling VLC to seek, with `sent_at` the perf_counter() from just before
		the request went to VLC. A restart (seek to 0) is predicted like a song start, so
		its first moments are not skipped. Anywhere else the audio waits until VLC's clock
		is seen moving at its new position, then comes in exactly there (a brief pause
		after a seek, instead of a correcting jump a second later)."""
		sent_at = time.perf_counter() if sent_at is None else sent_at
		if self.plan_timer:
			self.plan_timer.cancel()
		self.plan, self.plan_pending = None, False
		paused = self.model.state == 'paused'
		self.engine.pause()
		self.engine.seek(seconds)                   # so a resume from pause starts here too
		self.model.reset()
		self.moving_since = None
		self.started = False
		self.paused_seek = float(seconds) if paused else None
		if seconds <= 0 and not paused:
			self._plan('restart', sent_at, 0.0)

	def resumed(self, sent_at):
		"""Call when telling VLC to resume. After a seek made while paused, VLC needs a
		moment again (like after any seek): returns True, and the audio comes in once
		VLC's clock moves. Otherwise returns False, and the caller resumes the engine."""
		if self.paused_seek is None:
			return False
		self.paused_seek = None
		self.model.reset()
		self.moving_since = None
		return True

	def speed_changed(self):
		self.integral = 0.0
		self.no_settle = True                       # the model resets, but nothing needs to jump

	def run(self):
		next_control = 0.0
		while self.running:
			reading = None
			try:
				reading = self.clock()
			except Exception:
				pass
			if not reading or 'time_us' not in reading[1]:
				self.failures += 1
				if self.failures >= 5 and self.engine.playing:
					self.engine.pause()             # VLC gone (song over) or unreachable
				time.sleep(self.POLL)
				continue
			self.failures = 0
			t0, d = reading
			was_moving = self.model.moving
			self.model.add(t0, d['time_us'] / 1e6, d['state'], float(d['rate']))
			if not self.first_playing_seen and d['state'] == 'playing':
				self.first_playing_seen = True
				self._plan('start', t0, d['time_us'] / 1e6)
			if self.model.moving and not was_moving:
				self.moving_since = t0
			p = self.plan
			if (p and not p['learned'] and p['started'] and self.moving_since
			        and t0 - self.moving_since >= self.LEARN_AFTER):
				self._learn()
			now = time.perf_counter()
			if now >= next_control:
				try:
					self._control(now)
				except Exception as e:
					logging.error(f"AVSync control failed: {e}")
				next_control = now + 0.1
			time.sleep(self.POLL)

	def _plan(self, kind, t_ref, position, resuming = False):
		"""VLC's clock is about to (re)start at `position`: at a song start ('start',
		t_ref = when VLC first said "playing") or after a seek ('seek', t_ref = when the
		request was sent). Start the audio so that it is heard just as VLC's clock gets
		there: after the learned delay, minus the audio output's own latency. Nothing is
		skipped and nothing has to jump later."""
		if self.plan_timer:
			self.plan_timer.cancel()
		plan = self.plan = {'kind': kind, 't_ref': t_ref, 'position': position, 'learned': False, 'started': False}
		self.plan_pending = True
		when = t_ref + AVSync.delays[kind] - self.engine.output_latency

		def go():
			if not self.running or self.plan is not plan:
				return
			self.plan_pending = False
			# still paused: the engine resumes with VLC, from `position`. (Not checked when
			# VLC was just told to resume: the latest reading may lag and still say paused.)
			if self.model.state == 'paused' and not resuming:
				return
			late = max(0.0, time.perf_counter() - when) * max(self.model.rate, 0.1)
			self.engine.seek(position + late)
			self.engine.nudge = 1.0
			self.engine.play()
			self.started = plan['started'] = True
			self.last_seek = time.perf_counter()
			self._log(self.last_seek, f"audio {'started' if kind == 'start' else 'back'} at {position + late:.3f}s, "
			                          f"{AVSync.delays[kind] * 1000:.0f} ms after "
			                          f"{'VLC said playing' if kind == 'start' else 'the seek was sent'}")
		self.plan_timer = threading.Timer(max(0.0, when - time.perf_counter()), go)
		self.plan_timer.daemon = True
		self.plan_timer.start()

	def _learn(self):
		"""How long did VLC's clock really take this time? Measured with the same estimate
		the sync uses (the freshest reading over a window), not the first reading, which
		can be stale by tens of ms."""
		p = self.plan
		p['learned'] = True
		reached = self.model.time_at(p['position'])
		if reached is None:
			return
		actual = reached - p['t_ref']
		if 0.0 <= actual < 1.0:                     # otherwise VLC landed elsewhere (a keyframe)
			old = AVSync.delays[p['kind']]
			AVSync.delays[p['kind']] = float(np.clip(0.6 * old + 0.4 * actual, 0.0, 1.0))
			self._log(time.perf_counter(), f"VLC's clock moved {actual * 1000:.0f} ms after "
			          f"{'saying playing' if p['kind'] == 'start' else 'the seek'}; now expecting "
			          f"{AVSync.delays[p['kind']] * 1000:.0f} ms")

	def _log(self, now, msg):
		logging.info(f"AVSync +{now - self.born:5.2f}s: {msg}")

	def _control(self, now, dt = 0.1):
		e = self.engine
		if e.underflows != self.seen_underflows:
			self._log(now, f"audio dropouts: {e.underflows - self.seen_underflows} (total {e.underflows})")
			self.seen_underflows = e.underflows
		if self.model.resets != self.model_resets:  # VLC resumed, seeked or changed speed
			self.model_resets = self.model.resets
			if not self.no_settle:
				self.settle_until = now + 0.6 + self.SETTLE_TIME
			self.no_settle = False
		if self.model.state != 'playing':
			if e.playing:
				e.pause()
			e.nudge = 1.0
			return
		estimate = self.model.estimate(now)
		if estimate is None:
			return
		target = estimate - self.audio_delay
		if not self.started:
			# After a seek (or a song start the plan missed): wait until VLC's clock is
			# really moving at its new position, then land where VLC will be when the audio
			# is heard. A restart is planned instead (see seek()), unless it is superseded.
			if not self.model.moving or self.plan_pending:
				return
			e.seek(max(0.0, target + e.output_latency + 0.02))
			e.nudge = 1.0
			e.play()
			self.started = True
			self.last_seek = now
			self._log(now, f"audio back at {target:.3f}s once VLC's clock moved")
			return
		if not e.playing and not self.plan_pending:
			e.play()                                # VLC resumed: continue from where we paused
		# Don't correct from the readings VLC gives while still buffering (its clock is not
		# moving yet, so they would make it look ahead): wait for a real reading.
		if not self.model.moving or self.model.span() < 0.6:
			return
		drift = target - e.audible_position(now)
		self.drift = drift
		threshold = self.SETTLE_THRESHOLD if now < self.settle_until else self.SEEK_THRESHOLD
		if abs(drift) > threshold:
			if now - self.last_seek > 0.7:
				e.seek(max(0.0, target + e.output_latency + 0.02))
				e.nudge = 1.0
				self.last_seek = now
				self._log(now, f"jumped {drift * 1000:+.0f} ms to catch up with VLC")
			return
		# Trim the tempo only when clearly off (dead zone with hysteresis): the clock
		# estimate itself wobbles by +-10-30 ms, and chasing that kept the engine off its
		# exact path (speed 1, no resampling) most of the time. The integral part learns
		# a steady speed difference (e.g. VLC's x1.2 is really ~x1.2033) and stays on.
		if self.trimming and abs(drift) < self.TRIM_OFF:
			self.trimming = False
			self._log(now, f"back in sync (drift {drift * 1000:+.0f} ms), tempo exact again")
		elif not self.trimming and abs(drift) > self.TRIM_ON:
			self.trimming = True
			self._log(now, f"trimming the tempo: audio is {abs(drift) * 1000:.0f} ms "
			               f"{'behind' if drift > 0 else 'ahead of'} the video")
		if self.trimming:
			self.integral = float(np.clip(self.integral + drift * dt, -0.2, 0.2))
		trim = float(np.clip(self.INTEGRAL * self.integral + (self.GAIN * drift if self.trimming else 0.0),
		                     -self.MAX_NUDGE, self.MAX_NUDGE))
		e.nudge = 1.0 if abs(trim) < 0.0005 else 1.0 + trim
