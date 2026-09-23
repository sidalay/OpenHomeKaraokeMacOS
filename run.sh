#!/usr/bin/env bash

session_name=PiKaraoke

# Use the project venv's interpreter when there is one, so run.sh does not depend on
# whatever "python3" happens to be first on PATH (on macOS that is often a Homebrew or
# Anaconda build with a different, sometimes unsupported, set of packages).
cd "`dirname $0`"
PYTHON=python3
if [ -x .venv/bin/python ]; then
	PYTHON="`pwd`/.venv/bin/python"
fi

if [[ "$OSTYPE" == "darwin"* ]]; then
	# macOS: no X11 display to export and no PulseAudio. The vocal splitter is started
	# by app.py itself (see Karaoke.vocal_restart), so it gets no pane of its own here.
	cmds=("top"
	"PATH='$PATH' $PYTHON app.py"
	"PATH='$PATH' ./screencapture.sh -v -p 4000"
	)
else
	cmds=("top"
	#"sudo sh -c 'cp -f $HOME/.Xauthority ~ && PATH=$PATH python3 app.py -u $(whoami)'"
	"PATH='$PATH' $PYTHON app.py"
	"PATH='$PATH' ./screencapture.sh -v -p 4000"
	"PATH='$PATH' $PYTHON vocal_splitter.py -p -d ~/pikaraoke-songs/"
	#"pavucontrol"
	)
fi

if ! type tmux >/dev/null 2>&1; then
	echo "tmux is not installed. Install it (macOS: brew install tmux), or just run:" >&2
	echo "  $PYTHON app.py" >&2
	exit 1
fi

if [ "`tmux ls 2>/dev/null | grep $session_name`" ]; then
	echo "TMUX Session $session_name already exists!" >&2
	exit 1
fi

if [[ "$OSTYPE" != "darwin"* ]]; then
	export DISPLAY=:0
fi

tmux new-session -s $session_name -d -x 240 -y 60

for i in `seq 0 $[${#cmds[*]}-1]`; do
	sleep 0.2
	tmux split-window
	sleep 0.2
	tmux select-layout tile
	sleep 0.2
	tmux send-keys -l "${cmds[i]}"
	sleep 0.2
	tmux send-keys Enter
done

# Set pulseaudio recording source (Linux only; macOS has no PulseAudio)
if [[ "$OSTYPE" != "darwin"* ]] && type pacmd >/dev/null 2>&1; then
	src="`pacmd list-sources | grep '.monitor>' | awk '{print $2}' | head -1 `"
	idx="`pacmd list-source-outputs | grep index: | awk '{print $2}' | tail -1`"
	if [ "$src" ]; then
		pacmd move-source-output $idx "${src:1:-1}"
	fi
fi

if [ -n "$OHK_NO_ATTACH" ]; then
	# Started by the systemd unit (see deploy/pi/): the session must stay detached,
	# there is no terminal to attach to. Use `tmux attach -t PiKaraoke` to look at it.
	echo "Session $session_name started detached (OHK_NO_ATTACH set)."
	exit 0
fi

tmux a -t $session_name

