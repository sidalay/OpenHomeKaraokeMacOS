"""Drives a running OpenHomeKaraoke through its web routes and measures the audio engine.

Start the app nearly muted first, then run this (it mutes fully once a song plays):

    openkaraoke -w -v 1
    .venv/bin/python tests/live_engine_check.py "<song with vocals>" "<second song>"

Prints how far the engine's audio is from VLC's video after each kind of event.
"""
import json, os, subprocess, sys, threading, time, urllib.parse, urllib.request
import numpy as np

BASE = 'http://127.0.0.1:5000'
def get(path, **q):
	url = BASE + path + ('?' + urllib.parse.urlencode(q) if q else '')
	return urllib.request.urlopen(url, timeout = 30).read().decode()
def status():
	return json.loads(get('/audio_engine_status'))
def nowplaying():
	for _ in range(50):
		try: return json.loads(get('/nowplaying'))
		except Exception: time.sleep(0.2)

songs = [os.path.abspath(p) for p in sys.argv[1:3]]
for s in songs:
	get('/enqueue', song = s, user = 'test')
for _ in range(200):                                    # wait until the first song plays
	n = nowplaying()
	if n and n.get('now_playing') and status()['playing_this_song']: break
	time.sleep(0.1)
get('/vol/0')
vlc_args = subprocess.run(['pgrep', '-lf', 'VLC.*http-src'], capture_output = True, text = True).stdout
print(f"playing: {nowplaying()['now_playing']} | VLC started with --no-audio: {'--no-audio' in vlc_args}")
for _ in range(50):
	if status()['split_tracks']: break
	time.sleep(0.1)
print(f"split tracks loaded: {status()['split_tracks']}")

samples, marks, stop = [], [], False
def sampler():
	while not stop:
		try:
			s = status()
			if s['drift_ms'] is not None: samples.append((time.time(), s['drift_ms'], s['dropouts']))
		except Exception: pass
		time.sleep(0.1)
threading.Thread(target = sampler, daemon = True).start()
def phase(name, seconds):
	marks.append((name, time.time(), time.time() + seconds)); time.sleep(seconds)

phase('start', 8)
for b in list(np.linspace(0, -1, 20)) + list(np.linspace(-1, 1, 40)) + list(np.linspace(1, 0, 20)):
	get(f'/vocal_blend/{b:.3f}'); time.sleep(0.05)
phase('after vocal slider sweeps', 4)
n = nowplaying(); print(f"status to phones: audio_engine={n['audio_engine']} vocal_blend={n['vocal_blend']}")
get('/transpose/3'); time.sleep(2); get('/transpose/-2'); phase('after key changes', 4); get('/transpose/0')
get('/pause'); time.sleep(2); get('/pause'); phase('after pause 2 s / resume', 8)
get('/seek/90'); phase('after seek to 1:30', 8)
get('/play_speed/1.2'); phase('after speed x1.2', 10)
get('/play_speed/1'); phase('after speed back to x1.0', 8)
get('/restart'); phase('after restart', 8)
get('/skip')
for _ in range(150):
	n = nowplaying()
	if n and n.get('now_playing') and not n['now_playing'].startswith(os.path.basename(songs[0])[:10]) and status()['playing_this_song']: break
	time.sleep(0.1)
get('/vol/0')
phase('next song', 10)
stop = True
print(f"\nnext song: {nowplaying()['now_playing']} | engine: {status()['playing_this_song']} | split: {status()['split_tracks']}")
print('\nsync (engine audio vs VLC video), per phase, after its first 3 s:')
arr = np.array(samples)
for name, a, b in marks:
	rows = arr[(arr[:, 0] >= a) & (arr[:, 0] <= b)] if len(arr) else arr
	settled = rows[rows[:, 0] > a + 3][:, 1] if len(rows) else []
	first = next((r[0] - a for r in rows if abs(r[1]) < 20), None)
	if len(settled):
		print(f"  {name:28} median {np.median(np.abs(settled)):5.1f} ms, worst {np.abs(settled).max():6.1f} ms"
		      f" | within 20 ms after {'-' if first is None else f'{first:.1f} s'}")
	else:
		print(f"  {name:28} no samples")
print(f"\ndropouts (audio underflows) on the current song: {status()['dropouts']}, "
      f"callback {status()['callback_ms_mean']} ms, output latency {status()['output_latency_ms']} ms")
