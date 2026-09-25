/*
 * Swipe a row left to reveal a red delete button (the Queue and Browse lists).
 *
 * Row markup:
 *   <li class="swipe-row">
 *     <div class="swipe-track">
 *       <div class="swipe-content">...the row...</div>
 *       <button class="swipe-delete">&#x2715;</button>
 *     </div>
 *   </li>
 * The track is one button wider than the row (see static/custom.css), with the button
 * just past its right edge; swiping slides the track left and the button comes into view.
 *
 * A mostly-horizontal move to the left is a swipe; a vertical one is left to the page
 * (scrolling) or to the list (the queue's press-and-hold reordering). One row is open at
 * a time; while one is open, a tap anywhere else only closes it (it does not also add a
 * song, for instance).
 */
window.OHKSwipe = (function () {
	var WIDTH = 76;          // px: the delete button
	var SLOP = 8;            // px of movement before deciding what a gesture is
	var open = null;         // the row currently swiped open
	var swallowClick = false;

	function track(row) { return row.querySelector('.swipe-track'); }

	function place(row, x, animate) {
		var t = track(row);
		t.style.transition = animate ? 'transform 0.2s ease' : 'none';
		t.style.transform = x ? `translateX(${x}px)` : '';
	}

	function openRow(row) {
		if (open && open !== row) closeRow(open);
		open = row;
		row.classList.add('swipe-open');
		place(row, -WIDTH, true);
	}

	function closeRow(row) {
		row = row || open;
		if (!row) return;
		row.classList.remove('swipe-open');
		place(row, 0, true);
		if (open === row) open = null;
	}

	// a tap outside the open row closes it, and does nothing else
	document.addEventListener('click', function (e) {
		if (swallowClick) {
			swallowClick = false;
			e.stopPropagation(); e.preventDefault();
			return;
		}
		if (open && !document.body.contains(open)) open = null;     // its pane was replaced
		if (open && !(e.target.closest && e.target.closest('.swipe-delete'))) {
			closeRow();                                             // like iOS: this tap only closes it
			e.stopPropagation(); e.preventDefault();
		}
	}, true);

	/*
	 * list:   the element holding the rows (this pane's; the script runs on each visit)
	 * opts:   rowSelector  which elements are rows (default '.swipe-row')
	 *         onDelete(row) called when a row's delete button is pressed
	 *         busy()       true while another gesture owns the list (e.g. reordering)
	 */
	function attach(list, opts) {
		opts = opts || {};
		var sel = opts.rowSelector || '.swipe-row';
		var busy = opts.busy || function () { return false; };
		var g = null;        // the gesture in progress

		// capture phase: the row's own tap handler (e.g. Browse's tap-to-add) must not see it
		list.addEventListener('click', function (e) {
			var btn = e.target.closest('.swipe-delete');
			if (!btn || !list.contains(btn)) return;
			e.stopPropagation(); e.preventDefault();
			var row = btn.closest(sel);
			if (row && opts.onDelete) opts.onDelete(row);
		}, true);

		function point(e) { var t = e.touches ? e.touches[0] : e; return {x: t.clientX, y: t.clientY}; }

		function begin(e) {
			var row = e.target.closest(sel);
			if (!row || !list.contains(row) || e.target.closest('.swipe-delete') || busy()) return;
			if (e.type == 'mousedown' && e.button !== 0) return;
			var p = point(e), touch = e.type == 'touchstart';
			g = {row: row, x: p.x, y: p.y, touch: touch, mode: null, base: row === open ? -WIDTH : 0};
			document.addEventListener(touch ? 'touchmove' : 'mousemove', move, {passive: false});
			document.addEventListener(touch ? 'touchend' : 'mouseup', end);
			if (touch) document.addEventListener('touchcancel', end);
		}

		function move(e) {
			if (!g) return;
			var p = point(e), dx = p.x - g.x, dy = p.y - g.y;
			if (!g.mode) {
				if (Math.hypot(dx, dy) < SLOP) return;
				// horizontal enough to be a swipe (left to open; right to close an open row)
				g.mode = (Math.abs(dx) > Math.abs(dy) * 1.2 && !busy() && (dx < 0 || g.base < 0)) ? 'swipe' : 'other';
				if (g.mode == 'swipe' && open && open !== g.row) closeRow(open);
			}
			if (g.mode != 'swipe') return;
			e.preventDefault();                              // no page scroll while swiping
			var x = g.base + dx;
			if (x > 0) x = 0;
			if (x < -WIDTH) x = -WIDTH - (-WIDTH - x) / 3;    // rubber band past the button
			g.x_now = x;
			place(g.row, x, false);
		}

		function end() {
			document.removeEventListener(g.touch ? 'touchmove' : 'mousemove', move, {passive: false});
			document.removeEventListener(g.touch ? 'touchend' : 'mouseup', end);
			document.removeEventListener('touchcancel', end);
			if (g.mode == 'swipe') {
				(g.x_now || 0) < -WIDTH / 2 ? openRow(g.row) : closeRow(g.row);
				swallowClick = true;                           // the swipe is not also a tap
				setTimeout(function () { swallowClick = false; }, 400);
			}
			g = null;
		}

		list.addEventListener('touchstart', begin, {passive: true});
		list.addEventListener('mousedown', begin);
	}

	return {attach: attach, close: closeRow, width: WIDTH};
})();
