"""
Prototype: VLC shows the video, engine.py plays the audio, and a sync loop keeps the
two together. A small web page (default http://localhost:5055) has the controls.

    .venv/bin/python prototype/audio_mixer/proto.py "<song file>"        # try it by ear
    .venv/bin/python prototype/audio_mixer/proto.py "<song file>" --selftest   # measure

Stop OpenHomeKaraoke first: this starts its own VLC. The song needs split tracks in
<song dir>/nonvocal/ and <song dir>/vocal/ (the DNN splitter's output).

Sync, in short. VLC only refreshes its clock about every 250 ms, so a single reading
can be up to a quarter second old. But each refresh is exact at the moment it happens,
so over a 2 s window the reading furthest ahead of the wall clock is the freshest:
ClockModel keeps that as "VLC time = rate * wall + offset" (about +-10 ms). The engine
then follows: small differences are removed by nudging its tempo up to 2% (WSOLA keeps
the pitch, so a nudge is inaudible), large ones (a seek) by jumping, with a fade.
"""
import argparse, collections, json, os, shutil, statistics, subprocess, sys, threading, time
import base64, urllib.parse, urllib.request

import numpy as np
from flask import Flask, jsonify, request, Response

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..'))
import engine as E
from lib.vlcclient import get_default_vlc_path, find_vlc_http_dir


class VLCVideo:
	"""VLC playing the video only (--no-audio), controlled over its HTTP interface."""

	def __init__(self, song, port = 5098, headless = False, tmp = '/tmp/ohk-proto'):
		self.path = get_default_vlc_path('osx' if sys.platform == 'darwin' else 'linux')
		self.http = os.path.join(tmp, 'vlc-http')
		shutil.rmtree(self.http, ignore_errors = True)
		shutil.copytree(find_vlc_http_dir(self.path), self.http)
		shutil.copyfile(os.path.join(HERE, 'vlc_clock.json'), os.path.join(self.http, 'requests', 'clock.json'))
		self.base = f'http://127.0.0.1:{port}/requests/'
		self.auth = {'Authorization': 'Basic ' + base64.b64encode(b':ohk').decode()}
		cmd = [self.path, '--no-audio', '--extraintf', 'http', '--http-host', '127.0.0.1', '--http-port', str(port),
		       '--http-password', 'ohk', '--http-src', self.http, '--start-paused', '--play-and-exit',
		       '--no-video-title', '--no-loop', '--no-repeat']
		if headless:
			cmd += ['--intf', 'dummy', '--vout', 'dummy']
		elif sys.platform == 'darwin':
			cmd += ['--no-macosx-interfacestyle', '--no-macosx-show-playback-buttons', '--video-on-top']
		self.proc = subprocess.Popen(cmd + [song], stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL)
		for _ in range(150):
			try:
				if self.clock()[2].get('state') == 'paused':
					return
			except Exception:
				pass
			time.sleep(0.1)
		raise RuntimeError('VLC did not start')

	def _get(self, page, **params):
		url = self.base + page + ('?' + urllib.parse.urlencode(params) if params else '')
		return urllib.request.urlopen(urllib.request.Request(url, headers = self.auth), timeout = 2).read()

	def clock(self):
		"""(request start, request end, {"state", "time_us", "rate"})"""
		t0 = time.perf_counter()
		d = json.loads(self._get('clock.json'))
		return t0, time.perf_counter(), d

	def command(self, cmd, **params):
		self._get('status.xml', command = cmd, **params)

	def alive(self):
		return self.proc.poll() is None

	def close(self):
		if self.alive():
			self.proc.terminate()


class ClockModel:
	"""VLC time as slope * wall + offset, from its coarsely refreshed readings.

	The slope is VLC's *measured* speed: its rate setting is not exact (x1.2 runs at
	about x1.2033, x0.8 at x0.7981), so it is fitted from the fresh readings (the first
	one after each refresh) once there are enough; until then the rate setting is used.
	"""
	WINDOW = 2.0             # s of readings for the offset (freshest one wins)
	FIT_WINDOW = 8.0         # s of fresh readings for the slope
	MIN_FIT = 3.0

	def __init__(self):
		self.samples = collections.deque()      # (wall, vlc time)
		self.fresh = collections.deque()        # first reading after each VLC refresh
		self.rate = 1.0                         # VLC's rate setting
		self.slope = 1.0
		self.state = None
		self.resets = 0

	def reset(self):
		self.samples.clear()
		self.fresh.clear()
		self.slope = self.rate
		self.resets += 1

	def add(self, t0, vlc_time, state, rate):
		if state != self.state or abs(rate - self.rate) > 1e-3:
			self.reset()                            # pause, resume, speed change: new line
		elif self.samples and state == 'playing':
			# A seek VLC did on its own. Readings can be up to ~0.5 s stale, so they may
			# legitimately trail the prediction, but never lead it.
			ahead = vlc_time - self.estimate(t0)
			if ahead > 0.15 or ahead < -1.0:
				self.reset()
		self.state, self.rate = state, rate
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

	def span(self):
		return self.samples[-1][0] - self.samples[0][0] if self.samples else 0

	def estimate(self, at):
		if not self.samples:
			return None
		if self.state != 'playing':
			return self.samples[-1][1]
		offset = max(v - self.slope * t for t, v in self.samples)   # the freshest reading wins
		return self.slope * at + offset


class Player:
	SEEK_THRESHOLD = 0.15    # s: larger differences jump, smaller ones are nudged away
	SETTLE_TIME = 3.0        # s after a resume / seek / speed change ...
	SETTLE_THRESHOLD = 0.04  # ... during which differences over 40 ms jump right away
	GAIN = 0.25              # tempo trim per second of drift (0.1 s -> 2.5%)
	INTEGRAL = 0.05          # learns a steady speed difference, so no offset remains
	MAX_NUDGE = 0.02

	def __init__(self, song, headless = False, device = None):
		d, name = os.path.dirname(song), os.path.basename(song)
		self.song = song
		self.engine = E.Engine(os.path.join(d, 'nonvocal', name + '.m4a'), os.path.join(d, 'vocal', name + '.m4a'), device = device)
		# A video that is already a karaoke (instrumental) version splits into a silent
		# vocal track: the slider would seem to do nothing at the vocals end
		level = lambda x: 20 * np.log10(np.sqrt(np.mean(x[::50] ** 2)) + 1e-12)
		self.vocals_vs_music_db = level(self.engine.tracks[1]) - level(self.engine.tracks[0])
		self.warning = None
		if self.vocals_vs_music_db < -30:
			self.warning = (f'This song has almost no vocals ({self.vocals_vs_music_db:.0f} dB below the music): '
			                f'its video is probably already an instrumental/karaoke version.')
			print('WARNING:', self.warning)
		self.vlc = VLCVideo(song, headless = headless)
		self.model = ClockModel()
		self.audio_delay = 0.0
		self.speed = 1.0
		self.drift = None
		self.last_seek = 0
		self.integral = 0.0
		self.model_resets = 0
		self.settle_until = 0
		self.log = []                                # (wall, vlc estimate, engine position, drift, nudge)
		self.lock = threading.Lock()
		self.running = True
		self.engine.start()
		self.playing = False
		threading.Thread(target = self._poll, daemon = True).start()

	# -------------------------------------------------------------- controls

	def play(self):
		with self.lock:
			self.vlc.command('pl_forceresume')
			self.engine.play()
			self.playing = True

	def pause(self):
		with self.lock:
			self.vlc.command('pl_forcepause')
			self.engine.pause()
			self.playing = False

	def seek(self, seconds):
		with self.lock:
			self.vlc.command('seek', val = int(seconds))
			self.engine.seek(seconds)
			self.model.reset()                      # the old line is void
			self.last_seek = time.perf_counter()

	def set_speed(self, speed):
		with self.lock:
			self.speed = float(speed)
			self.integral = 0.0                     # the speed error it learned was for the old rate
			self.vlc.command('rate', val = self.speed)
			self.engine.speed = self.speed

	def close(self):
		self.running = False
		time.sleep(0.2)
		self.engine.close()
		self.vlc.close()

	# ------------------------------------------------------------------- sync

	def _poll(self):
		next_control = 0
		while self.running and self.vlc.alive():
			try:
				t0, t1, d = self.vlc.clock()
			except Exception:
				time.sleep(0.05)
				continue
			if 'time_us' in d:
				self.model.add(t0, d['time_us'] / 1e6, d['state'], float(d['rate']))
				# Our Play/Pause is the authority. VLC's macOS interface ignores
				# --start-paused and starts by itself; keep it paused until Play.
				if d['state'] == 'playing' and not self.playing:
					with self.lock:
						if not self.playing:
							self.vlc.command('pl_forcepause')
							if d['time_us'] > 1e6 and self.engine.audible_position() < 0.5:
								self.vlc.command('seek', val = 0)
			now = time.perf_counter()
			if now >= next_control:
				self._control(now)
				next_control = now + 0.1
			time.sleep(0.03)
		self.running = False

	def _control(self, now, dt = 0.1):
		e = self.engine
		if self.model.resets != self.model_resets:       # VLC resumed, seeked or changed speed
			self.model_resets = self.model.resets
			self.settle_until = now + 0.6 + self.SETTLE_TIME
		if not self.playing or self.model.state != 'playing' or self.model.span() < 0.6:
			e.nudge = 1.0
			return
		target = self.model.estimate(now) - self.audio_delay
		heard = e.audible_position(now)
		drift = target - heard
		self.drift = drift
		self.log.append((now, target, heard, drift, e.nudge))
		threshold = self.SETTLE_THRESHOLD if now < self.settle_until else self.SEEK_THRESHOLD
		if abs(drift) > threshold:
			if now - self.last_seek > 0.7:
				e.seek(target + e.output_latency + 0.02)   # lands where VLC will be when heard
				e.nudge = 1.0
				self.last_seek = now
			return
		self.integral = float(np.clip(self.integral + drift * dt, -0.2, 0.2))
		e.nudge = 1.0 + float(np.clip(self.GAIN * drift + self.INTEGRAL * self.integral, -self.MAX_NUDGE, self.MAX_NUDGE))

	def status(self):
		e = self.engine
		cost = list(e.callback_cost)
		return {
			'playing': self.playing, 'position': e.audible_position(), 'duration': e.duration,
			'blend': e.blend, 'semitones': e.semitones, 'speed': self.speed, 'volume': e.volume,
			'audio_delay_ms': self.audio_delay * 1000,
			'drift_ms': None if self.drift is None else round(self.drift * 1000, 1),
			'nudge_pct': round((e.nudge - 1) * 100, 2), 'underflows': e.underflows,
			'callback_ms': round(statistics.mean(cost) * 1000, 2) if cost else None,
			'output_latency_ms': round(e.output_latency * 1000, 1), 'vlc': self.model.state,
			'warning': self.warning,
		}


PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>Audio mixer prototype</title><style>
:root{--bg:#F4ECE1;--card:#FFFBF5;--text:#3A2D24;--muted:#6B5646;--accent:#8F6140;--border:#E5D8C5}
@media (prefers-color-scheme:dark){:root{--bg:#1C1511;--card:#2A2019;--text:#F3E9DC;--muted:#CDBBA7;--accent:#D9A877;--border:#463729}}
body{margin:0;background:var(--bg);color:var(--text);font:16px -apple-system,system-ui,sans-serif}
main{max-width:520px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:18px;margin-bottom:14px}
h1{font-size:1.3rem;margin:0 0 4px}.song{color:var(--muted);font-size:.9rem;margin-bottom:6px}
label{display:flex;justify-content:space-between;font-weight:600;margin:16px 0 6px}label span{color:var(--muted);font-weight:500}
input[type=range]{width:100%;accent-color:var(--accent)}
.ends{display:flex;justify-content:space-between;color:var(--muted);font-size:.8rem}
button{background:var(--accent);color:var(--card);border:0;border-radius:999px;padding:10px 22px;font:600 1rem inherit;cursor:pointer}
.row{display:flex;gap:10px;align-items:center}.stats{font:13px ui-monospace,Menlo,monospace;color:var(--muted);line-height:1.6}
</style></head><body><main>
<div class=card><h1>Audio mixer prototype</h1><div class=song id=song></div>
<div id=warn style="display:none;background:#E2B56333;border-radius:12px;padding:10px 12px;margin:8px 0;font-size:.9rem"></div>
<div class=row><button id=pp onclick="post('toggle')">Play</button><span id=time></span></div>
<input type=range id=seek min=0 max=100 step=0.1 onchange="post('seek',{t:this.value})"></div>
<div class=card>
<label>Vocals <span id=bl_v></span></label>
<input type=range id=blend min=-100 max=100 value=0 oninput="setv('blend',this.value/100,'bl_v')">
<div class=ends><span>Music only</span><span>Both</span><span>Vocals only</span></div>
<label>Key <span id=key_v></span></label><input type=range id=key min=-6 max=6 step=1 value=0 oninput="setv('semitones',this.value,'key_v')">
<label>Speed <span id=spd_v></span></label><input type=range id=spd min=0.75 max=1.25 step=0.05 value=1 oninput="setv('speed',this.value,'spd_v')">
<label>Volume <span id=vol_v></span></label><input type=range id=vol min=0 max=100 value=80 oninput="setv('volume',this.value/100,'vol_v')">
<label>Audio delay <span id=ad_v></span></label><input type=range id=ad min=-200 max=200 step=5 value=0 oninput="setv('audio_delay_ms',this.value,'ad_v')">
</div>
<div class="card stats" id=stats></div>
</main><script>
const fmt=s=>Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0');
const labels={blend:v=>v<0?Math.round((1+ +v)*100)+'% vocals':v>0?Math.round((1-v)*100)+'% music':'both',semitones:v=>(v>0?'+':'')+v,speed:v=>'x'+(+v).toFixed(2),volume:v=>Math.round(v*100)+'%',audio_delay_ms:v=>v+' ms'};
let pending={},timer=null;
function setv(k,v,id){document.getElementById(id).textContent=labels[k](v);pending[k]=v;if(!timer)timer=setTimeout(()=>{post('set',pending);pending={};timer=null},40)}
function post(p,q={}){return fetch('/api/'+p+'?'+new URLSearchParams(q),{method:'POST'})}
async function tick(){try{const s=await (await fetch('/api/status')).json();
document.getElementById('pp').textContent=s.playing?'Pause':'Play';
const w=document.getElementById('warn');if(s.warning){w.textContent=s.warning;w.style.display='block'}
document.getElementById('time').textContent=fmt(s.position)+' / '+fmt(s.duration);
const sk=document.getElementById('seek');sk.max=s.duration;if(document.activeElement!==sk)sk.value=s.position;
document.getElementById('stats').innerHTML=`sync: audio vs video ${s.drift_ms===null?'-':(s.drift_ms>0?'+':'')+s.drift_ms+' ms'} &middot; tempo trim ${s.nudge_pct}%<br>`+
`VLC ${s.vlc} &middot; output latency ${s.output_latency_ms} ms<br>audio callback ${s.callback_ms} ms &middot; dropouts ${s.underflows}`;}catch(e){}}
for(const [k,id,lid,f] of [['blend','blend','bl_v',v=>v/100],['semitones','key','key_v',v=>v],['speed','spd','spd_v',v=>v],['volume','vol','vol_v',v=>v/100],['audio_delay_ms','ad','ad_v',v=>v]])
 document.getElementById(lid).textContent=labels[k](f(document.getElementById(id).value));
document.getElementById('song').textContent=SONG;
post('set',{volume:document.getElementById('vol').value/100});   // the engine starts where the sliders are
setInterval(tick,250);tick();
</script></body></html>"""


def serve(player, port):
	app = Flask(__name__)
	song = json.dumps(os.path.basename(player.song).split('---')[0])

	@app.route('/')
	def index():
		return Response(PAGE.replace('SONG;', song + ';'), mimetype = 'text/html')

	@app.route('/api/status')
	def status():
		return jsonify(player.status())

	@app.route('/api/toggle', methods = ['POST'])
	def toggle():
		player.pause() if player.playing else player.play()
		return ''

	@app.route('/api/seek', methods = ['POST'])
	def seek():
		player.seek(float(request.args['t']))
		return ''

	@app.route('/api/set', methods = ['POST'])
	def set_values():
		a, e = request.args, player.engine
		if 'blend' in a: e.blend = float(a['blend'])
		if 'semitones' in a: e.semitones = float(a['semitones'])
		if 'volume' in a: e.volume = float(a['volume'])
		if 'speed' in a: player.set_speed(a['speed'])
		if 'audio_delay_ms' in a: player.audio_delay = float(a['audio_delay_ms']) / 1000
		return ''

	import logging
	logging.getLogger('werkzeug').setLevel(logging.ERROR)
	app.run(host = '0.0.0.0', port = port, threaded = True)


def selftest(player):
	"""Scripted run with the volume at 0. Reports sync and glitch numbers."""
	e = player.engine
	e.volume = 0.0
	e.tap = []
	marks = {}
	def settle(label, seconds):
		marks[label] = (time.perf_counter(), time.perf_counter() + seconds)
		time.sleep(seconds)

	player.play();                                     settle('start (both released together)', 12)
	for b in np.linspace(0, -1, 40): e.blend = b; time.sleep(0.05)
	for b in np.linspace(-1, 1, 80): e.blend = b; time.sleep(0.05)
	e.blend = 0;                                       settle('after blend sweeps', 4)
	e.semitones = 3; time.sleep(2); e.semitones = -2;  settle('after key changes', 4)
	e.semitones = 0
	player.pause(); time.sleep(2); player.play();      settle('after pause 2 s / resume', 8)
	player.seek(90);                                   settle('after seek to 1:30', 8)
	player.set_speed(1.2);                             settle('after speed x1.2', 10)
	player.set_speed(1.0);                             settle('after speed back to x1.0', 10)
	player.pause()

	print('\nsync (engine audio vs VLC video clock), per phase:')
	log = np.array(player.log)
	for label, (a, b) in marks.items():
		rows = log[(log[:, 0] >= a) & (log[:, 0] <= b)]
		if not len(rows):
			print(f'  {label:34} no samples'); continue
		d = rows[:, 3] * 1000
		inside = rows[:, 0] > a + 3                    # after the first 3 s of each phase
		settled = d[inside] if inside.any() else d
		first_ok = next((r[0] - a for r in rows if abs(r[3]) < 0.02), None)
		print(f'  {label:34} settled: median {np.median(np.abs(settled)):5.1f} ms, worst {np.abs(settled).max():6.1f} ms'
		      f' | within 20 ms after {"-" if first_ok is None else f"{first_ok:.1f} s"}')
	out = np.concatenate(e.tap)[:, 0]
	silent_runs, run, pos = [], 0, 0                # (where it started in the output, length)
	for blk in e.tap:
		if np.abs(blk).max() < 1e-6: run += len(blk)
		elif run: silent_runs.append((pos - run, run)); run = 0
		pos += len(blk)
	cost = np.array(e.callback_cost) * 1000
	print(f'\naudio: {len(out) / E.SR:.0f} s rendered, dropouts (PortAudio underflows) {e.underflows}, '
	      f'callback {cost.mean():.2f} ms mean / {cost.max():.2f} ms max, output latency {e.output_latency * 1000:.1f} ms')
	print('silent stretches in the output (expected: the song\'s own silence, and the 2 s pause): ' +
	      ', '.join(f'{n / E.SR * 1000:.0f} ms at {at / E.SR:.1f} s' for at, n in silent_runs))


def main():
	ap = argparse.ArgumentParser(description = __doc__, formatter_class = argparse.RawDescriptionHelpFormatter)
	ap.add_argument('song')
	ap.add_argument('--port', type = int, default = 5055)
	ap.add_argument('--device', default = None, help = 'audio output device (name or index)')
	ap.add_argument('--selftest', action = 'store_true', help = 'scripted, silent run that prints sync numbers')
	args = ap.parse_args()
	player = Player(os.path.abspath(args.song), headless = args.selftest, device = args.device)
	try:
		if args.selftest:
			selftest(player)
		else:
			print(f'Open http://localhost:{args.port} (Ctrl-C to quit)')
			serve(player, args.port)
	finally:
		player.close()


if __name__ == '__main__':
	main()
