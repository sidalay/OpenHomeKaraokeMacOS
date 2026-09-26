# Mac mini setup

Set up a fresh Mac mini to run OpenHomeKaraoke on the TV, so guests can scan the QR code
and run everything from their phones. There are two ways to run it:

| | **A. Manual start** | **B. Dedicated karaoke box** |
|---|---|---|
| How it starts | You run `openkaraoke` | By itself when the Mac powers on |
| Best for | A Mac you also use for other things | A Mac that lives behind the TV |
| macOS settings changed | None | Automatic login, FileVault off, power settings |
| Steps | 1–7 | 1–6, then 8 |

Steps 1–6 are the same for both. You can switch either way later (see
[Switching between the two](#switching-between-the-two)). Allow 30–45 minutes, most of
it downloads.

> Every command here was run on Apple Silicon with macOS and Python 3.12. The autostart
> script was tested for install, reinstall, restart, crash recovery and uninstall, and
> the keep-awake behavior in both modes. It has not yet been run end to end on a
> brand-new machine, so if a step doesn't match what you see, trust the error message
> and let me know.

**Contents:** [Before you start](#0-before-you-start) ·
[Install](#1-homebrew) · [First run](#6-first-run) ·
[A. Manual start](#7-option-a-manual-start) ·
[B. Dedicated box](#8-option-b-dedicated-karaoke-box) ·
[Switching](#switching-between-the-two) · [Day to day](#day-to-day) ·
[Updating](#updating) · [Troubleshooting](#troubleshooting)

---

## 0. Before you start

- Connect the Mac mini to the TV over **HDMI**. Ethernet is better than Wi-Fi for
  downloads, but either works.
- In Setup Assistant, create your account. For a dedicated box, a separate account
  such as `karaoke` keeps things tidy.
- **FileVault (dedicated box only):** automatic login (step 8b) needs FileVault **off**,
  because macOS doesn't allow automatic login with FileVault on. For manual start,
  leave FileVault on. You can change this later in System Settings → Privacy &
  Security → FileVault.

Everything below runs in **Terminal** (Applications → Utilities → Terminal).

## 1. Homebrew

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

This also installs Apple's Command Line Tools, which include `git`. When it finishes, it
prints two commands to add Homebrew to your PATH. Run them, or run these, which do
the same thing:

```bash
echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
eval "$(/opt/homebrew/bin/brew shellenv)"
```

## 2. System dependencies

```bash
brew install python@3.12 ffmpeg deno
brew install --cask vlc
```

| Package | Why |
|---|---|
| `python@3.12` | **Must be 3.12.** Flask 2.3 and other pinned dependencies fail to import on 3.13+ |
| `ffmpeg` | Decodes downloads and encodes the split vocal/backing tracks |
| `deno` | JavaScript runtime yt-dlp needs for full YouTube support |
| `vlc` | The video player |

Next, **open VLC once from Applications** and click *Open* when macOS asks whether
you trust it. That clears the "downloaded from the internet" check, so the dialog
never blocks the karaoke player later. Then quit VLC.

## 3. Get the code

```bash
git clone https://github.com/sidalay/OpenHomeKaraokeMacOS.git ~/OpenHomeKaraoke
cd ~/OpenHomeKaraoke
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The last step downloads PyTorch and takes a few minutes.

## 4. Songs folder

```bash
mkdir -p ~/pikaraoke-songs/nonvocal ~/pikaraoke-songs/vocal
```

Songs download into `~/pikaraoke-songs`. The two subfolders enable vocal splitting:
`nonvocal/` stores backing tracks with the vocals removed, and `vocal/` stores the
vocals on their own. If you only want backing tracks, skip `vocal/` and splitting
takes half as long.

Keep the library out of Documents, Desktop and Downloads. macOS restricts access to
those folders, and the permission prompt would show up on the TV when the app starts
at login.

### Optional: bring your existing library over

To skip re-downloading and re-splitting, copy the library from your MacBook. First
turn on **Remote Login** on the mini (System Settings → General → Sharing), then run
this **on the MacBook**:

```bash
rsync -a --progress --exclude='.input.wav' --exclude='.vocal.wav' --exclude='.nonvocal.wav' ~/pikaraoke-songs/ karaoke@<mini-name>.local:pikaraoke-songs/
```

Replace `<mini-name>`, **including the `<` and `>`**, with the mini's local hostname,
and `karaoke` with the account's short name. For a mini with the hostname `Mac-Mini.local`
and the account `sid`, the end of the command is `sid@Mac-Mini.local:pikaraoke-songs/`.
(Left in, zsh reads `<` as "read from a file" and fails with *no such file or directory*.)
The hostname is at the bottom of the Sharing settings, under *Local hostname*; the
account's short name is what `whoami` prints in Terminal on the mini. The first time,
answer `yes` to the "authenticity of host" question, then enter the mini account's
password. If the copy is interrupted, run the same command again: it picks up where it
stopped. This copies the split tracks and your saved per-song delays
(`.delays`). The excluded files are temporary splitter scratch files.

## 5. `openkaraoke` / `exitkaraoke` commands

```bash
mkdir -p ~/.local/bin
ln -s ~/OpenHomeKaraoke/openkaraoke ~/.local/bin/openkaraoke
ln -s ~/OpenHomeKaraoke/exitkaraoke ~/.local/bin/exitkaraoke
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zprofile
```

**Open a new Terminal window** so the PATH change takes effect. (`~/.local/bin` isn't on
a fresh Mac's PATH, and without this step the commands won't be found.)

## 6. First run

First, turn off **AirPlay Receiver**: System Settings → General → **AirDrop & Handoff**
→ *AirPlay Receiver* → off. It is on by default and occupies port 5000, the port
OpenHomeKaraoke uses. Left on, phones that scan the QR code get AirPlay's empty
"403 Forbidden" page instead of the app. (The app now refuses to start and says so when
the port is taken. To keep AirPlay Receiver instead, run the app on another port,
e.g. `openkaraoke -p 5050`.)

```bash
openkaraoke
```

The TV should switch to a full-screen splash screen with a QR code and an address like
`http://192.168.x.x:5000`.

1. Scan the QR code with your phone. The phone must be on the **same network** as the mini.
2. Search for a song and add it to the queue. It downloads, then plays.
3. After about 10 seconds, the vocal / backing-track switch for that song becomes
   available.

The first start of the day also updates yt-dlp, so give it a minute.

Stop it with **ESC** on the karaoke screen, or with `exitkaraoke` from any terminal.

### Set an admin password (recommended for both)

Without one, *every* phone on the network counts as admin (see `is_admin` in `app.py`),
which means any guest can edit the queue, rename or delete songs, or quit the app. With
a password set, guests can still search for songs and add them to the queue. Player
controls (pause, skip), queue editing, song editing and shutdown need an admin login in
the web UI. If you'd rather let guests skip and pause, leave the password off.

The password is a startup option: `--admin-password <pick-one>`. The next step shows
where to put it for each option.

## 7. Option A: manual start

Nothing more to install. Run `openkaraoke` when you want karaoke, and stop it with
ESC or `exitkaraoke` when you're done.

To always start with your admin password, add this line to `~/.zshrc` and open a new
Terminal window:

```bash
alias openkaraoke='openkaraoke --admin-password <pick-one>'
```

**Sleep is handled for you.** While the app is running, `openkaraoke` keeps the Mac and
the display awake, so the TV doesn't go black between songs and phones don't lose the
connection. When the app stops, your normal sleep settings apply again. No macOS
setting is changed.

Two steps from Option B are useful here too: [8d](#8d-manage-it-without-a-keyboard)
(control the mini from your MacBook) and [8e](#8e-keep-the-address-stable)
(stop the QR code's address from changing).

## 8. Option B: dedicated karaoke box

### 8a. Start automatically at login

```bash
~/OpenHomeKaraoke/deploy/macos/launch-agent.sh install --admin-password <pick-one>
```

This starts the app now and at every login. Any arguments after `install` are passed
to the app. Leave out `--admin-password` if you don't want one.

How it behaves:

- **If it crashes**, it restarts within about 10 seconds.
- **If you stop it** (ESC or `exitkaraoke`), it stays stopped until the next login or a
  `restart`. It won't come back while you're working on it.

### 8b. Log in automatically

System Settings → **Users & Groups** → *Automatically log in as* → your karaoke account.
(If this option is greyed out, FileVault is on. See step 0.)

### 8c. Stay reachable, and power back on after an outage

```bash
sudo pmset -a sleep 0 autorestart 1
```

- `sleep 0` keeps the mini reachable over the network even while the app is stopped,
  for example so you can SSH in and restart it.
- `autorestart 1` powers the mini back on after a power cut. Combined with 8a and 8b,
  the karaoke screen then comes back by itself.

You don't need to stop the *display* from sleeping: `openkaraoke` keeps it on while the
app runs.

Also, in System Settings → **Lock Screen**, set the screen saver to *Never* and
*Require password after screen saver begins or display is turned off* to *Never*.

### 8d. Manage it without a keyboard

System Settings → General → **Sharing**: turn on **Remote Login** (SSH) and
**Screen Sharing**. Then, from your MacBook:

- `ssh <account>@<mini-name>.local` to run commands
- Finder sidebar → Network → the mini → *Share Screen*, to see and control the TV
  output

### 8e. Keep the address stable

The QR code holds the mini's IP address, e.g. `http://192.168.0.179:5000`. Your router
hands out these addresses (that's DHCP) and can give the mini a different one after a
restart or a power cut. The QR code on the TV always shows the current address, but
bookmarks and home-screen icons on phones would stop working. The fix is a **DHCP
reservation**: you tell the router "always give this device this address". Nothing
changes on the Mac. Routers also call it *address reservation*, *static lease*,
*fixed IP* or *static DHCP*.

**1. Note the mini's current address and its MAC address.** On the mini: System Settings
→ Network → **Ethernet** → *Details*. *TCP/IP* shows the IP address (e.g.
`192.168.0.179`) and *Router* (e.g. `192.168.0.1`); *Hardware* shows the MAC address,
six pairs like `a1:b2:c3:d4:e5:f6`. From Terminal:

```bash
ipconfig getifaddr en0; networksetup -getmacaddress Ethernet
```

A router tells devices apart by MAC address, and an Ethernet port's never changes. (On
Wi-Fi, macOS may use a rotating private address instead. If the mini is on Wi-Fi, set
Wi-Fi → *Details* → *Private Wi-Fi address* to **Fixed** first.)

**2. Open the router's settings.** Either:

- **A web page:** open the *Router* address from step 1 (e.g. `http://192.168.0.1`) in a
  browser. The admin password is often on a sticker on the router. If you never set
  one, try the router's app, or search "<router model> default admin password".
- **An app:** mesh systems (eero, Google Nest Wifi, TP-Link Deco, Netgear Orbi) and many
  ISP routers (Xfinity, Spectrum, AT&T) are set up from a phone app instead.

**3. Reserve the address.** Find the list of connected devices and the mini in it (by
its name or MAC address). Then look for one of these:

| Router | Where |
|---|---|
| Most web pages (TP-Link, Netgear, ASUS, Linksys) | *LAN* or *DHCP Server* → *Address Reservation* / *DHCP Reservation* → add the MAC and IP |
| eero | Devices → the mini → *Advanced* → *Reserve IP* |
| Google Nest Wifi / Google Wifi | Google Home → Wi-Fi → Settings → Advanced networking → *DHCP IP reservations* |
| TP-Link Deco | More → Advanced → *Address Reservation* |
| Netgear Orbi (web page) | Advanced → Setup → LAN Setup → *Address Reservation* |
| Xfinity | Connect → the mini → *Reserve IP* |

Reserve the address the mini **already has** (step 1): then nothing needs to change now.
Save, or *Apply*.

**4. Check it.** Restart the mini, or on the mini: Network → Ethernet → *Details* →
TCP/IP → **Renew DHCP Lease**. The address in step 1 should be the same as before.

**If your router can't reserve addresses,** set the address on the Mac instead: Network →
Ethernet → *Details* → TCP/IP → *Configure IPv4*: **Manually**, and enter an IP address,
subnet mask (usually `255.255.255.0`) and router, plus the router's address as DNS
server. Pick an address the router won't hand to another device, i.e. outside its DHCP
range (shown in its LAN/DHCP settings), e.g. `192.168.0.250`. A reservation is better:
if you ever change routers, a manually set address can leave the mini unreachable.

**Bonus: a name instead of a number.** iPhones and Macs also reach the mini by name, e.g.
`http://Mac-Mini.local:5000` (the *Local hostname* at the bottom of the Sharing
settings). That works even if the IP address changes, so it's a good bookmark for
iPhones. Many Android phones don't support `.local` names, and the QR code uses the IP
address, so the reservation is still worth doing.

---

## Switching between the two

**Manual → dedicated box:** do step 8.

**Dedicated box → manual:**

```bash
~/OpenHomeKaraoke/deploy/macos/launch-agent.sh uninstall
```

That's the only required step: the app no longer starts at login, and `openkaraoke`
works as in Option A. To also put the Mac back to normal, turn off automatic login
(Users & Groups), run `sudo pmset restoredefaults` to restore the default power
settings, and turn FileVault back on if you want it.

## Day to day

| | A. Manual start | B. Dedicated box |
|---|---|---|
| Start | `openkaraoke` | Starts itself. To start it again after stopping it: `~/OpenHomeKaraoke/deploy/macos/launch-agent.sh restart` |
| Stop | ESC or `exitkaraoke` | ESC or `exitkaraoke` (it stays stopped until the next login or a `restart`) |
| Logs | In the Terminal window you started it from | `tail -f ~/Library/Logs/OpenHomeKaraoke.log` |
| Is it running? | Is the karaoke screen up? | `~/OpenHomeKaraoke/deploy/macos/launch-agent.sh status` |

**Fullscreen:** the screen says to press **F** to toggle fullscreen. If you leave
fullscreen during a song, the VLC window takes keyboard focus and F stops working.
To get it back, use the **Toggle fullscreen** button on the web UI's Info page
(admin only).

## Updating

```bash
cd ~/OpenHomeKaraoke
git pull
.venv/bin/python -m pip install -r requirements.txt
```

Then restart the app. For manual start, run `exitkaraoke` and then `openkaraoke`. For
a dedicated box, run `deploy/macos/launch-agent.sh restart`.

yt-dlp updates itself when the app starts (at most once a day), so YouTube fixes arrive
without a `git pull`.

## Troubleshooting

**The QR code opens a blank page.**
AirPlay Receiver has port 5000 (see step 6). Turn it off, then restart the app.

**Phones can't open the page.**
Check that the phone is on the same network as the mini. Guest Wi-Fi networks usually
block device-to-device traffic. If the macOS firewall is on (System Settings → Network →
Firewall), allow incoming connections for Python.

**No sound, or sound from the mini's speaker instead of the TV.**
System Settings → Sound → Output → choose the TV.

**Downloads fail with "Sign in to confirm you're not a bot".**
YouTube wants cookies from a signed-in browser. The app reads cookies from the
**default browser** automatically, but only if that browser is Chrome, Brave, Edge,
Vivaldi, Opera, Chromium, Firefox or Safari (see `get_default_browser_cookie_osx` in
`app.py`). The easiest fix:

1. Install Chrome, sign in to YouTube, and set Chrome as the default browser.
2. Restart the app (`exitkaraoke` then `openkaraoke`, or `launch-agent.sh restart`)
3. macOS will ask once for access to *Chrome Safe Storage*. The prompt appears on the TV.
   Click **Always Allow**.

**`AttributeError: module 'pkgutil' has no attribute 'get_loader'`.**
The venv was created with the wrong Python. Delete `.venv` and redo step 3 using the
exact `python3.12` path shown there.

**The app didn't start at login (dedicated box).**
Run `launch-agent.sh status`, then check the end of `~/Library/Logs/OpenHomeKaraoke.log`.
A non-zero "last exit code" plus a Python traceback in the log usually points straight
at the cause.

**The log file keeps growing (dedicated box).**
It grows by about 4 MB a day, mostly from routine polling requests. To clear it:
`: > ~/Library/Logs/OpenHomeKaraoke.log`
