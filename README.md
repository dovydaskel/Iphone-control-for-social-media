# iPhone Control for Social Media

Control a real, unmodified iPhone from a Mac — no jailbreak, no app on the phone. The Mac
watches the phone's screen over USB and moves a **Bluetooth mouse pointer** on it through an
ESP32, so every tap is a real hardware HID event. On top of that: a scheduler that drops a
video into Photos over the cable, scrolls the feed for a few minutes, then walks the real
TikTok/Instagram posting UI and hits **Post**.

> [!WARNING]
> **Accounts posted to this way are currently getting shadow banned.** Posts go through and
> look normal from the account's side, but views collapse to near-zero. The humanised input
> here is clearly not enough on its own — detection is happening somewhere else (device or
> account fingerprint, cadence, the content itself). Treat the TikTok/Instagram flows as
> **not working for reach** right now, and don't point this at an account you care about.
> The layer underneath — BLE HID pointer, screen capture, OCR, USB media import — works
> fine and is the reusable part.

```
┌─────────────┐  screen video (AVFoundation) ┌─────────────┐
│     Mac     │ ◀───────── USB ──────────────│   iPhone    │
│   FastAPI   │  files (AFC) ───────────────▶│   SE 2020   │
│   server    │  serial ──▶ ESP32 ─── BLE ──▶│ AssistiveTouch
└─────────────┘                               └─────────────┘
```

## How it works

iOS has no automation API, but **AssistiveTouch → Pointer Devices** accepts a Bluetooth
mouse and gives you a grey cursor that iOS treats as a finger. So a BLE HID mouse can tap,
swipe and scroll anything, in any app, with nothing installed on the phone.

HID movement is *relative* and iOS adds pointer acceleration, so the Mac never knows where
the cursor is — it **looks**. OpenCV finds the grey disc in the live video feed, the server
moves, re-checks, corrects, and learns the px-per-mouse-unit gain as it goes. Screen text is
read with Apple's **Vision** OCR, so buttons are found by label (`Next`, `Save draft`) rather
than hard-coded coordinates. When the cursor can't be tracked — over a playing video — it
slams the pointer into a screen corner (a known position) and dead-reckons from there.

Scripted actions can run in "human" mode: Bézier paths, Fitts's-law timing with a
minimum-jerk profile, Perlin-noise tremor, a few pixels of aim error, and realistic dwell
times between swipes.

## Hardware

| Part | Used | Notes |
| --- | --- | --- |
| Phone | **iPhone SE (2020)**, 750 × 1334 | Cheap second-hand, and the layout coordinates in `tiktok.py` were measured on this screen. |
| USB hub | **[Powered USB hub (Amazon.de B0797NZFYP)](https://www.amazon.de/-/en/dp/B0797NZFYP?ref=ppx_yo2ov_dt_b_fed_asin_title&th=1)** | Must have its **own power supply** — the phone stays plugged in 24/7 and has to charge while being captured. Bus-powered ports brown out. |
| Board | **ESP32 dev board** (`esp32dev`) | Any plain ESP32. An **S2 won't work** — no Bluetooth. |
| Cables | USB data cables (not charge-only) | Charge-only cables won't carry screen capture. |

No wiring — everything is USB into the hub. The ESP32 pairs to the phone over Bluetooth,
not over a wire.

## Setup

**Mac**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./capture/capture --list          # should list your iPhone
```

The two Swift helpers compile automatically on first run (needs `xcode-select --install`).

**ESP32**

```bash
.venv/bin/pio run -d firmware -t upload     # set upload_port in platformio.ini first
.venv/bin/pio device monitor -d firmware    # "EVT ready"; try `?`, `M 100 0`, `C`
```

**iPhone**

1. **Settings → Accessibility → Touch → AssistiveTouch → On**, then **Devices → Bluetooth
   Devices** → pair **ESP Mouse 1**. A grey pointer appears.
2. Plug the phone in and tap **Trust This Computer**.
3. Set **Auto-Lock → Never**, raise AssistiveTouch **idle opacity**, turn **iCloud Photos
   off**, and use a phone with **no passcode**.
4. For sending videos, install **VLC**, press **Create import folder** in the web UI, then
   build a Shortcut named **`USB Import`**: *Get Contents of Folder* (`On My iPhone › VLC ›
   Mac Import`) → *Save to Photo Album*. Add it to the home screen and run it once, allowing
   everything.
5. Optional, for the "clear first" step: a Shortcut named **`Delete Videos`** — *Find Photos*
   (Media Type is Video) → *Delete Photos*. Put it on the home screen as a small widget.

> ⚠️ `Delete Videos` deletes **every** video in Photos. Use a dedicated phone and keep
> iCloud Photos off.

## Running

```bash
./run.sh            # 0.0.0.0:8000 (prints the Tailscale URL if present)
./run.sh --local    # 127.0.0.1 only
```

Open <http://127.0.0.1:8000>. **There is no authentication** — keep it on localhost or
Tailscale.

First run: check *Status* shows **ESP connected** and ~30 fps, press **Wake & unlock**, then
open a light screen like Settings and press **Calibrate pointer**.

## The web UI

One `static/index.html`, no build step, works from a phone browser.

- **Live screen** — click = tap, hold = long press, drag = swipe, wheel = scroll.
- **Hands-free scrolling** — opens TikTok or Instagram Reels, finds the feed, dismisses
  sign-up prompts, and swipes on a human dwell time for *n* minutes.
- **TikTok / Instagram** — caption + *Drafts* or *Post*. Opens TikTok → `+` → picks the video
  → swaps the sound for the next one in **Favorites** (cycling) → types the caption on the
  on-screen keyboard → finishes. Optionally repeats it as an Instagram reel using TikTok's
  saved copy. *Emoji and accented characters can't be typed and are skipped.*
- **Schedule** — routines (below).
- **Send to Photos** — upload files and watch each one reach `downloaded`.

## Routines

A named list of steps that runs at `HH:MM` on chosen weekdays, plus an optional random delay
so it never fires on the same minute twice. Steps: `send_video` (takes the next video from
the routine's own queue), `autoscroll`, `upload`, `delete_videos`, `wait`.

```
02:30 (+ up to 30 min random), every day
  1. send next video to Photos (clear first)
  2. scroll TikTok 5 min
  3. scroll Instagram 5 min
  4. post on TikTok + Instagram
```

Each routine keeps its own video queue, so you can load a week at once. Times use **this
Mac's clock**, so the Mac must be awake. A routine stops at the first failing step.

## Layout

```
capture/     capture.swift (screen → JPEG frames), ocr.swift (Vision OCR → JSON)
firmware/    ESP32 BLE HID mouse driven by a serial line protocol
server/      app.py (FastAPI + pointer control), tiktok.py, instagram.py,
             schedule.py, humanize.py, media.py, usb_media.py, static/index.html
tools/       send_to_phone.py — push local files into Photos from the CLI
```

Runtime state (`calibration.json`, `routines.json`, `tiktok_state.json`, `media/`) is
git-ignored.

<details>
<summary><b>HTTP API</b></summary>

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/api/media` | `file=@…`, `send=true\|false` |
| `POST` | `/api/media/send` | Import everything queued in one Shortcut run |
| `GET`/`DELETE` | `/api/media[/{id}]` | List / inspect / drop queued files |
| `POST` | `/api/usb/folder` | Create the import folder on the phone |
| `GET`/`POST` | `/api/tiktok` | Caption, `finish=draft\|post`, `instagram`, `run` |
| `GET`/`POST`/`DELETE` | `/api/routines[/{id}]` | Manage routines |
| `POST` | `/api/routines/{id}/videos` | Append videos to a routine's queue |
| `POST` | `/api/routines/{id}/run` | Run now |
| `WS` | `/ws` | JPEG frames + status down; commands up |

WebSocket commands are JSON, queued and run one at a time: `tap`, `tap_exact`, `move`,
`long_press`, `swipe`, `scroll`, `wake`, `home_button`, `home_pointer`, `calibrate`,
`autoscroll`, `tiktok_draft`, `delete_videos`, `stop_autoscroll`, `stop_routine`. Any of them
takes `"human": true`.

```bash
.venv/bin/python tools/send_to_phone.py clip1.mp4 clip2.mp4
```

</details>

<details>
<summary><b>ESP32 serial protocol</b> (115200 baud, one line per command)</summary>

| Command | Meaning |
| --- | --- |
| `M dx dy` | Relative move, split into `STEP`-sized reports |
| `R dx dy` | One raw report now — host controls timing (used for human glides) |
| `D` / `U` | Press / release left button (drag = swipe) |
| `C` | Click |
| `W n` | Scroll wheel |
| `STEP n` / `DELAY ms` | Units per report (1–127) / pause between reports |
| `?` | Status |

Replies are `OK …` / `ERR …`; link changes arrive as `EVT connected|secured|disconnected`.
On `ERR not connected` the server reboots the ESP32 by pulsing EN via RTS — the phone
sometimes drops the BLE link after hours locked and only comes back after a reset.

</details>

<details>
<summary><b>Environment variables</b></summary>

| Variable | Default |
| --- | --- |
| `ESP_PORT` | `/dev/cu.usbserial-0001` |
| `USB_IMPORT_SHORTCUT` | `USB Import` |
| `DELETE_VIDEOS_SHORTCUT` | `Delete Videos` |
| `USB_IMPORT_APP` | `org.videolan.vlc-ios` |
| `USB_IMPORT_FOLDER` | `Mac Import` |
| `INSTAGRAM_REELS_SLOTS` | `2,4` |

For several phones: flash each ESP32 with `-DDEVICE_NAME='"ESP Mouse 2"'`, then run one
server per phone with its own `ESP_PORT`, port and working directory.

</details>

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `No iPhone found` | Unlock, **Trust This Computer**, use a data cable. |
| Feed at 0 fps | Something else grabbed the capture device — quit QuickTime. |
| `ERR not connected` | BLE dropped; the server reboots the ESP itself. If not, re-pair. |
| `pointer not found` | Calibrate on a light, static screen. |
| `AssistiveTouch menu didn't open` | The button moved — drag it back and raise its opacity. |
| `no "TikTok" icon on the home screen` | Icon must be on one of the first 4 pages with its label visible. |
| `Shortcut saved 0 of N files` | A prompt is waiting on the phone — run it by hand once. |
| Taps land slightly off | Re-run **Calibrate pointer**. |

## Limits

macOS only (AVFoundation + Vision). Classic ESP32 only. One phone per server process. No
auth. Coordinates are tuned for 750 × 1334 and scale by `width / 750`, so home-indicator
phones need adjusting. App redesigns can break a step.

## Legal

Automating TikTok and Instagram is very likely against their terms of service, and as noted
at the top, **shadow banning is what's actually happening**. The humanised motion exists
because robotic input is unreliable on real UIs — not as something that defeats detection.
Use your own accounts and devices, for content you have the rights to; the risk is yours.
This is published as a hardware/computer-vision project.

## License

MIT — see [LICENSE](LICENSE).
