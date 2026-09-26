import os, sys, io, random, time, json, datetime
import logging, socket, subprocess, threading
import multiprocessing as mp
import shutil, psutil, traceback, tarfile, requests
from subprocess import check_output
from collections import *

import numpy as np

from constants import media_types

import pygame
import qrcode
import arabic_reshaper
from bidi.algorithm import get_display
from unidecode import unidecode
from flask import request
from lib import omxclient, vlcclient
from lib.get_platform import *
from lib.NLP import *
from lib import audio_engine
from app import getString

if get_platform() != "windows":
	from signal import SIGALRM, alarm, signal, SIGTERM
	signal(SIGTERM, lambda signum, stack_frame: os.K.stop())

STD_VOL = 65536/8/np.sqrt(2)
ip2websock, ip2pane = {}, {}

ws_send = lambda ip, msg: ip2websock[ip].send(msg) if ip in ip2websock else None

def flash(message: str, category: str = "message", client_ip = ''):
	ws_send(client_ip or request.remote_addr, f'showNotification("{message}", "{category}")')

def cleanse_modules(name):
	try:
		for module_name in sorted(sys.modules.keys()):
			if module_name.startswith(name):
				del sys.modules[module_name]
		del globals()[name]
	except:
		pass


class Karaoke:
	ref_W, ref_H = 1920, 1080      # reference screen size, control drawing scale

	queue = []
	queue_json = ''
	available_songs = []
	rename_history = {}
	songname_trans = {} # transliteration is used for sorting and initial letter search
	now_playing = None
	now_playing_filename = None
	now_playing_user = None
	now_playing_transpose = 0
	now_playing_slave = ''
	playing_bundle = None       # the combined file VLC is playing, see make_bundle()
	_bundle_cache = None
	use_engine = False          # play song audio through lib/audio_engine.py (VLC shows video)
	engine_error = ''           # why the engine is unavailable, if it is
	engine = None               # the current song's audio_engine.Engine
	av_sync = None              # keeps it on VLC's clock
	vocal_blend = 0.0           # -1 instrumental only, 0 original recording, +1 vocals only
	_split_loading = False
	audio_delay = 0
	has_video = True
	has_subtitle = False
	subtitle_delay = 0
	play_speed = 1.0
	show_subtitle = True
	last_vocal_info = 0
	last_vocal_time = 0
	use_DNN_vocal = True
	vocal_process = None
	vocal_device = None
	vocal_mode = 'mixed'
	is_paused = True
	firstSongStarted = False
	switchingSong = False
	qr_code_path = None
	base_path = os.path.dirname(__file__)
	volume_offset = 0
	default_logo_path = os.path.join(base_path, "logo.jpg")
	logical_volume = None   # for normalized volume
	status_dirty = True
	event_dirty = threading.Event()

	def __init__(self, args):

		# override with supplied constructor args if provided
		self.__dict__.update(args.__dict__)
		self.omxplayer_adev = 'both'
		self.download_path = args.dl_path
		self.volume_offset = self.volume = args.volume
		self.logo_path = self.default_logo_path if args.logo_path == None else args.logo_path

		# other initializations
		self.platform = get_platform()
		self.vlcclient = None
		self.omxclient = None
		self.screen = None
		# must exist even when the splash screen is disabled: the osx player path reads it
		self.full_screen = not args.windowed
		# Set by the web UI (see /toggle_fullscreen in app.py) and consumed by handle_run_loop,
		# because pygame's display calls must run on the main thread (required on macOS)
		self.fullscreen_request = False
		self.player_state = {}
		self.downloading_songs = {}
		self.log_level = int(args.log_level)

		logging.basicConfig(
			format = "[%(asctime)s] %(levelname)s: %(message)s",
			datefmt = "%Y-%m-%d %H:%M:%S",
			level = self.log_level,
			force = True,	# anything that logged during startup must not win over -l
		)

		logging.debug(vars(args))

		if self.save_delays:
			self.init_save_delays()

		# Generate connection URL and QR code, retry in case pi is still starting up
		# and doesn't have an IP yet (occurs when launched from /etc/rc.local)
		end_time = int(time.time()) + 30

		if self.platform == "raspberry_pi":
			while int(time.time()) < end_time:
				addresses_str = check_output(["hostname", "-I"]).strip().decode("utf-8")
				addresses = addresses_str.split(" ")
				self.ip = addresses[0]
				if not self.is_network_connected():
					logging.debug("Couldn't get IP, retrying....")
				else:
					break
		else:
			self.ip = self.get_ip()

		logging.debug("IP address (for QR code and splash screen): " + self.ip)

		self.url = "%s://%s:%s" % (('https' if self.ssl else 'http'), self.ip, self.port)

		# get songs from download_path
		self.get_available_songs()
		self.get_youtubedl_version()
		self.song2vol = Try(lambda: json.load(Open(self.download_path+'/.mp3_volume.json.gz')), {})
		
		# Automatically upgrade yt-dlp if using pip
		if not args.youtubedl_path:
			threading.Thread(target=self._upgrade_yt_dlp).start()

		# clean up old sessions
		self.kill_player()

		self.generate_qr_code()
		if self.use_vlc:
			self.vlcclient = vlcclient.VLCClient(port = self.vlc_port, path = self.vlc_path,
			                                     qrcode = (self.qr_code_path if self.show_overlay else None), url = self.url)
			if self.platform == "osx":
				vlcclient.forget_playback_positions(self.download_path)
			self.init_audio_engine(getattr(args, 'audio_engine', 'auto'))
		else:
			self.omxclient = omxclient.OMXClient(path = self.omxplayer_path, adev = self.omxplayer_adev,
			                                     dual_screen = self.dual_screen, volume_offset = self.volume_offset)

		if not self.hide_splash_screen:
			self.initialize_screen(not args.windowed)
			self.render_splash_screen()

		self.cloud = args.cloud
		if args.cloud:
			self.cloud_trigger = threading.Event()
			self.cloud_tasks = []
			threading.Thread(target=self._cloud_thread).start()

	def _upgrade_yt_dlp(self):
		import yt_dlp
		fn = '.yt-dlp.last-update'
		date_today = datetime.datetime.today().isoformat()[:10]
		date_last = Try(lambda: open(fn).read().strip(), '')
		if date_today == date_last:
			logging.info(f"yt-dlp is up-to-date at {date_today}")
			return

		self.upgrade_youtubedl()
		self.get_youtubedl_version()
		with open(fn, 'w') as fp:
			print(date_today, file=fp)


	def _cloud_thread(self):
		while True:
			self.cloud_trigger.wait()
			self.cloud_trigger.clear()
			if not self.running: return
			while self.cloud_tasks:
				try:
					fn = self.cloud_tasks.pop(0)
					bn, dn = os.path.basename(fn), os.path.dirname(fn)
					if os.path.isfile(f'{self.download_path}nonvocal/{bn}.m4a') and os.path.isfile(f'{self.download_path}vocal/{bn}.m4a'):
						continue
					os.system(f'ffmpeg -y -i "{fn}" -vn -c copy {self.tmp_dir}/input.m4a')
					with open(f'{self.tmp_dir}/input.m4a', 'rb') as f:
						r = requests.post(self.cloud+'/split_vocal', files={'file': f})
					with open(f'{self.tmp_dir}/output.tar.gz', 'wb') as f:
						f.write(r.content)
					with tarfile.open(f'{self.tmp_dir}/output.tar.gz') as tar:
						tar.extract('nonvocal.m4a', f'{self.download_path}nonvocal')
						os.rename(f'{self.download_path}nonvocal/nonvocal.m4a', f'{self.download_path}nonvocal/{bn}.m4a')
						tar.extract('vocal.m4a', f'{self.download_path}vocal')
						os.rename(f'{self.download_path}vocal/vocal.m4a', f'{self.download_path}vocal/{bn}.m4a')
				except:
					traceback.print_exc()


	# Other ip-getting methods are unreliable and sometimes return 127.0.0.1
	# https://stackoverflow.com/a/28950776
	def get_ip(self):
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		try:
			# doesn't even have to be reachable
			s.connect(("8.8.8.8", 1))
			IP = s.getsockname()[0]
		except Exception:
			IP = "127.0.0.1"
		finally:
			s.close()
		return IP

	def get_youtubedl_version(self):
		self.youtubedl_version = self.call_yt_dlp(['--version'], True).strip()
		return self.youtubedl_version

	def upgrade_youtubedl(self):
		logging.info("Upgrading youtube-dl, current version: %s" % self.youtubedl_version)
		if self.youtubedl_path:
			self.call_yt_dlp(['-U'])
		else:
			try:
				# pip.main() is unsupported in-process and dumps a wall of DEPRECATION
				# warnings into our log: run pip as the separate process it expects to be.
				subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'],
				               stdout = subprocess.DEVNULL, stderr = subprocess.DEVNULL, check = True)
				cleanse_modules('yt_dlp')
				import yt_dlp
			except:
				pass
		logging.info("Done. New version: %s" % self.get_youtubedl_version())

	def is_network_connected(self):
		return not len(self.ip) < 7

	def generate_qr_code(self):
		logging.debug("Generating URL QR code")
		qr = qrcode.QRCode(version = 1, box_size = 1, border = 4, error_correction = qrcode.constants.ERROR_CORRECT_H)
		qr.add_data(self.url)
		qr.make()
		img = qr.make_image()
		self.qr_code_path = os.path.join(self.base_path, "qrcode.png")
		img.save(self.qr_code_path)

	def get_default_display_mode(self):
		if self.use_vlc:
			if self.platform == "raspberry_pi":
				# HACK apparently if display mode is fullscreen the vlc window will be at the bottom of pygame
				os.environ["SDL_VIDEO_CENTERED"] = "1"
				return pygame.NOFRAME
			else:
				return pygame.FULLSCREEN
		else:
			return pygame.FULLSCREEN

	def initialize_screen(self, fullscreen=True):
		if not self.hide_splash_screen:
			logging.debug("Initializing pygame")
			pygame.init()
			pygame.display.set_caption("pikaraoke")
			pygame.mouse.set_visible(0)
			self.fonts = {}
			self.WIDTH = pygame.display.Info().current_w
			self.HEIGHT = pygame.display.Info().current_h
			logging.debug("Initializing screen mode")

			if self.platform != "raspberry_pi":
				self.toggle_full_screen(fullscreen)
			else:
				# this section is an unbelievable nasty hack - for some reason Pygame
				# needs a keyboardinterrupt to initialise in some limited circumstances
				# source: https://stackoverflow.com/questions/17035699/pygame-requires-keyboard-interrupt-to-init-display
				class Alarm(Exception):
					pass

				def alarm_handler(signum, frame):
					raise Alarm

				signal(SIGALRM, alarm_handler)
				alarm(3)
				try:
					self.toggle_full_screen(fullscreen)
					alarm(0)
				except Alarm:
					raise KeyboardInterrupt
			logging.debug("Done initializing splash screen")

	def toggle_full_screen(self, fullscreen=None):
		if not self.hide_splash_screen:
			self.full_screen = not self.full_screen if fullscreen is None else fullscreen
			logging.debug("Toggling fullscreen -> %s" % self.full_screen)
			if self.full_screen:
				self.screen = pygame.display.set_mode([self.WIDTH, self.HEIGHT], self.get_default_display_mode())
			else:
				self.screen = pygame.display.set_mode([self.WIDTH*3//4, self.HEIGHT*3//4], pygame.RESIZABLE)
			if self.is_file_playing():
				self.play_transposed(self.now_playing_transpose)
			else:
				self.render_splash_screen()

	def normalize(self, v):
		r = self.screen.get_width()/self.ref_W
		if type(v) is list:
			return [i*r for i in v]
		elif type(v) is tuple:
			return tuple(i * r for i in v)
		return v*r

	def render_splash_screen(self):
		if self.hide_splash_screen:
			return

		# Clear the screen and start
		logging.debug("Rendering splash screen")
		self.screen.fill((0, 0, 0))
		blitY = self.ref_W*self.screen.get_height()//self.screen.get_width() - 40
		sysfont_size = 30

		# Draw the logo, centred (no name under it)
		if not hasattr(self, 'logo'):
			self.logo = pygame.image.load(self.logo_path)
		_, _, W, H = self.normalize(list(self.logo.get_rect()))
		W, H = W/2, H/2
		center = self.screen.get_rect().center
		self.logo1 = pygame.transform.scale(self.logo, (W, H))
		self.screen.blit(self.logo1, (center[0]-W/2, center[1]-H/2))

		if not self.hide_ip:
			qr_size = 150
			if not hasattr(self, 'p_image'):
				self.p_image = pygame.image.load(self.qr_code_path)
			self.p_image1 = pygame.transform.scale(self.p_image, self.normalize((qr_size, qr_size)))
			self.screen.blit(self.p_image1, self.normalize((20, blitY - 125)))
			if not self.is_network_connected():
				text = self.render_font(sysfont_size, getString(48), (255, 255, 255))
				self.screen.blit(text[0], self.normalize((qr_size + 35, blitY)))
				time.sleep(10)
				logging.info("No IP found. Network/Wifi configuration required. For wifi config, try: sudo raspi-config or the desktop GUI: startx")
				self.stop()
			else:
				text = self.render_font(sysfont_size, getString(49) + self.url, (255, 255, 255))
				self.screen.blit(text[0], self.normalize((qr_size + 35, blitY)))
				# Windows and Mac-OS should use screen projection and AirPlay
				if self.streamer_alive():
					text = self.render_font(sysfont_size, getString(50) + self.url.rsplit(":", 1)[0] + ":4000", (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 40)))
				if not self.firstSongStarted:
					text = self.render_font(sysfont_size, getString(51), (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 120)))
					text = self.render_font(sysfont_size, getString(52), (255, 255, 255))
					self.screen.blit(text[0], self.normalize((qr_size + 35, blitY - 80)))

		blitY = 10
		if not self.has_video:
			logging.debug("Rendering current song to splash screen")
			render_next_song = self.render_font([60, 50, 40], getString(58) + (self.now_playing or ''), (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], getString(57) + (self.now_playing_user or ''), (0, 240, 0))
			self.screen.blit(render_next_song[0], (self.screen.get_width() - render_next_song[1].width - 10, self.normalize(10)))
			self.screen.blit(render_next_user[0], (self.screen.get_width() - render_next_user[1].width - 10, self.normalize(80)))
			blitY += 140

		if len(self.queue) >= 1:
			logging.debug("Rendering next song to splash screen")
			next_song = self.queue[0]["title"]
			next_user = self.queue[0]["user"]
			render_next_song = self.render_font([60, 50, 40], getString(56) + next_song, (255, 255, 0))
			render_next_user = self.render_font([50, 40, 30], getString(57) + next_user, (0, 240, 0))
			self.screen.blit(render_next_song[0], (self.screen.get_width() - render_next_song[1].width - 10, self.normalize(blitY)))
			self.screen.blit(render_next_user[0], (self.screen.get_width() - render_next_user[1].width - 10, self.normalize(blitY+70)))
		elif not self.firstSongStarted:
			text1 = self.render_font(sysfont_size, getString(196) + ': ' + self.download_path, (255, 255, 0))
			self.screen.blit(text1[0], self.normalize((20, 20)))
			text2 = self.render_font(sysfont_size, getString(197) + ': %d'%len(self.available_songs), (255, 255, 0))
			self.screen.blit(text2[0], self.normalize((20, 30+sysfont_size)))

	def render_font(self, sizes, text, *kargs):
		if type(sizes) != list:
			sizes = [sizes]

		# normalize font size
		sizes = [s*self.screen.get_width()/self.ref_W for s in sizes]

		# initialize fonts if not found
		for size in sizes:
			if size not in self.fonts:
				self.fonts[size] = [pygame.freetype.SysFont(pygame.freetype.get_default_font(), size)] \
						+ [pygame.freetype.Font(f'font/{name}', size) for name in ['arial-unicode-ms.ttf', 'unifont.ttf']]

		# find a font that contains all characters of the song title, if cannot find, then display transliteration instead
		found = None
		for ii, font in enumerate(self.fonts[size]):
			if None not in font.get_metrics(text):
				found = ii
				break
		if found is None:
			text = unidecode(text)
			found = 0

		# reshape Arabic text
		text = get_display(arabic_reshaper.reshape(text))

		# draw the font, if too wide, half the string
		width = self.screen.get_width()
		for size in sorted(sizes, reverse = True):
			font = self.fonts[size][found]
			render = font.render(text, *kargs)
			# reduce font size if text too long
			if render[1].width > width and size != min(sizes):
				continue
			while render[1].width >= width:
				text = text[:int(len(text) * min(width / render[1].width, 0.618))] + '…'
				del render
				render = font.render(text, *kargs)
			break
		return render

	def get_js_runtime_opt(self):
		# yt-dlp needs a JavaScript runtime to solve YouTube's signature/n challenges;
		# without one it warns and some formats are missing. Only deno is enabled by
		# default, so point yt-dlp at whatever runtime is actually installed.
		if not hasattr(self, '_js_runtime_opt'):
			self._js_runtime_opt = []
			if not shutil.which('deno'):
				for runtime in ['node', 'bun', 'qjs']:
					if shutil.which(runtime):
						self._js_runtime_opt = ['--js-runtimes', runtime]
						logging.info(f"Using '{runtime}' as the JavaScript runtime for yt-dlp")
						break
				else:
					logging.warning("No JavaScript runtime (deno/node/bun) found: some YouTube formats may be "
					                "unavailable. Install one with e.g. 'brew install deno'")
		return self._js_runtime_opt

	def call_yt_dlp(self, argv, get_stdout = False):
		argv = self.get_js_runtime_opt() + argv
		if self.youtubedl_path:
			if get_stdout:
				return subprocess.check_output([self.youtubedl_path]+argv).decode("utf-8")
			else:
				return subprocess.call([self.youtubedl_path]+argv)
		ret_code = 0
		if get_stdout:
			old_stdout = sys.stdout
			sys.stdout = io.StringIO()
		try:
			import yt_dlp
			yt_dlp.main(argv)
		except SystemExit as e:
			ret_code = e.code
		if get_stdout:
			ret_stdout = sys.stdout
			sys.stdout = old_stdout
			return ret_stdout.getvalue()
		return ret_code

	def get_search_results(self, textToSearch):
		logging.info("Searching YouTube for: " + textToSearch)
		num_results = 10
		yt_search = 'ytsearch%d:%s' % (num_results, textToSearch)
		cmd = ["-j", "--no-playlist", "--flat-playlist", yt_search]
		logging.debug("Youtube-dl search command: " + " ".join(cmd))
		try:
			# output = subprocess.check_output(cmd).decode("utf-8")
			output = self.call_yt_dlp(cmd, True)
			logging.debug("Search results: " + output)
			rc = []
			for each in output.split("\n"):
				if len(each) > 2:
					j = json.loads(each)
					if (not "title" in j) or (not "url" in j):
						continue
					rc.append([j["title"], j["url"], j["id"], sec2hhmmss(j.get("duration")),
					           j.get("channel") or j.get("uploader") or ""])
			return rc
		except Exception as e:
			logging.debug("Error while executing search: " + str(e))
			raise e

	def get_yt_dlp_json(self, url):
		# out_json = subprocess.check_output([self.youtubedl_path, '-j', url])
		out_json = self.call_yt_dlp(['-j', url], True)
		return json.loads(out_json)

	def get_downloaded_file_basename(self, url):
		try:
			youtube_id = url.split("watch?v=")[1].split('&')[0]
		except:
			try:
				info_json = self.get_yt_dlp_json(url)
				youtube_id = info_json['id']
			except:
				logging.error("Error parsing video id from url: " + url)
				return None

		try:
			return [i for i in os.listdir(self.download_path+'tmp/') if youtube_id in i][0]
		except:
			pass

		filename = f"{info_json['title']}---{info_json['id']}.{info_json['ext']}"
		return filename if os.path.isfile(self.download_path+'tmp/'+filename) else None

	def download_video(self, client_lang='', client_ip='', song_url = '', enqueue = False, song_added_by = "Pikaraoke", include_subtitles = False, high_quality = False):
		logging.info("Downloading video: " + song_url)
		getString2 = lambda ii: os.langs.get(client_lang, os.langs['en_US'])[ii]
		self.downloading_songs[song_url] = 1
		dl_path = "%(title)s---%(id)s.%(ext)s"
		opt_quality = ['-f', 'bestvideo[height<=1080]+bestaudio[abr<=160]'] if high_quality else ['-f', 'mp4+m4a']
		opt_sub = ['--sub-langs', 'all', '--embed-subs'] if include_subtitles else []
		cmd = ['--fixup', 'force', '--socket-timeout', '3', '-R', 'infinite', '--remux-video', 'mp4'] + self.cookies_opt + opt_quality +\
		      ["-o", self.download_path+'tmp/'+dl_path] + opt_sub + [song_url]
		logging.info("Youtube-dl command: " + " ".join(cmd))
		rc = self.call_yt_dlp(cmd)
		if rc != 0:
			logging.error("Error code while downloading, retrying without format options ...")
			cmd = ["-o", self.download_path + 'tmp/' + dl_path] + opt_sub + [song_url]
			logging.debug("Youtube-dl command: " + " ".join(cmd))
			rc = self.call_yt_dlp(cmd)
		if rc == 0:
			logging.debug("Song successfully downloaded: " + song_url)
			self.downloading_songs[song_url] = 0
			bn = self.get_downloaded_file_basename(song_url)
			if bn:
				shutil.move(self.download_path+'tmp/'+bn, self.download_path+bn)
				self.get_available_songs()
				if enqueue:
					self.enqueue(self.download_path+bn, song_added_by)
					self.downloading_songs[song_url] = '00'
					flash(getString2(189)+' '+getString2(191), client_ip = client_ip)
				else:
					flash(getString2(189), client_ip = client_ip)
			else:
				logging.error("Error queueing song: " + song_url)
				self.downloading_songs[song_url] = '01'
				flash(getString2(189)+' '+getString2(192), client_ip = client_ip)
		else:
			logging.error("Error downloading song: " + song_url)
			self.downloading_songs[song_url] = -1
			flash(getString2(190), client_ip = client_ip)
		return ws_send(client_ip, 'download_ended()')

	def get_available_songs(self):
		logging.info("Fetching available songs in: " + self.download_path)
		files_grabbed = []
		self.songname_trans = {}
		for bn in os.listdir(self.download_path):
			fn = self.download_path + bn
			if not bn.startswith('.') and os.path.isfile(fn):
				if os.path.splitext(fn)[1].lower() in media_types:
					files_grabbed.append(fn)
					trans = unidecode(self.filename_from_path(fn)).lower()
					# strip leading non-transliterable symbols
					while trans and not trans[0].islower() and not trans[0].isdigit():
						trans = trans[1:]
					self.songname_trans[fn] = trans

		# self.available_songs = sorted(files_grabbed, key = lambda f: str.lower(os.path.basename(f)))
		self.available_songs = sorted(self.songname_trans, key = self.songname_trans.get)

	def get_all_assoc_files(self, song_path):
		basename = os.path.basename(song_path)
		basestem = os.path.splitext(basename)
		return [self.download_path + basename,
				self.download_path + basestem[0] + '.cdg',
				self.download_path + 'nonvocal/' + basename + '.m4a',
				self.download_path + 'nonvocal/.' + basename + '.m4a',
				self.download_path + 'vocal/' + basename + '.m4a',
				self.download_path + 'vocal/.' + basename + '.m4a']

	def delete_if_exist(self, filename):
		if os.path.isfile(filename):
			try:
				os.remove(filename)
			except:
				pass

	def delete(self, song_path):
		logging.info("Deleting song: " + song_path)

		# delete all associated cdg/vocal/nonvocal files if exist
		for fn in self.get_all_assoc_files(song_path):
			self.delete_if_exist(fn)

		self.get_available_songs()

	def rename_if_exist(self, old_path, new_path):
		if os.path.isfile(old_path):
			try:
				shutil.move(old_path, new_path)
			except:
				pass

	def rename(self, song_path, new_basestem):
		logging.info("Renaming song: '" + song_path + "' to: " + new_basestem)
		ext = os.path.splitext(song_path)
		if len(ext) < 2:
			ext += ['']
		new_basename = new_basestem + ext[1]

		# can handle the case while the file is being processed by vocal splitter, it has been renamed multiple times
		old_basename = os.path.basename(song_path)
		self.rename_history[old_basename] = new_basename
		for k, v in self.rename_history.items():
			if v == old_basename:
				self.rename_history[k] = new_basename

		# rename all associated cdg/vocal/nonvocal files if exist
		for src, tgt in zip(self.get_all_assoc_files(song_path), self.get_all_assoc_files(new_basename)):
			self.rename_if_exist(src, tgt)

		# rename queue entry if inside queue
		for item in self.queue:
			if item['file'] == song_path:
				item['file'] = self.download_path + new_basename
				item['title'] = self.filename_from_path(item['file'])
				break

		self.get_available_songs()

	def filename_from_path(self, file_path):
		rc = os.path.basename(file_path)
		rc = os.path.splitext(rc)[0]
		rc = rc.split("---")[0]  # removes youtube id if present
		return rc

	def kill_player(self):
		if self.use_vlc:
			logging.debug("Killing old VLC processes")
			if self.vlcclient != None:
				self.vlcclient.kill()
		elif self.omxclient != None:
				self.omxclient.kill()

	def play_file(self, file_path, extra_params = []):
		self.switchingSong = True
		if self.use_vlc:
			if self.save_delays:
				saved_delays = self.delays.get(os.path.basename(file_path), {})
				self.audio_delay = self.audio_delay if self.audio_delay else saved_delays.get('audio_delay', 0)
				self.subtitle_delay = self.subtitle_delay if self.subtitle_delay else saved_delays.get('subtitle_delay', 0)
				self.show_subtitle = False if self.show_subtitle==False else saved_delays.get('show_subtitle', True)
			extra_params1 = []
			logging.info("Playing video in VLC: " + file_path)
			if self.platform != 'osx':
				extra_params1 += ['--drawable-hwnd' if self.platform == 'windows' else '--drawable-xid',
				                  hex(pygame.display.get_wm_info()['window'])]
			self.now_playing_slave = self.try_set_vocal_mode(self.vocal_mode, file_path)
			self.stop_engine()
			if self.save_delays and 'vocal_blend' in saved_delays:
				self.vocal_blend = saved_delays['vocal_blend']
			if self.normalize_vol and self.logical_volume is not None:
				self.volume = min(self.logical_volume / np.sqrt(self.get_mp3_volume(file_path)), self.VOL_FULL)
			use_engine = self.start_engine(file_path)
			play_path = file_path
			self.playing_bundle = None if use_engine else self.make_bundle(file_path)
			if use_engine:
				# the engine plays the audio (and takes care of key and vocal level)
				extra_params1 += ['--no-audio']
			elif self.playing_bundle:
				play_path = self.playing_bundle['path']
				extra_params1 += [f'--audio-track={self.playing_bundle["tracks"].get(self.vocal_mode, 0)}']
			elif os.path.isfile(self.now_playing_slave):
				extra_params1 += [f'--input-slave={self.now_playing_slave}', '--audio-track=1']
			if self.audio_delay:
				extra_params1 += [f'--audio-desync={self.audio_delay * 1000}']
			if self.subtitle_delay:
				extra_params1 += [f'--sub-delay={self.subtitle_delay * 10}']
			if self.show_subtitle:
				extra_params1 += [f'--sub-track=0']
			if self.play_speed != 1:
				extra_params1 += [f'--rate={self.play_speed}']
			self.now_playing = self.filename_from_path(file_path)
			self.now_playing_filename = file_path
			self.is_paused = ('--start-paused' in extra_params1) or ('--start-paused' in extra_params)
			if use_engine:
				# volume 0: VLC has no audio output, so don't wait for it to report a volume
				xml = self.vlcclient.play_file(play_path, 0, extra_params + extra_params1)
			# With live control the pitch filter is always loaded (it is transparent at 0
			# semitones), so the pitch can later be changed without a restart
			elif self.now_playing_transpose == 0 and not self.vlcclient.live_control:
				xml = self.vlcclient.play_file(play_path, self.volume, extra_params + extra_params1)
			else:
				xml = self.vlcclient.play_file_transpose(play_path, self.now_playing_transpose, self.volume, extra_params + extra_params1)
			xml = xml or ''
			self.has_subtitle = "<info name='Type'>Subtitle</info>" in xml
			self.has_video = "<info name='Type'>Video</info>" in xml
			if use_engine:
				self.av_sync = audio_engine.AVSync(self.engine, self.vlcclient.clock)
				self.av_sync.audio_delay = self.audio_delay
				self.av_sync.start()
			elif xml:
				self.volume = round(float(self.vlcclient.get_val_xml(xml, 'volume')))
				if self.volume > self.VOL_FULL:             # VLC's saved volume was boosted: cap at 100%
					self.vlcclient.vol_set(self.VOL_FULL)
					self.volume = self.VOL_FULL
			if self.normalize_vol:
				self.media_vol = self.get_mp3_volume(self.now_playing_filename)
				self.logical_volume = self.volume * np.sqrt(self.media_vol)
		else:
			logging.info("Playing video in omxplayer: " + file_path)
			self.omxclient.play_file(file_path)

		self.switchingSong = False
		self.status_dirty = True
		self.render_splash_screen()  # remove old previous track

	# ---------------------------------------------------------------- audio engine

	# video containers whose audio the engine plays (audio-only songs, e.g. mp3+cdg zips,
	# keep VLC's audio: VLC with --no-audio would have nothing to show and no clock)
	ENGINE_EXTS = ('.mp4', '.m4v', '.mkv', '.webm', '.mov', '.avi', '.flv')

	def init_audio_engine(self, setting):
		"""setting: 'auto' (use it when possible), 'on' or 'off' (--audio-engine)."""
		ok, why = audio_engine.available()
		if ok and not self.vlcclient.live_control:
			ok, why = False, "VLC live control is unavailable (the engine follows VLC's clock through it)"
		self.engine_error = '' if ok else why
		self.use_engine = ok and setting != 'off'
		if self.use_engine:
			# -v arrives as a string. The start volume normally comes from VLC's saved
			# volume, which the engine never sees (VLC has no audio), so when none is
			# given start at 100% (256 in VLC's units).
			try:
				self.volume = float(self.volume)
			except (TypeError, ValueError):
				self.volume = 0
			if self.volume <= 0:
				self.volume = 256
		if setting == 'on' and not ok:
			logging.warning(f"--audio-engine on, but the engine cannot run: {why}. Using VLC's audio.")
		logging.info(f"Song audio: {'audio engine (smooth vocal slider)' if self.use_engine else 'VLC'}"
		             + (f" ({why})" if why else ''))

	def set_audio_engine(self, on):
		"""Switch between the engine and VLC's own audio; the current song continues."""
		on = bool(on) and not self.engine_error
		if on == self.use_engine:
			return self.use_engine
		self.use_engine = on
		logging.info(f"Song audio switched to {'the audio engine' if on else 'VLC'}")
		if self.is_file_playing() and self.now_playing_filename:
			self.relaunch()
		return self.use_engine

	def start_engine(self, file_path):
		"""Load `file_path`'s audio into a new engine. False (use VLC's audio) if not possible."""
		if not self.use_engine or os.path.splitext(file_path)[1].lower() not in self.ENGINE_EXTS:
			return False
		try:
			engine = audio_engine.Engine(file_path)
		except Exception as e:
			logging.warning(f"Audio engine cannot play {os.path.basename(file_path)}, using VLC's audio: {e}")
			return False
		engine.volume = self.volume / 256.0          # VLC volume units: 256 = 100%
		engine.semitones = float(self.now_playing_transpose)
		engine.speed = float(self.play_speed)
		engine.blend = self.vocal_blend
		try:
			engine.start()
		except Exception as e:
			logging.warning(f"Audio engine cannot open the audio output, using VLC's audio: {e}")
			engine.close()
			return False
		self.engine = engine
		self.load_split_tracks(file_path)
		return True

	def stop_engine(self):
		if self.av_sync:
			self.av_sync.stop()
			self.av_sync = None
		if self.engine:
			self.engine.close()
			self.engine = None

	def split_track_paths(self, file_path):
		prefix = '' if self.use_DNN_vocal else '.'
		name = os.path.basename(file_path)
		paths = [f'{self.download_path}{mode}/{prefix}{name}.m4a' for mode in ('nonvocal', 'vocal')]
		return paths if all(os.path.isfile(p) for p in paths) else None

	def load_split_tracks(self, file_path):
		"""Give the engine the split tracks in the background (the song is already playing
		its original audio; the vocal slider takes effect once they are in)."""
		engine, paths = self.engine, self.split_track_paths(file_path)
		if not engine or not paths or self._split_loading:
			return
		self._split_loading = True
		def load():
			try:
				engine.add_split(*paths)
				logging.info(f"Audio engine: split tracks loaded {engine.split_info}")
				self.status_dirty = True
			except Exception as e:
				logging.warning(f"Audio engine could not load the split tracks: {e}")
			finally:
				self._split_loading = False
		threading.Thread(target = load, daemon = True).start()

	def set_vocal_blend(self, value):
		"""-1 instrumental only, 0 original recording, +1 vocals only (any value between)."""
		self.vocal_blend = float(np.clip(float(value), -1, 1))
		if self.engine:
			self.engine.blend = self.vocal_blend
		if self.save_delays and self.now_playing_filename:
			self.set_delays_dict(self.now_playing_filename, 'vocal_blend', self.vocal_blend, 0.0)
		self.status_dirty = True
		return self.vocal_blend

	def relaunch(self, force_paused = None):
		"""Restart the current song where it is (to apply what VLC only reads at launch)."""
		status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
		info = self.vlcclient.get_info_xml(status_xml)
		posi = info['position'] * info['length']
		paused = self.is_paused if force_paused is None else force_paused
		self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if paused else []))

	# Song containers that can be remuxed together with their split tracks
	BUNDLE_EXTS = ('.mp4', '.m4v', '.mkv', '.webm', '.mov')

	def make_bundle(self, file_path):
		"""Remux a song with its split instrumental/vocal tracks into one temporary file.

		VLC switches between the audio tracks of one file live, like the languages of a
		movie. It cannot do that for a separate --input-slave file: the new track stays
		silent until a seek, and VLC 3 seeks land on the previous keyframe, a second or
		two back. So with everything in one file, Music/Mixed/Voice switches without a
		restart or a jump. Stream copy only (~0.1s). Returns None if not possible, and
		play_file then falls back to --input-slave.
		"""
		if not self.vlcclient.live_control or os.path.splitext(file_path)[1].lower() not in self.BUNDLE_EXTS:
			return None
		prefix = '' if self.use_DNN_vocal else '.'
		name = os.path.basename(file_path)
		parts = [(mode, f'{self.download_path}{mode}/{prefix}{name}.m4a') for mode in ('nonvocal', 'vocal')]
		parts = [(mode, path) for mode, path in parts if os.path.isfile(path)]
		if not parts:
			return None
		key = (file_path, self.use_DNN_vocal, tuple((path, os.path.getmtime(path)) for _, path in parts))
		cached = self._bundle_cache
		if cached and cached['key'] == key and os.path.isfile(cached['path']):
			return cached
		os.makedirs(self.vlcclient.tmp_dir, exist_ok = True)
		out = os.path.join(self.vlcclient.tmp_dir, 'bundle.mkv')
		part = os.path.join(self.vlcclient.tmp_dir, 'bundle.part.mkv')
		cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-i', file_path]
		for _, path in parts:
			cmd += ['-i', path]
		cmd += ['-map', '0:v?', '-map', '0:a:0']
		for i in range(len(parts)):
			cmd += ['-map', f'{i + 1}:a:0']
		cmd += ['-map', '0:s?', '-c', 'copy']
		# Matroska cannot hold MP4's mov_text subtitles; retry converting them to SRT
		for extra in ([], ['-c:s', 'srt']):
			try:
				r = subprocess.run(cmd + extra + [part], capture_output = True, timeout = 60)
				if r.returncode == 0:
					# rename, don't overwrite: a still-running VLC keeps reading the old file
					os.replace(part, out)
					self._bundle_cache = {'key': key, 'path': out, 'source': file_path, 'dnn': self.use_DNN_vocal,
					                      'tracks': dict([('mixed', 0)] + [(mode, i + 1) for i, (mode, _) in enumerate(parts)])}
					return self._bundle_cache
				error = r.stderr.decode('utf-8', 'ignore').strip()
			except Exception as e:
				error = str(e)
		logging.warning(f"Could not combine {name} with its split tracks, vocal changes will restart playback: {error}")
		return None

	def play_transposed(self, semitones):
		if self.engine:
			self.engine.semitones = float(semitones)
			self.now_playing_transpose = int(float(semitones))
			self.status_dirty = True
			return
		if self.use_vlc:
			# Live: retune the running player's pitch filter (no restart, no jump back)
			if self.vlcclient.set_pitch_live(semitones):
				self.now_playing_transpose = int(float(semitones))
				self.status_dirty = True
				return
			self.now_playing_transpose = semitones
			status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
			info = self.vlcclient.get_info_xml(status_xml)
			posi = info['position']*info['length']
			self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
		else:
			logging.error("Not using VLC. Can't transpose track.")

	def is_file_playing(self):
		client = self.vlcclient if self.use_vlc else self.omxclient
		if client is not None and client.is_running():
			return True
		elif self.now_playing_filename:
			self.now_playing = self.now_playing_filename = None
		return False

	def is_song_in_queue(self, song_path):
		return song_path in map(lambda t: t['file'], self.queue)

	def enqueue(self, song_path, user = "Pikaraoke"):
		if (self.is_song_in_queue(song_path)):
			logging.warn("Song is already in queue, will not add: " + song_path)
			return False
		else:
			logging.info("'%s' is adding song to queue: %s" % (user, song_path))
			self.queue.append({"user": user, "file": song_path, "title": self.filename_from_path(song_path)})
			self.update_queue()
			return True

	def queue_add_random(self, amount):
		logging.info("Adding %d random songs to queue" % amount)
		songs = list(self.available_songs)  # make a copy
		if len(songs) == 0:
			logging.warn("No available songs!")
			return False
		i = 0
		while i < amount:
			r = random.randint(0, len(songs) - 1)
			if self.is_song_in_queue(songs[r]):
				logging.warn("Song already in queue, trying another... " + songs[r])
			else:
				self.queue.append({"user": "Random", "file": songs[r], "title": self.filename_from_path(songs[r])})
				i += 1
			songs.pop(r)
			if len(songs) == 0:
				self.update_queue()
				logging.warn("Ran out of songs!")
				return False
		self.update_queue()
		return True

	def update_queue(self):
		self.queue_json = json.dumps(self.queue)
		self.status_dirty = True

	def queue_clear(self):
		logging.info("Clearing queue!")
		self.queue = []
		self.update_queue()
		self.skip()

	def queue_edit(self, song_file, action, **kwargs):
		if action == "move":
			try:
				src, tgt, size = [int(kwargs[n]) for n in ['src', 'tgt', 'size']]
				if size > len(self.queue):
					# new songs have started while dragging the list
					diff = size - len(self.queue)
					src -= diff
					tgt -= diff
				song = self.queue.pop(src)
				self.queue.insert(tgt, song)
			except:
				logging.error("Invalid move song request: " + str(kwargs))
				return False
		else:
			match = [(ii,each) for ii,each in enumerate(self.queue) if song_file in each["file"]]
			index, song = match[0] if match else (-1, None)
			if song == None:
				logging.error("Song not found in queue: " + song["file"])
				return False
			if action == "up":
				if index < 1:
					logging.warn("Song is up next, can't bump up in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song up in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index - 1, song)
			elif action == "down":
				if index == len(self.queue) - 1:
					logging.warn("Song is already last, can't bump down in queue: " + song["file"])
					return False
				else:
					logging.info("Bumping song down in queue: " + song["file"])
					del self.queue[index]
					self.queue.insert(index + 1, song)
			elif action == "delete":
				logging.info("Deleting song from queue: " + song["file"])
				del self.queue[index]
			else:
				logging.error("Unrecognized direction: " + action)
				return False
		self.update_queue()
		return True

	def skip(self):
		if self.is_file_playing():
			logging.info("Skipping: " + self.now_playing)
			self.stop_engine()
			if self.use_vlc:
				self.vlcclient.stop()
			else:
				self.omxclient.stop()
			self.reset_now_playing()
			return True
		logging.warning("Tried to skip, but no file is playing!")
		return False

	def seek(self, seek_sec):
		if self.is_file_playing():
			if self.use_vlc:
				sent_at = time.perf_counter()
				self.vlcclient.seek(seek_sec)
				if self.av_sync:
					self.av_sync.seek(float(seek_sec), sent_at)
			else:
				logging.warning("OMXplayer cannot seek track!")
			return True
		logging.warning("Tried to seek, but no file is playing!")
		return False

	def set_delays_dict(self, filename, key, val, dft_val=0):
		basename = os.path.basename(filename)
		delays = self.delays.get(basename, {})
		if val == dft_val:
			delays.pop(key, None)
		else:
			delays[key] = val
		if delays:
			self.delays[basename] = delays
		else:
			self.delays.pop(basename, {})
		self.delays_dirty = True

	def set_audio_delay(self, delay):
		if delay == '+':
			self.audio_delay += 0.1
		elif delay == '-':
			self.audio_delay -= 0.1
		elif delay == '':
			self.audio_delay = 0
		else:
			try:
				self.audio_delay = float(delay)
			except:
				logging.warning(f"Tried to set audio delay to an invalid value {delay}, ignored!")
				return False

		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'audio_delay', self.audio_delay)

		if self.is_file_playing():
			if self.av_sync:
				self.av_sync.audio_delay = self.audio_delay     # VLC has no audio to delay
			elif self.use_vlc:
				self.vlcclient.command(f"audiodelay&val={self.audio_delay}")
			else:
				logging.warning("OMXplayer cannot set audio delay!")
			self.status_dirty = True
			return self.audio_delay
		logging.warning("Tried to set audio delay, but no file is playing!")
		return False

	def set_subtitle_delay(self, delay):
		if delay == '+':
			self.subtitle_delay += 0.1
		elif delay == '-':
			self.subtitle_delay -= 0.1
		elif delay == '':
			self.subtitle_delay = 0
		else:
			try:
				self.subtitle_delay = float(delay)
			except:
				logging.warning(f"Tried to set subtitle delay to an invalid value {delay}, ignored!")
				return False

		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'subtitle_delay', self.subtitle_delay)

		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.command(f"subdelay&val={self.subtitle_delay}")
			else:
				logging.warning("OMXplayer cannot set subtitle delay!")
			self.status_dirty = True
			return self.subtitle_delay
		logging.warning("Tried to set subtitle delay, but no file is playing!")
		return False

	def toggle_subtitle(self):
		self.show_subtitle = not self.show_subtitle
		if self.save_delays:
			self.set_delays_dict(self.now_playing_filename, 'show_subtitle', self.show_subtitle, True)
		self.play_vocal(force=True)

	def pause(self):
		if self.is_file_playing():
			logging.info("Toggling pause: " + self.now_playing)
			if self.use_vlc:
				if self.vlcclient.is_playing():
					self.vlcclient.pause()
					self.is_paused = True
				else:
					sent_at = time.perf_counter()
					self.vlcclient.play()
					self.is_paused = False
				if self.engine:
					if self.is_paused:
						self.engine.pause()
					elif not (self.av_sync and self.av_sync.resumed(sent_at)):
						self.engine.play()
			else:
				if self.omxclient.is_playing():
					self.omxclient.pause()
					self.is_paused = True
				else:
					self.omxclient.play()
					self.is_paused = False
			self.status_dirty = True
			return True
		else:
			logging.warning("Tried to pause, but no file is playing!")
			return False

	# The web UI shows the volume in percent, 0-100%, where 100% is the song as recorded
	# (no boost). Internally it stays in VLC's units, in which 256 is 100%. (omxplayer
	# keeps its own units, millibels.)
	VOL_FULL = 256
	VOL_STEP = 5            # percent per press of a volume button

	def percent_to_vol(self, percent):
		return int(round(np.clip(float(percent), 0, 100) * self.VOL_FULL / 100))

	def vol_to_percent(self, volume = None):
		"""VLC units -> the percent the web UI shows. None: the current volume (the player
		reports none while nothing plays)."""
		if volume is None:
			volume = self.volume
		if not self.use_vlc:
			return volume
		try:
			return int(round(np.clip(float(volume), 0, self.VOL_FULL) * 100 / self.VOL_FULL))
		except (TypeError, ValueError):
			return None

	def engine_vol_set(self, volume):
		"""Volume in VLC's units (256 = 100%), applied by the engine."""
		self.volume = int(round(np.clip(float(volume), 0, self.VOL_FULL)))
		self.engine.volume = self.volume / 256.0
		self.update_logical_vol()
		self.status_dirty = True
		return self.volume

	def vol_up(self):
		"""One step up; returns the new volume as the web UI shows it."""
		if not self.use_vlc:
			return self.omx_vol(self.omxclient.vol_up, "up")
		p = self.vol_to_percent(self.volume) or 0
		return self.vol_set((p // self.VOL_STEP + 1) * self.VOL_STEP)       # to the next multiple of 5

	def vol_down(self):
		if not self.use_vlc:
			return self.omx_vol(self.omxclient.vol_down, "down")
		p = self.vol_to_percent(self.volume) or 0
		return self.vol_set((-(-p // self.VOL_STEP) - 1) * self.VOL_STEP)   # to the previous multiple of 5

	def vol_set(self, percent):
		"""Set the volume in percent (0-100; a trailing % is fine). While nothing plays it
		applies to the next song. Returns the volume as the web UI shows it."""
		if not self.use_vlc:
			logging.warning("Only VLC player can set volume, ignored!")
			return self.omxclient.volume_offset
		try:
			volume = self.percent_to_vol(str(percent).strip().rstrip('%'))
		except ValueError:
			return self.vol_to_percent(self.volume)
		if self.is_file_playing() and self.engine:
			self.engine_vol_set(volume)
		elif self.is_file_playing():
			self.vlcclient.vol_set(volume)
			xml = self.vlcclient.command().text
			self.volume = int(self.vlcclient.get_val_xml(xml, 'volume'))
			self.update_logical_vol()
		else:
			self.volume = volume
			self.player_state['volume'] = volume
		self.status_dirty = True
		return self.vol_to_percent(self.volume)

	def omx_vol(self, change, direction):
		if not self.is_file_playing():
			logging.warning(f"Tried to volume {direction}, but no file is playing!")
			return False
		self.volume = change()
		self.update_logical_vol()
		return self.volume

	def play_speed_set(self, speed):
		if self.is_file_playing():
			if self.use_vlc:
				self.vlcclient.playspeed_set(speed)
				xml = self.vlcclient.command().text
				self.play_speed = float(self.vlcclient.get_val_xml(xml, 'rate'))
				if self.engine:
					self.engine.speed = float(speed)
					self.av_sync and self.av_sync.speed_changed()
				logging.info(f"Playback speed set to {self.play_speed}")
			else:
				logging.warning("Only VLC player can set playback speed, ignored!")
			return self.play_speed
		else:
			logging.warning("Tried to set play speed, but no file is playing!")
			return False

	def try_set_vocal_mode(self, mode, now_playing_filename):
		if mode not in ['mixed', 'vocal', 'nonvocal']:
			mode = {1: 'nonvocal', 2: 'mixed', 3: 'vocal'}[self.get_vocal_mode()]
		play_slave = '' if mode == 'mixed' else self.download_path + mode + '/' + ('' if self.use_DNN_vocal else '.') \
		                                       + os.path.basename(now_playing_filename) + '.m4a'
		if os.path.isfile(play_slave):
			self.vocal_mode = mode
		else:
			play_slave = ''
			self.vocal_mode = 'mixed'
		return play_slave

	def play_vocal(self, mode = None, force = False):
		# mode=vocal/nonvocal/mixed, or else (use current)
		if self.engine and not force:
			if mode in ('nonvocal', 'mixed', 'vocal'):
				self.set_vocal_blend({'nonvocal': -1.0, 'mixed': 0.0, 'vocal': 1.0}[mode])
			else:
				# splitter mode (DNN / stereo) changed: swap in the other set of tracks
				self.engine.remove_split()
				self.load_split_tracks(self.now_playing_filename)
			self.status_dirty = True
			return
		if self.use_vlc:
			play_slave = self.try_set_vocal_mode(mode, self.now_playing_filename)
			if not force and self.now_playing_slave == play_slave:
				return
			# Live: select another audio track of the combined file (see make_bundle).
			# force=True (subtitle toggle) needs a relaunch, so it skips this.
			b = self.playing_bundle
			if not force and b and b['source'] == self.now_playing_filename and b['dnn'] == self.use_DNN_vocal \
					and self.vocal_mode in b['tracks'] and self.vlcclient.select_audio_track_live(b['tracks'][self.vocal_mode]):
				self.now_playing_slave = play_slave
				self.get_vocal_info(True)
				self.status_dirty = True
				return
			status_xml = self.vlcclient.command().text if self.is_paused else self.vlcclient.pause(False).text
			info = self.vlcclient.get_info_xml(status_xml)
			posi = info['position']*info['length']
			self.play_file(self.now_playing_filename, [f'--start-time={posi}'] + (['--start-paused'] if self.is_paused else []))
			self.get_vocal_info(True)
		else:
			logging.error("Not using VLC. Can't play vocal/nonvocal.")

	def get_vocal_mode(self):
		if '/nonvocal/' in self.now_playing_slave.replace('\\', '/'):
			return 1
		elif '/vocal/' in self.now_playing_slave.replace('\\', '/'):
			return 3
		return 2

	def get_vocal_info(self, force_update=False):
		tm = time.time()
		if not force_update and tm-self.last_vocal_time < 2:
			return self.last_vocal_info
		if not self.now_playing_filename:
			return 0
		mask = 0
		bn = os.path.basename(self.now_playing_filename)
		if os.path.isfile(f'{self.download_path}nonvocal/{bn}.m4a'):
			mask |= 0b00000001
		if os.path.isfile(f'{self.download_path}vocal/{bn}.m4a'):
			mask |= 0b00000010
		if os.path.isfile(f'{self.download_path}nonvocal/.{bn}.m4a'):
			mask |= 0b00000100
		if os.path.isfile(f'{self.download_path}vocal/.{bn}.m4a'):
			mask |= 0b00001000
		if 'vocal/.' in self.now_playing_slave:
			mask |= 0b10000000
		if self.use_DNN_vocal:
			mask |= 0b01000000
		if self.engine:
			if not self.engine.has_split:
				self.load_split_tracks(self.now_playing_filename)   # the splitter may just have finished
			b = self.vocal_blend
			mask |= (1 if b <= -0.5 else 3 if b >= 0.5 else 2) << 4
		else:
			mask |= (self.get_vocal_mode() << 4)
		self.last_vocal_info = mask
		self.last_vocal_time = tm
		return mask

	def get_state(self):
		if self.use_vlc and self.vlcclient.is_transposing:
			return defaultdict(lambda: None, self.player_state)
		if not self.is_file_playing():
			self.player_state['now_playing'] = None
			return defaultdict(lambda: None, self.player_state)
		new_state = self.vlcclient.get_info_xml() if self.use_vlc else {
			'volume': self.omxclient.volume_offset,
			'state': ('paused' if self.omxclient.paused else 'playing')
		}
		self.player_state.update(new_state)
		if self.engine:
			self.player_state['volume'] = self.volume
			self.player_state['audiodelay'] = self.audio_delay
		return defaultdict(lambda: None, self.player_state)

	def restart(self):
		if self.is_file_playing():
			if self.use_vlc:
				sent_at = time.perf_counter()
				self.vlcclient.restart()
				if self.av_sync:
					self.av_sync.seek(0.0, sent_at)
			else:
				self.omxclient.restart()
			self.is_paused = False
			return True
		else:
			logging.warning("Tried to restart, but no file is playing!")
			return False

	def stop(self):
		self.running = False

	def handle_run_loop(self):
		if self.fullscreen_request:
			self.fullscreen_request = False
			self.toggle_full_screen()
		for event in pygame.event.get():
			if event.type == pygame.QUIT:
				logging.warn("Window closed: Exiting pikaraoke...")
				self.running = False
			elif event.type == pygame.KEYDOWN:
				if event.key == pygame.K_ESCAPE:
					logging.warn("ESC pressed: Exiting pikaraoke...")
					self.running = False
				if event.key == pygame.K_f:
					self.toggle_full_screen()
		if not self.is_file_playing() or not self.has_video:
			self.render_splash_screen()
			pygame.display.update()
		pygame.time.wait(100)

	# Use this to reset the screen in case it loses focus
	# This seems to occur in windows after playing a video
	def pygame_reset_screen(self):
		if not self.hide_splash_screen:
			logging.debug("Resetting pygame screen...")
			pygame.display.quit()
			self.initialize_screen()
			self.render_splash_screen()

	def reset_now_playing(self):
		self.auto_save_delays()
		self.now_playing = None
		self.now_playing_filename = None
		self.now_playing_user = None
		self.is_paused = True
		self.now_playing_transpose = 0
		self.now_playing_slave = ''
		self.playing_bundle = None
		self.stop_engine()
		self.audio_delay = 0
		self.subtitle_delay = 0
		self.show_subtitle = True
		self.has_subtitle = False
		self.has_video = True
		self.last_vocal_info = 0
		self.play_speed = 1

	def streamer_alive(self):
		try:
			return bool([1 for p in psutil.process_iter() if './screencapture.sh' in p.cmdline()])
		except:
			return None

	def streamer_restart(self, delay=0):
		if self.platform in ['windows', 'osx']:
			return
		os.system(f"sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c && tmux send-keys -t PiKaraoke:0.3 Up Enter")

	def streamer_stop(self, delay=0):
		if self.platform in ['windows', 'osx']:
			return
		os.system(f"sleep {delay} && tmux send-keys -t PiKaraoke:0.3 C-c")

	def vocal_alive(self):
		try:
			return bool(self.vocal_process and self.vocal_process.is_alive())\
					or bool([1 for p in psutil.process_iter() if 'vocal_splitter.py' in p.cmdline()])
		except:
			return None

	def vocal_restart(self):
		if self.platform in ['windows', 'osx'] or self.run_vocal:
			import vocal_splitter
			if self.vocal_process is not None and self.vocal_process.is_alive():
				self.vocal_process.kill()
			if shutil.which('ffmpeg'):
				self.vocal_process = mp.Process(target=vocal_splitter.main, args=(['-p', '-d', self.download_path],))
				self.vocal_process.start()
			else:
				logging.error("ffmpeg not found in PATH, vocal splitter disabled. Install it with: brew install ffmpeg")
		else:
			os.system(f"tmux send-keys -t PiKaraoke:0.4 C-c && tmux send-keys -t PiKaraoke:0.4 Up Enter")

	def vocal_stop(self):
		if self.vocal_process is not None and self.vocal_process.is_alive():
			self.vocal_process.kill()
		elif self.platform not in ['windows', 'osx']:
			os.system(f"tmux send-keys -t PiKaraoke:0.4 C-c")

	def get_mp3_volume(self, filename):
		try:
			basename, md5, fsize = os.path.basename(filename), md5sum(filename), os.stat(filename).st_size
			vol_fsize_md5 = self.song2vol.get(basename, [0]*3)
			if fsize == vol_fsize_md5[1] and md5 == vol_fsize_md5[2]:
				return vol_fsize_md5[0]
			pcm_data = subprocess.check_output(['ffmpeg', '-i', filename, '-vn', '-f', 's16le', '-acodec', 'pcm_s16le', '-'], stderr = subprocess.DEVNULL)
			volume_val = np.clip(np.sqrt(np.std(np.frombuffer(pcm_data, dtype = np.int16))/STD_VOL), 1/16, 16)
			self.song2vol[basename] = [volume_val, fsize, md5]
			with Open(self.download_path+'/.mp3_volume.json.gz', 'wb') as fp:
				json.dump(self.song2vol, fp, indent=1)
			return volume_val
		except:
			self.normalize_vol = False
			return 1

	def update_logical_vol(self):
		if hasattr(self, 'media_vol'):
			self.logical_volume = self.volume * self.media_vol

	def enable_vol_norm(self, enable):
		self.normalize_vol = enable
		if enable and shutil.which('ffmpeg') is None:
			self.normalize_vol = enable = False
		if enable and self.now_playing_filename:
			if not self.engine:
				self.volume = self.vlcclient.get_info_xml()['volume']
			self.media_vol = self.get_mp3_volume(self.now_playing_filename)
			self.update_logical_vol()
		return str(self.logical_volume)

	def init_save_delays(self):
		self.delays_dirty = False
		try:
			self.delays = eval(open(self.save_delays).read())
		except:
			self.delays = {}
			with open(self.save_delays, 'w') as fp:
				fp.write(str(self.delays))

	def set_save_delays(self, state):
		if state != bool(self.save_delays):
			if state:
				self.save_delays = self.dft_delays_file
				self.init_save_delays()
			else:
				self.save_delays = None
				self.delete_if_exist(self.dft_delays_file)

	def auto_save_delays(self):
		if self.save_delays and self.delays_dirty:
			self.delays_dirty = False
			with open(self.save_delays, 'w') as fp:
				fp.write(str(self.delays))

	def run(self):
		logging.info("Starting PiKaraoke!")
		self.running = True

		# Windows and macOS do not run the tmux session from run.sh, so the vocal splitter
		# can only be invoked from the main program
		if self.platform in ['windows', 'osx'] or self.run_vocal:
			Try(lambda: self.vocal_restart())

		while self.running:
			try:
				if not self.is_file_playing() and self.now_playing != None:
					self.reset_now_playing()
				if self.queue:
					if not self.is_file_playing():
						self.reset_now_playing()
						self.render_splash_screen()
						tm = time.time()
						while time.time()-tm < self.splash_delay:
							self.handle_run_loop()
						head = self.queue.pop(0)
						self.play_file(head['file'])
						if self.cloud:
							self.cloud_tasks += [head['file']]
							self.cloud_trigger.set()
						if not self.firstSongStarted:
							if self.streamer_alive():
								self.streamer_restart(1)
							self.firstSongStarted = True
						self.now_playing_user = head["user"]
						self.update_queue()
				self.handle_run_loop()
			except KeyboardInterrupt:
				logging.warn("Keyboard interrupt: Exiting pikaraoke...")
				self.running = False

		# Clean up before quit
		self.streamer_stop()
		self.vocal_stop()
		self.stop_engine()
		vplayer = self.vlcclient if self.use_vlc else self.omxclient
		if vplayer is not None: vplayer.stop()
		self.auto_save_delays()
		time.sleep(1)
		if vplayer is not None: vplayer.kill()
