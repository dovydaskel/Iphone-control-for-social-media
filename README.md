# iPhone Control for Social Media

Drive a real, unmodified iPhone from a Mac — no jailbreak, no app on the phone, no
accessibility API, no network on the phone's side. The Mac watches the iPhone's screen
over the USB cable and moves a **Bluetooth mouse pointer** on it through an ESP32, so
every tap is a genuine hardware HID event on real hardware.

On top of that there's a scheduler that posts to TikTok and Instagram: it drops a video
into Photos over USB, scrolls the feed for a few minutes like a person would, then walks
the real posting UI — picks the video, swaps the sound for the next favourite, types the
caption on the on-screen keyboard, and hits **Post** or **Save draft**.

> [!WARNING]
> **Status: accounts posted to this way are currently getting shadow banned.** Views
> collapse to near-zero on the posted videos even though the posts themselves go through
> and look normal from the account's side. The humanised input in this project is clearly
> not enough on its own — the detection is happening somewhere else (device/account
> fingerprint, posting cadence, the content itself, or some combination). Treat the
> TikTok/Instagram flows as **not working for reach** right now, and don't point this at an
> account you care about. The hardware control layer underneath — BLE HID pointer, screen
> capture, OCR, USB media import — works fine and is the reusable part.

```
┌─────────────┐   USB (AVFoundation)   ┌─────────────┐
│             │ ◀───── screen video ───│             │
│     Mac     │   USB (AFC/usbmux)     │   iPhone    │
│  (FastAPI   │ ─────  files ─────────▶│   SE 2020   │
│   server)   │                        │             │
│             │   USB serial 115200    │             │
│             │ ──▶ ┌──────────┐       │             │
└─────────────┘     │  ESP32   │ ─ BLE │             │
       ▲            │ HID mouse│ ─────▶│ AssistiveTouch
       │            └──────────┘       └─────────────┘
   browser UI
 (localhost / Tailscale)
```

**Why a mouse?** iOS has no public automation API, but it does support Bluetooth mice for
accessibility. Turning on **AssistiveTouch → Pointer Devices** gives you a grey circular
cursor that iOS treats as a finger. A BLE HID mouse can therefore tap, long-press, swipe
and scroll anything on the phone, in any app, with nothing installed on the phone.

The catch is that HID mice send *relative* movement and iOS applies pointer acceleration,
so the Mac never knows where the cursor is. It finds out by **looking**: it tracks that
grey disc in the live video feed with OpenCV and closes the loop, and it reads on-screen
labels with Apple's Vision OCR instead of hard-coding coordinates.

---

## Table of contents

- [Hardware](#hardware)
- [How it works](#how-it-works)
- [Repository layout](#repository-layout)
- [Setup](#setup)
  - [1. Mac](#1-mac)
  - [2. ESP32](#2-esp32)
  - [3. iPhone](#3-iphone)
- [Running it](#running-it)
- [The web UI](#the-web-ui)
- [Routines (the scheduler)](#routines-the-scheduler)
- [HTTP API](#http-api)
- [WebSocket protocol](#websocket-protocol)
- [ESP32 serial protocol](#esp32-serial-protocol)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Limits and known quirks](#limits-and-known-quirks)
- [Running more than one phone](#running-more-than-one-phone)
- [Legal / terms of service](#legal--terms-of-service)
- [License](#license)

---

## Hardware

This is the exact rig the project was built and tested on.

| Part | What was used | Why |
| --- | --- | --- |
| **Phone** | **iPhone SE (2020)** — 2nd gen, 4.7″, **750 × 1334** | Cheap, plentiful second-hand, and the Home button model is easiest: screen capture over Lightning works, and the layout coordinates in `server/tiktok.py` were measured on this screen. |
| **Powered USB hub** | **[Powered USB hub (Amazon.de, B0797NZFYP)](https://www.amazon.de/-/en/dp/B0797NZFYP?ref=ppx_yo2ov_dt_b_fed_asin_title&th=1)** | The phone stays plugged in 24/7 for screen capture *and* file transfer, so it must charge at the same time. A hub with its **own power supply** is essential — a Mac's bus-powered ports will brown-out once you add phones and ESP32 boards. Per-port power also lets you hard-reset a phone or an ESP without unplugging anything. |
| **Microcontroller** | **ESP32 dev board** (`esp32dev`, e.g. ESP32-WROOM-32 DevKitC) | Any plain ESP32 with USB-serial works. It needs classic ESP32 BLE (NimBLE) to advertise as a HID mouse; an ESP32-S2 has no Bluetooth and will not work. |
| **Cables** | USB-A → Lightning (data), USB-A → micro-USB / USB-C for the ESP32 | Charge-only cables will not carry screen capture — use data cables. |
| **Host** | Any Mac (Apple silicon or Intel) running macOS | The screen capture and OCR helpers are Swift and use AVFoundation + Vision, so macOS is required. |

### Wiring

There is none. Everything is USB:

1. Powered hub → Mac.
2. iPhone → hub (data cable). Unlock it and tap **Trust This Computer**.
3. ESP32 → hub. It shows up as `/dev/cu.usbserial-XXXX`.
4. The ESP32 pairs to the iPhone **over Bluetooth**, not over any wire.

The ESP32 never touches the Mac's Bluetooth stack and the Mac never touches the phone's
Bluetooth — the only thing the Mac does with the ESP is write ASCII lines to a serial port.

---

## How it works

### Screen capture — `capture/capture.swift`

macOS exposes a connected iPhone as an **external muxed AVFoundation capture device** (this
is the same mechanism QuickTime's "New Movie Recording → iPhone" uses). The helper flips the
private `kCMIOHardwarePropertyAllowScreenCaptureDevices` flag to make those devices visible,
grabs frames at 30 fps, JPEG-encodes them and writes them to stdout, each frame prefixed
with a 4-byte big-endian length. The Python server reads that stream and keeps the latest
frame; the browser gets the same JPEGs pushed down a WebSocket.

### Pointer control — `firmware/src/main.cpp` + `server/app.py`

The ESP32 advertises itself as a BLE HID mouse (`NimBLE-Arduino`, appearance `0x03C2`) with
a standard 3-button + X/Y/wheel report map, and exposes a tiny line protocol over USB serial.
The Mac sends `M dx dy`, `C`, `D`/`U`, `W n`; the ESP turns them into HID reports.

Because the reports are relative, `Controller` runs a closed loop:

1. **Find the cursor.** `find_pointer_candidates()` masks out grey pixels (`|B−G|<10`,
   `|G−R|<10`, `140 ≤ G ≤ 170`) and keeps 32–44 px blobs that are roughly square — that's
   the AssistiveTouch disc. If the background is grey too, `find_moved_discs()` nudges the
   mouse ±30 units and diffs two frames instead, which works over any wallpaper or video.
2. **Move, predict, measure.** It converts pixels to mouse units with a learned gain
   (`kx`, `ky` ≈ 1.58 screen px per unit on this phone), sends the move, waits
   `FRAME_LATENCY` (150 ms) for a fresh frame, and finds where the cursor actually landed.
3. **Correct and learn.** Up to 5 iterations until it's within 3 px. Long moves feed the
   measured gain back into `kx`/`ky` with an exponential average, so pointer acceleration
   and per-phone differences are absorbed automatically.

When the cursor can't be tracked — over a playing TikTok video, for instance — the code
falls back to **`tap_exact()`**: slam the pointer into the nearest screen corner with
`M ±5000 ±5000` (a known absolute position), then dead-reckon to the target using the
*calibrated* gain, which only `calibrate()` is allowed to change. Slower, but it never
loses the cursor. The whole TikTok/Instagram flow uses it.

### Reading the screen — `capture/ocr.swift`

`ocr` takes a JPEG on stdin and returns `[{text, confidence, x, y, w, h}]` from Apple's
**Vision** `VNRecognizeTextRequest`, in image pixels, origin top-left. Everything the
automation clicks is found by label (`"Next"`, `"Save draft"`, `"For You"`) rather than by
fixed coordinate, so an app update that shifts a button sideways doesn't break the flow.
Fixed coordinates are only a fallback, and they're scaled from the measured 750-wide layout
by `self.s = width / 750`.

### Pressing Home without a Home button

The pointer can't press a physical button, so `press_home()` taps the **AssistiveTouch
button** (located by masked template match against `server/assets/assistive_touch.png`) and
then taps **Home** in the menu that opens. It tells that "Home" apart from the Home *app*
icon by requiring two other menu labels (`Device`, `Siri`, `Control Center`, …) to be on
screen at the same time. The button's last known position is cached in `calibration.json`,
because when idle it fades out and can't be found by template match at all.

### Looking human — `server/humanize.py`

Every scripted action can run in `human=True` mode:

- **Path** — cubic Bézier with randomly placed control points, so moves arc slightly.
- **Speed** — duration from **Fitts's law** (`MT = a + b·log2(D/W + 1)`, randomised ±20%)
  with a **minimum-jerk** velocity profile: accelerate, then ease into the target.
- **Tremor** — 1D Perlin noise, ~1 px, strongest mid-move and faded out at the target.
- **Aim** — a Gaussian miss of a few pixels, clamped, instead of always hitting dead centre.
- **Timing** — log-normal reaction delay before acting, random 60–140 ms click hold.
- **Dwell** — `watch_time()` returns mostly 5–20 s per short video with a 12% chance of a
  quick 1.5–3.5 s skip, capped at 25 s so the screen never hits iOS's 30 s auto-lock.

Because iOS gives less pointer gain to a slow, eased human-style move than to a
constant-speed one, `human_gain` is learned separately from `kx`/`ky` by measuring how much
of each planned glide actually happened.

### Getting video onto the phone — `server/usb_media.py`

No cloud, no AirDrop, no network. Files are pushed into **VLC's Documents** folder over AFC
(`pymobiledevice3` House Arrest — App Store apps only expose `Documents`, which is enough).
A Shortcut on the phone called **USB Import** reads that folder and runs *Save to Photo
Album*. The server taps the Shortcut's home-screen icon with the mouse, then watches the
camera roll (`/DCIM`) over AFC until the expected number of new files appear, and finally
clears the folder from the Mac side. Only "Trust This Computer" is needed — no Developer Mode.

---

## Repository layout

```
.
├── run.sh                      # build the Swift helpers if needed, start the server
├── requirements.txt
├── capture/
│   ├── capture.swift           # iPhone screen → length-prefixed JPEG frames on stdout
│   └── ocr.swift               # JPEG on stdin → Vision OCR boxes as JSON
├── firmware/
│   ├── platformio.ini          # esp32dev + NimBLE-Arduino
│   └── src/main.cpp            # BLE HID mouse driven by a serial line protocol
├── server/
│   ├── app.py                  # FastAPI app, Capture/ESP/Controller, command worker
│   ├── humanize.py             # Bézier paths, Fitts's law, min-jerk, Perlin tremor
│   ├── tiktok.py               # TikTok draft/post flow + the on-screen keyboard driver
│   ├── instagram.py            # Instagram reel flow (reuses tiktok.ScreenFlow)
│   ├── schedule.py             # routines: steps, per-routine video queue, cron thread
│   ├── media.py                # upload queue and per-file status
│   ├── usb_media.py            # AFC push into VLC's Documents, camera-roll watching
│   ├── assets/
│   │   └── assistive_touch.png # template for locating the AssistiveTouch button
│   └── static/index.html       # the whole web UI (one file, no build step)
└── tools/
    └── send_to_phone.py        # CLI: push local files into Photos via the server
```

Runtime state is written next to the code and is **git-ignored**: `calibration.json`
(learned gain + AssistiveTouch position), `tiktok_state.json` (caption, last sound used,
last result), `routines.json` (your schedule), and the `media/` + `routine_videos/` folders.

---

## Setup

### 1. Mac

```bash
git clone https://github.com/dovydaskel/Iphone-control-for-social-media.git
cd Iphone-control-for-social-media

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The two Swift helpers are compiled automatically on first run by `run.sh` (it needs the
Xcode command line tools: `xcode-select --install`). To build them by hand:

```bash
swiftc -O capture/capture.swift -o capture/capture
swiftc -O capture/ocr.swift     -o capture/ocr
```

Check that the Mac can see the phone's screen:

```bash
./capture/capture --list     # prints "<uniqueID>	<name>" per connected iPhone
```

### 2. ESP32

```bash
.venv/bin/pio run -d firmware -t upload     # build + flash
.venv/bin/pio device monitor -d firmware    # should print "EVT ready"
```

Set the port in `firmware/platformio.ini` (`upload_port` / `monitor_port`) if your board
isn't on `/dev/cu.usbserial-0001`.

With the monitor open you can drive the mouse by hand — type `?` for status, `M 100 0` to
move, `C` to click.

### 3. iPhone

**a. Pair the ESP32 as a mouse**

1. **Settings → Accessibility → Touch → AssistiveTouch → On.**
2. **Devices → Bluetooth Devices** → pick **ESP Mouse 1** → pair.
3. A grey circular pointer appears. Confirm it moves when you send `M 100 0` over serial.

**b. Phone settings that matter**

| Setting | Value | Why |
| --- | --- | --- |
| Display & Brightness → Auto-Lock | **Never** (or 5 min) | Locked screen = no video and no taps. |
| Accessibility → Touch → AssistiveTouch → Idle Opacity | high | The faded button can't be template-matched. |
| Settings → Photos → iCloud Photos | **off** recommended | "Delete all videos" would propagate to every device. |
| Low Power Mode | off | It throttles the screen-capture pipeline. |
| Passcode | none, for a dedicated phone | `wake()` unlocks by pressing Home; a passcode would need typing. |

Leave the AssistiveTouch button parked somewhere predictable (the right edge works well)
— the server caches its position and re-finds it from there.

**c. The `USB Import` Shortcut** (needed for *Send to Photos* and routine video steps)

1. Install **VLC** from the App Store and open it once.
2. With the phone plugged in, press **Create import folder** in the web UI's *Send to
   Photos* card (this calls `POST /api/usb/folder`).
3. On the phone: **Shortcuts → +**.
4. Add **Get Contents of Folder** → pick **On My iPhone › VLC › Mac Import**.
5. Add **Save to Photo Album** (Recents) with the folder contents as input. No delete step
   is needed — the Mac clears the folder itself.
6. Name it exactly **`USB Import`**, then **Share → Add to Home Screen**.
7. Run it once by hand and answer every permission prompt with **Always Allow**.

**d. The `Delete Videos` Shortcut** (needed for *clear first* / *delete videos* steps)

1. **Shortcuts → +** → **Find Photos** → **Add Filter** → *Media Type is Video*. Leave Limit off.
2. Add **Delete Photos** with the found photos as input.
3. Name it exactly **`Delete Videos`**.
4. Put it on the home screen as a **small single-shortcut widget**, to the right of the
   USB Import icon.
5. Run it once by hand and accept the prompts. iOS still asks *"Allow … to delete?"* on
   every run — the server finds that prompt by OCR and taps **Delete** for you.

> ⚠️ This deletes **every** video in Photos (into Recently Deleted for 30 days). Use a
> dedicated phone, and keep iCloud Photos off.

---

## Running it

```bash
./run.sh            # listen on 0.0.0.0:8000 (also prints the Tailscale URL if present)
./run.sh --local    # 127.0.0.1 only
```

Then open <http://127.0.0.1:8000>.

**There is no authentication.** `run.sh` binds to all interfaces so you can reach the phone
from your own laptop or phone over **Tailscale**; don't expose port 8000 to anything less
private than that.

First time, in this order:

1. Check the *Status* card: **ESP** green (`connected`) and **Feed** showing ~30 fps.
2. Press **Wake & unlock**, then **Home**.
3. Open a light screen (Settings works well) and press **Calibrate pointer**. It measures
   the gain in both axes and writes `calibration.json`. Re-run it if you change phone,
   AssistiveTouch tracking speed, or iOS version.

---

## The web UI

One `static/index.html`, no build step, dark, collapsible cards, and usable from a phone
browser. The live screen is a `<canvas>` fed JPEG frames over the WebSocket.

**Direct control** — on the canvas: click = tap, hold = long press, drag = swipe,
wheel = scroll. Ripples show where your tap landed.

**Controls** — Wake & unlock · Home · Calibrate pointer · Pointer to corner.

**Hands-free scrolling** — pick TikTok or Instagram Reels and a duration. It opens the app
from the home screen, works its way to the right feed (`For You` / the Reels tab, which
Instagram moved from 4th to 2nd place in 2025 — both are tried), dismisses sign-up sheets
and prompts (`Not now`, `Skip`, `Maybe later`, … plus a geometric white-bottom-sheet
detector), then swipes to the next video on a human dwell time. Shows videos watched and
time left.

**TikTok / Instagram** — the caption, **Finish with: Drafts / Post**, and *"then do the
same on Instagram"*. Running it does:

> TikTok → `+` → pick the only video in Photos → open the sound sheet → **Favorites** tab →
> pick the next favourite sound, cycling (it remembers the last one in `tiktok_state.json`)
> → post page → type the caption on the on-screen keyboard → **Drafts** or **Post**.

With Instagram enabled it then waits for TikTok's *saved copy* of the posted video (which
carries the sound) to land in Photos, and makes a reel of it with the same caption →
**Save draft** or **Share**. There's also an **Instagram only** button that posts the 2nd
video in the gallery, for resuming a run whose TikTok half already succeeded.

The keyboard driver in `tiktok.py` is a small model of the iOS keyboard: it locates the rows
from the bottom-left `123`/`ABC` key, handles the letter / `123` / `#+=` pages, shift state,
`@`, `#` and newlines, and normalises smart quotes. **Emoji and accented characters are
skipped** and reported back as `skipped_chars`.

**Schedule** — routines, see below.

**Send to Photos** — pick files, watch each one go
`queued → sending → copying → importing → downloaded`. Plus **Delete all videos from
Photos** and the two collapsible one-time setup guides.

**Status** — ESP name/link, capture fps and frame age, busy/queued, last pointer position,
the live `kx`/`ky`/`human_gain`, last action timing, last error.

---

## Routines (the scheduler)

A routine is a named list of steps that runs at `HH:MM` on chosen weekdays, with an optional
**random delay** of up to 120 minutes so it never fires on the exact same minute twice.

Step types:

| Step | What it does |
| --- | --- |
| `send_video` | Takes the **next video from this routine's own queue** and imports it into Photos over USB. With `clear_first` (default on) it deletes the videos already there first, so the new one is the only candidate for the upload step. |
| `autoscroll` | Scroll TikTok or Instagram Reels for *n* minutes (1–240). |
| `upload` | The TikTok (and optionally Instagram) flow, with `finish: draft \| post`. An empty description falls back to the one saved in the TikTok / Instagram card. |
| `delete_videos` | Run the Delete Videos Shortcut. |
| `wait` | Sleep *n* minutes (interruptible by **Stop routine**). |

Each routine keeps its **own upload queue** in `routine_videos/<id>/`, so you can drop in a
week of videos at once and each run consumes the next one. A typical "night post":

```
02:30 (+ up to 30 min random)   every day
  1. send next video to Photos (clear first)
  2. scroll TikTok 5 min
  3. scroll Instagram 5 min
  4. post on TikTok + Instagram
```

The warm-up scrolling before posting is deliberate: a session that opens the app and
immediately uploads looks nothing like a person's.

A background thread checks every 10 s and queues a routine **once per day** when its time
comes. Times are **this Mac's local clock**, so the Mac must be awake — a run missed while
asleep is skipped unless the Mac wakes within the 60-minute `GRACE` window. A routine stops
at the first step that fails and records the error in `last_result`.

---

## HTTP API

The UI is a client of this; everything is reachable with `curl`.

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/` | The web UI. |
| `GET` | `/api/media` | Upload queue with per-item status. |
| `POST` | `/api/media` | `file=@…`, `send=true\|false`. Queues a file; `send=true` starts the import. |
| `GET` | `/api/media/{id}` | One item. |
| `POST` | `/api/media/{id}/send` | Re-queue a single item. |
| `DELETE` | `/api/media/{id}` | Drop it from the queue. |
| `POST` | `/api/media/send` | Import everything queued — one Shortcut run for all of it. |
| `POST` | `/api/usb/folder` | Create `VLC/Documents/Mac Import` on the phone. |
| `GET` | `/api/tiktok` | Saved caption, last sound used, last run's result. |
| `POST` | `/api/tiktok` | `description`, `finish=draft\|post`, `instagram`, `run`, `instagram_only`. |
| `GET` | `/api/routines` | All routines with `next_run` and a human-readable `summary`. |
| `POST` | `/api/routines` | JSON body `{id?, name, enabled, time, days, jitter, steps}`. |
| `DELETE` | `/api/routines/{id}` | Delete it and its video folder. |
| `POST` | `/api/routines/{id}/videos` | Multipart `files=@…` — append to the queue. |
| `DELETE` | `/api/routines/{id}/videos/{vid}` | Remove one queued video. |
| `POST` | `/api/routines/{id}/videos/{vid}/move` | `delta=-1` / `delta=1` to reorder. |
| `POST` | `/api/routines/{id}/run` | Run it now. |
| `WS` | `/ws` | Frames + status down, commands up. |

Push files from the command line:

```bash
.venv/bin/python tools/send_to_phone.py clip1.mp4 clip2.mp4
# optional: --server http://mac:8000  --no-wait  --timeout 180
```

It uploads all the files, triggers **one** import run, follows each item's status and exits
non-zero if any failed.

---

## WebSocket protocol

`/ws` sends **binary** messages (JPEG frames, as captured) and **text** messages (a JSON
status object every 0.5 s — the same shape as `status_payload()`).

Commands are JSON text messages. They're put on a single queue and executed one at a time by
the worker thread, so the phone is never asked to do two things at once:

```jsonc
{"type": "tap",        "x": 375, "y": 700, "human": false}
{"type": "tap_exact",  "x": 375, "y": 700}        // dead reckoning from a corner
{"type": "move",       "x": 375, "y": 700}
{"type": "long_press", "x": 375, "y": 700, "seconds": 1.5}   // capped at 5
{"type": "swipe",      "x1": 300, "y1": 900, "x2": 300, "y2": 300}
{"type": "scroll",     "amount": -5}
{"type": "wake"}            {"type": "home_button"}
{"type": "home_pointer"}    {"type": "calibrate"}
{"type": "autoscroll", "app": "tiktok", "minutes": 15}
{"type": "stop_autoscroll"} {"type": "stop_routine"}
{"type": "tiktok_draft"}    {"type": "delete_videos"}
```

Any action takes `"human": true` to get the humanised motion described above.

---

## ESP32 serial protocol

115200 baud, one command per line, each answered with `OK …` or `ERR …`. Connection changes
arrive unsolicited as `EVT connected` / `EVT secured` / `EVT disconnected reason=N`.

| Command | Meaning |
| --- | --- |
| `M dx dy` | Relative move, split into `STEP`-sized reports `DELAY` ms apart. |
| `R dx dy` | One raw report immediately (−127…127) — the host controls the timing. Used for humanised glides at 8 ms per report. |
| `D` / `U` | Press / release left button (move while pressed = drag = swipe). |
| `C` | Click (down, 60 ms, up). |
| `W n` | Scroll wheel. |
| `STEP n` | Max units per report, 1–127 (default 10; `home()` temporarily uses 127). |
| `DELAY ms` | Pause between move reports (default 8). |
| `?` | `OK name=… connected=… secured=… step=… delay=…` |

The Python side is defensive about one specific failure: the iPhone sometimes drops the BLE
link and never comes back (seen after the phone has been locked for hours). Any `ERR not
connected` makes the server **reboot the ESP32 by pulsing EN via RTS**, after which the phone
re-pairs within seconds; the idle loop does the same every 120 s while disconnected, so a
2:30 a.m. routine finds a working mouse.

---

## Configuration

All optional environment variables, read at server start:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ESP_PORT` | `/dev/cu.usbserial-0001` | Serial port of the ESP32. |
| `USB_IMPORT_SHORTCUT` | `USB Import` | Home-screen label of the import Shortcut. |
| `DELETE_VIDEOS_SHORTCUT` | `Delete Videos` | Home-screen label of the delete Shortcut. |
| `USB_IMPORT_APP` | `org.videolan.vlc-ios` | Bundle id whose `Documents` is used as the drop folder. |
| `USB_IMPORT_FOLDER` | `Mac Import` | Folder name inside that app's `Documents`. |
| `INSTAGRAM_REELS_SLOTS` | `2,4` | Which of the 5 bottom-bar slots to try for the Reels tab. |

Tunables in the source: `FRAME_LATENCY` (0.15 s), `PRECISE_STEP` (10), `HOME_STEP` (127) in
`app.py`; `REPORT_INTERVAL` (8 ms) and the Fitts's-law constants in `humanize.py`; `GRACE`
(60 min) in `schedule.py`; the 750-wide key coordinates in `tiktok.py`.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| `No iPhone found. Is it connected, unlocked and trusted?` | Unlock the phone and tap **Trust This Computer**. Use a **data** cable, straight into the powered hub. |
| Feed shows 0 fps / frame age climbing | Something else grabbed the capture device — quit QuickTime. Re-plug the phone. |
| **ESP** red, `ERR not connected` | The phone dropped the BLE link. The server reboots the ESP automatically; if it never recovers, re-pair under **AssistiveTouch → Devices**. |
| `pointer not found; open a light screen and retry` | Calibrate on a light, static screen (Settings). A dark or busy screen hides the grey disc. |
| `AssistiveTouch menu didn't open` | The AssistiveTouch button moved. Drag it back near where it was, raise its idle opacity, and press **Home** again — the server re-finds and re-caches it. |
| `no "TikTok" icon on the home screen` | The icon must be on one of the first 4 home-screen pages, with its **label visible** (OCR finds the label, then taps 60 px above it). |
| `the "USB Import" Shortcut saved 0 of N files` | A prompt is waiting on the phone's screen. Run the Shortcut by hand once and answer **Always Allow**. |
| `no delete prompt appeared` | Usually harmless — it means there were no videos to delete. The code treats that case as success in `clear_first`. |
| `TikTok's posted video didn't appear in Photos` | Turn on **Save to device** in TikTok's post settings; the Instagram step needs that copy. |
| Caption is missing characters | Expected: emoji and accented letters can't be typed on the modelled keyboard and are reported as `skipped_chars`. |
| Taps land slightly off | Re-run **Calibrate pointer**. If iOS pointer tracking speed changed, `kx`/`ky` are stale. |

---

## Limits and known quirks

- **macOS only** — `capture.swift` and `ocr.swift` use AVFoundation and Vision.
- **Classic ESP32 only** — the S2 has no Bluetooth.
- **One phone per server process.** Run more instances for more phones (see below).
- **No authentication** on the server. Keep it on localhost or Tailscale.
- **Coordinates are tuned for 750 × 1334.** Everything scales by `width / 750`, but
  home-indicator phones (no Home button) have different safe areas and the fixed fallback
  coordinates will need adjusting.
- **App UIs change.** The flows are OCR-first and retry a lot, but a TikTok or Instagram
  redesign can still break a step. Errors name the step that failed.
- **The Mac must stay awake** for routines to fire.
- **"Delete all videos" is destructive** and hits iCloud Photos if it's on.

---

## Running more than one phone

The design scales to a small fleet, which is what the powered hub is really for:

1. Flash each ESP32 with a distinct name — add
   `build_flags = -DDEVICE_NAME='"ESP Mouse 2"'` to `platformio.ini` — and pair each one to
   its own phone.
2. Give each phone its own copy of the `server/` state (its own working directory, so
   `calibration.json`, `routines.json` and `media/` stay separate).
3. Start one server per phone with its own port and serial port:

   ```bash
   ESP_PORT=/dev/cu.usbserial-0002 .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8001
   ```

Each phone then has its own URL, its own calibration and its own schedule.

---

## Legal / terms of service

Automating TikTok and Instagram is very likely against their terms of service, whatever the
input method. Accounts driven this way can be limited, shadow banned or banned outright —
and as noted at the top of this README, **shadow banning is what is actually happening
right now**. The humanised motion in this project exists because jittery robotic input is
unpleasant to watch and unreliable on real UIs — treat it as an engineering detail, not a
guarantee of anything, and clearly not as something that defeats detection.

Use this on accounts and devices you own, for content you have the rights to, and accept that
the risk is yours. It is published as a hardware/computer-vision project: closed-loop
relative-pointer control, OCR-driven UI walking, and USB media transfer on an unmodified
iPhone.

---

## License

MIT — see [LICENSE](LICENSE).
