#!/usr/bin/env bash
#
# Start OpenHomeKaraoke automatically at login, via a per-user LaunchAgent.
#
#   deploy/macos/launch-agent.sh install [app.py args...]   # e.g. install --admin-password 1234
#   deploy/macos/launch-agent.sh restart      # e.g. after `git pull`; also starts it if stopped
#   deploy/macos/launch-agent.sh uninstall
#   deploy/macos/launch-agent.sh status
#
# A LaunchAgent (not a LaunchDaemon) because the app needs the logged-in GUI session:
# pygame draws the splash screen and VLC plays onto the display.
#
# Restart policy: relaunched if it crashes (non-zero exit), but NOT when stopped on
# purpose, since both `exitkaraoke` and the ESC key make the app exit with status 0.
# Start it again with `openkaraoke` or `deploy/macos/launch-agent.sh install`.

set -euo pipefail

LABEL="com.openhomekaraoke"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/OpenHomeKaraoke.log"
DOMAIN="gui/$(id -u)"

PROJECT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# launchd starts agents with PATH=/usr/bin:/bin:/usr/sbin:/sbin, which has no Homebrew.
# The app runs `ffmpeg` and looks up `deno` on PATH, so both would silently go missing.
BREW_PREFIX="$(brew --prefix 2>/dev/null || echo /opt/homebrew)"
AGENT_PATH="$BREW_PREFIX/bin:$BREW_PREFIX/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

# `launchctl bootout` returns before launchd has finished removing the service (the app
# is still shutting down), and bootstrapping again during that window fails with
# "Bootstrap failed: 5: Input/output error". So wait until it is really gone.
unload() {
	is_loaded || return 0
	launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
	for _ in $(seq 1 60); do
		is_loaded || return 0
		sleep 0.5
	done
	echo "launch-agent: $LABEL still loaded after 30s." >&2
	return 1
}

xml_escape() { sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'; }

cmd_install() {
	if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
		echo "launch-agent: no .venv in $PROJECT_DIR — finish the install steps first." >&2
		exit 1
	fi

	local args_xml="		<string>$(printf '%s' "$PROJECT_DIR/openkaraoke" | xml_escape)</string>"$'\n'
	for a in "$@"; do
		args_xml+="		<string>$(printf '%s' "$a" | xml_escape)</string>"$'\n'
	done

	mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
	cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
$args_xml	</array>
	<key>WorkingDirectory</key>
	<string>$(printf '%s' "$PROJECT_DIR" | xml_escape)</string>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PATH</key>
		<string>$AGENT_PATH</string>
	</dict>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>RunAtLoad</key>
	<true/>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>StandardOutPath</key>
	<string>$LOG</string>
	<key>StandardErrorPath</key>
	<string>$LOG</string>
</dict>
</plist>
EOF
	plutil -lint "$PLIST" >/dev/null

	# Replace any previous version, then load (RunAtLoad starts it immediately).
	unload
	launchctl bootstrap "$DOMAIN" "$PLIST"
	echo "Installed $PLIST"
	echo "OpenHomeKaraoke is starting now, and will start at every login."
	echo "Log: $LOG"
}

cmd_restart() {
	if [ ! -f "$PLIST" ]; then
		echo "launch-agent: not installed, run install first." >&2
		exit 1
	fi
	if is_loaded; then
		# -k kills the running instance first; works whether it is running or was stopped.
		launchctl kickstart -k "$DOMAIN/$LABEL"
	else
		launchctl bootstrap "$DOMAIN" "$PLIST"   # RunAtLoad starts it
	fi
	echo "Restarted."
}

cmd_uninstall() {
	unload
	rm -f "$PLIST"
	echo "Removed the LaunchAgent. OpenHomeKaraoke will no longer start at login."
}

cmd_status() {
	if [ ! -f "$PLIST" ]; then
		echo "Not installed."
		return
	fi
	launchctl print "$DOMAIN/$LABEL" 2>/dev/null \
		| grep -E '^\s*(state|pid|last exit code|runs) =' \
		|| echo "Installed but not loaded (log out and back in, or run install again)."
}

case "${1:-}" in
	install)   shift; cmd_install "$@" ;;
	restart)   cmd_restart ;;
	uninstall) cmd_uninstall ;;
	status)    cmd_status ;;
	*) sed -n '3,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1 ;;
esac
