/*
 * Applies the web UI theme. Loaded in <head> so the theme is set before the page first
 * paints (no flash of the old look). The choice is per device, kept in localStorage, so
 * every guest's phone can pick its own; phones that never chose get DEFAULT_THEME.
 *
 * "auto" is not a theme of its own: it shows latte or espresso to match the device's
 * light/dark setting, and follows it live when the device switches.
 *
 * To add a theme: add its token block to theme.css, then an entry here.
 */
(function () {
	var THEMES = [
		// swatches: the three dots shown in the picker; bar: the phone browser's toolbar color
		{ name: 'auto',     label: 'Auto',     swatches: ['#F4ECE1', '#8F6140', '#1C1511'], light: 'latte', dark: 'espresso' },
		{ name: 'latte',    label: 'Latte',    swatches: ['#F4ECE1', '#D2B48F', '#8F6140'], bar: '#F4ECE1' },
		{ name: 'espresso', label: 'Espresso', swatches: ['#1C1511', '#45342A', '#D9A877'], bar: '#1C1511' },
		{ name: 'classic',  label: 'Classic',  swatches: ['#222222', '#375A7F', '#1ABC9C'], bar: '#375A7F' }
	];
	var DEFAULT_THEME = 'auto';
	var STORAGE_KEY = 'ohk-theme';

	function find(name) {
		for (var i = 0; i < THEMES.length; i++) if (THEMES[i].name === name) return THEMES[i];
		return null;
	}

	function load() {
		try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
	}

	function save(name) {
		try { localStorage.setItem(STORAGE_KEY, name); } catch (e) { /* private mode: session only */ }
	}

	var darkQuery = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

	// The theme actually drawn: "auto" resolves to its light or dark theme
	function resolve(theme) {
		if (!theme.light) return theme;
		return find(darkQuery && darkQuery.matches ? theme.dark : theme.light);
	}

	// Applies the chosen theme and returns the choice (which may be "auto")
	function apply(name) {
		var choice = find(name) || find(DEFAULT_THEME);
		var theme = resolve(choice);
		var root = document.documentElement;
		root.setAttribute('data-theme', theme.name);
		// "classic" is the untouched original Bulma look: none of theme.css's rules apply
		if (theme.name === 'classic') root.classList.remove('themed');
		else root.classList.add('themed');

		var meta = document.querySelector('meta[name="theme-color"]');
		if (!meta) {
			meta = document.createElement('meta');
			meta.name = 'theme-color';
			document.head.appendChild(meta);
		}
		meta.content = theme.bar;
		return choice.name;
	}

	var current = apply(load());

	// Follow the device switching between light and dark while the page is open
	function onSchemeChange() {
		if (find(current).light) apply(current);
	}
	if (darkQuery) {
		if (darkQuery.addEventListener) darkQuery.addEventListener('change', onSchemeChange);
		else if (darkQuery.addListener) darkQuery.addListener(onSchemeChange);   // Safari < 14
	}
	// Phones usually switch at sunset while locked or with the browser in the background.
	// Re-check when the page comes back, in case the change event was not delivered then.
	document.addEventListener('visibilitychange', function () {
		if (!document.hidden) onSchemeChange();
	});
	window.addEventListener('pageshow', onSchemeChange);   // restored from the back/forward cache

	window.OHKTheme = {
		themes: THEMES,
		get: function () { return current; },                     // the choice, e.g. "auto"
		shown: function () { return document.documentElement.getAttribute('data-theme'); },
		set: function (name) {
			current = apply(name);
			save(current);
			return current;
		},
		// Fills `el` with one button per theme. Used by the Info page.
		renderPicker: function (el) {
			if (!el) return;
			el.innerHTML = '';
			THEMES.forEach(function (t) {
				var b = document.createElement('button');
				b.type = 'button';
				b.className = 'ohk-theme-option';
				b.setAttribute('data-theme-name', t.name);
				b.setAttribute('aria-pressed', String(t.name === current));
				var dots = document.createElement('span');
				dots.className = 'ohk-swatches';
				t.swatches.forEach(function (c) {
					var d = document.createElement('i');
					d.style.background = c;
					dots.appendChild(d);
				});
				var label = document.createElement('span');
				label.textContent = t.label;
				b.appendChild(dots);
				b.appendChild(label);
				b.addEventListener('click', function () {
					window.OHKTheme.set(t.name);
					var all = el.querySelectorAll('.ohk-theme-option');
					for (var i = 0; i < all.length; i++)
						all[i].setAttribute('aria-pressed', String(all[i].getAttribute('data-theme-name') === current));
				});
				el.appendChild(b);
			});
		}
	};
})();
