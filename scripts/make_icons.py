"""Draws the app icon (a microphone in the Latte/Espresso palette) in every size needed.

    .venv/bin/python scripts/make_icons.py

Drawn at 2048 px with Pillow and scaled down, so the edges are smooth. Writes, into static/icons/:
  icon-192.png, icon-512.png   rounded tile (browsers, Android "any" icon)
  maskable-512.png             full-bleed tile, mic inside the 80% safe zone (Android masks)
  apple-touch-icon.png         180 px full-bleed square (iOS rounds the corners itself)
  favicon-32.png, favicon-16.png
"""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'static', 'icons')
N = 2048                                              # drawing size; 1 unit = N/512 px
u = lambda v: int(round(v * N / 512))

BG_TOP, BG_BOTTOM = (98, 76, 58), (38, 28, 22)        # espresso gradient
CREAM, GRILLE = (246, 238, 227), (226, 210, 190)
CARAMEL, LATTE, LATTE_DARK = (217, 168, 119), (227, 185, 142), (196, 150, 108)


def background(rounded):
	grad = Image.new('RGB', (1, N))
	for y in range(N):
		t = y / (N - 1)
		grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(BG_TOP, BG_BOTTOM)))
	tile = grad.resize((N, N)).convert('RGBA')
	if rounded:
		mask = Image.new('L', (N, N), 0)
		ImageDraw.Draw(mask).rounded_rectangle((0, 0, N - 1, N - 1), radius = u(112), fill = 255)
		tile.putalpha(mask)
	return tile


def microphone(scale):
	"""An upright handheld mic on a transparent layer, then tilted like the old icon."""
	s = lambda v: u(256 + (v - 256) * scale)          # scale around the centre
	layer = Image.new('RGBA', (N, N), (0, 0, 0, 0))
	d = ImageDraw.Draw(layer)
	# handle: tapered, with a rounded end cap
	d.polygon([(s(208), s(262)), (s(304), s(262)), (s(284), s(418)), (s(228), s(418))], fill = LATTE)
	d.rounded_rectangle((s(228), s(400), s(284), s(446)), radius = u(20 * scale), fill = LATTE_DARK)
	# head: a cream ball with a grille of dots, and a caramel band where it meets the handle
	head = (s(164), s(84), s(348), s(268))
	d.ellipse(head, fill = CREAM)
	cx, cy, r = s(256), s(176), (head[2] - head[0]) / 2
	step = u(22 * scale)
	for gx in range(int(cx - r), int(cx + r), step):
		for gy in range(int(cy - r), int(s(214)), step):
			if (gx - cx) ** 2 + (gy - cy) ** 2 < (r * 0.78) ** 2:
				d.ellipse((gx - u(3.2 * scale), gy - u(3.2 * scale), gx + u(3.2 * scale), gy + u(3.2 * scale)), fill = GRILLE)
	band = Image.new('L', (N, N), 0)
	ImageDraw.Draw(band).rectangle((0, s(222), N, s(250)), fill = 255)
	inside = Image.new('L', (N, N), 0)
	ImageDraw.Draw(inside).ellipse(head, fill = 255)
	layer.paste(Image.new('RGBA', (N, N), CARAMEL + (255,)), (0, 0), Image.composite(band, Image.new('L', (N, N), 0), inside))
	return layer.rotate(-38, resample = Image.BICUBIC, center = (u(256), u(256)))


def compose(rounded, scale):
	tile = background(rounded)
	mic = microphone(scale)
	shadow = Image.new('RGBA', (N, N), (0, 0, 0, 0))
	shadow.paste((20, 12, 8, 110), (u(10), u(14)), mic.split()[3])          # soft drop shadow
	shadow = shadow.filter(ImageFilter.GaussianBlur(u(10)))
	out = Image.alpha_composite(tile, shadow)
	out = Image.alpha_composite(out, mic)
	if rounded:                                        # keep the corners transparent
		out.putalpha(Image.composite(out.split()[3], Image.new('L', (N, N), 0), tile.split()[3]))
	return out


def save(img, size, name, opaque = False):
	img = img.resize((size, size), Image.LANCZOS)
	if opaque:
		img = img.convert('RGB')
	img.save(os.path.join(HERE, name), optimize = True)


tile = compose(rounded = True, scale = 1.0)
save(tile, 512, 'icon-512.png'); save(tile, 192, 'icon-192.png')
save(tile, 32, 'favicon-32.png'); save(tile, 16, 'favicon-16.png')
save(compose(rounded = False, scale = 0.80), 512, 'maskable-512.png', opaque = True)
save(compose(rounded = False, scale = 0.92), 180, 'apple-touch-icon.png', opaque = True)
print('written:', ', '.join(sorted(f for f in os.listdir(HERE) if f.endswith('.png'))))
