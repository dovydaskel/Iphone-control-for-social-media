"""iPhone control server: streams the iPhone screen to the browser and turns clicks
on the feed into ESP32 mouse movements + clicks.

Run:  ../.venv/bin/uvicorn app:app --port 8000     (from this directory)
"""

import asyncio
import json
import logging
import math
import os
import queue
import random
import re
import struct
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
import serial
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import humanize
import instagram
import schedule
import tiktok
import usb_media
from media import MediaStore

ROOT = Path(__file__).resolve().parent.parent
CAPTURE_BIN = ROOT / "capture" / "capture"
OCR_BIN = ROOT / "capture" / "ocr"
ASSISTIVE_TOUCH_TEMPLATE = Path(__file__).resolve().parent / "assets" / "assistive_touch.png"
CALIBRATION_FILE = Path(__file__).resolve().parent / "calibration.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SERIAL_PORT = os.environ.get("ESP_PORT", "/dev/cu.usbserial-0001")
# Home-screen Shortcut that saves the files copied over USB (see usb_media.py) to Photos.
USB_IMPORT_SHORTCUT = os.environ.get("USB_IMPORT_SHORTCUT", "USB Import")
# Home-screen Shortcut that deletes every video in Photos (Find Photos -> Delete Photos).
DELETE_VIDEOS_SHORTCUT = os.environ.get("DELETE_VIDEOS_SHORTCUT", "Delete Videos")

# Time from the ESP finishing a move until the frame we grab reflects it.
FRAME_LATENCY = 0.15
# Precise moves use small steps so iOS pointer acceleration stays predictable.
PRECISE_STEP = 10
HOME_STEP = 127

log = logging.getLogger("iphone-control")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class Capture(threading.Thread):
    """Runs the Swift capture helper and keeps the latest JPEG frame."""

    def __init__(self):
        super().__init__(daemon=True)
        self.jpeg = None
        self.seq = 0
        self.timestamp = 0.0
        self.size = (0, 0)
        self.fps = 0.0
        self._cond = threading.Condition()

    def run(self):
        while True:
            proc = subprocess.Popen([str(CAPTURE_BIN), "--fps", "30"], stdout=subprocess.PIPE)
            log.info("capture started")
            frames, window_start = 0, time.time()
            while True:
                header = proc.stdout.read(4)
                if len(header) < 4:
                    break
                data = proc.stdout.read(struct.unpack(">I", header)[0])
                with self._cond:
                    if self.size == (0, 0):
                        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                        self.size = (img.shape[1], img.shape[0])
                    self.jpeg = data
                    self.seq += 1
                    self.timestamp = time.time()
                    self._cond.notify_all()
                frames += 1
                if time.time() - window_start >= 1:
                    self.fps = frames / (time.time() - window_start)
                    frames, window_start = 0, time.time()
            proc.wait()
            self.fps = 0
            log.warning("capture stopped (exit %s), retrying in 2s", proc.returncode)
            time.sleep(2)

    def wait_for_frame(self, t, timeout):
        """True if a frame newer than time t arrives within timeout."""
        deadline = time.time() + timeout
        with self._cond:
            while self.timestamp <= t and time.time() < deadline:
                self._cond.wait(deadline - time.time())
            return self.timestamp > t

    def frame_after(self, t, timeout=1.5):
        """Decoded frame captured after time t (or the latest one on timeout)."""
        deadline = time.time() + timeout
        with self._cond:
            while self.timestamp <= t and time.time() < deadline:
                self._cond.wait(deadline - time.time())
            data = self.jpeg
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR) if data else None


class ESP:
    """Line-based serial link to the ESP32 mouse firmware."""

    def __init__(self, port):
        self.port = port
        self.ser = None
        self.name = None
        self.connected = False
        self.last_reboot = 0.0

    def open(self):
        ser = serial.Serial()
        ser.port, ser.baudrate, ser.timeout = self.port, 115200, 10
        # Keep DTR/RTS released; the board still resets on open, so give it time to boot.
        ser.dtr = ser.rts = False
        ser.open()
        time.sleep(4)
        ser.reset_input_buffer()
        self.ser = ser
        self.status()

    def cmd(self, line):
        reply = self._cmd(line)
        if reply.startswith("ERR not connected"):
            # The iPhone can drop the Bluetooth link and not come back (seen after hours
            # locked); after an ESP reboot it reconnects within seconds.
            log.warning("esp: not connected to the iPhone; rebooting it")
            if self.reboot():
                reply = self._cmd(line)
        return reply

    def reboot(self, wait=20):
        """Restart the ESP32 (pulse EN via RTS) and wait for the iPhone to reconnect.
        Returns whether it's connected."""
        self.last_reboot = time.time()
        if self.ser is None:
            self.open()  # opening the port resets the board too
            return self.connected
        self.ser.rts = True
        time.sleep(0.2)
        self.ser.rts = False
        deadline = time.time() + wait
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="replace").strip()
            if line.startswith("EVT"):
                log.info("esp: %s", line)
                if line.startswith("EVT connected"):
                    time.sleep(2)  # let pairing security finish before sending reports
                    break
        self.ser.reset_input_buffer()
        self.status()
        log.info("esp: after reboot connected=%s", self.connected)
        return self.connected

    def _cmd(self, line):
        if self.ser is None:
            self.open()
        try:
            self.ser.write((line + "\n").encode())
            while True:
                reply = self.ser.readline().decode(errors="replace").strip()
                if not reply:
                    raise serial.SerialException(f"no reply to {line!r}")
                if reply.startswith("EVT"):
                    log.info("esp: %s", reply)
                    if "disconnected" in reply:
                        self.connected = False
                    continue
                if reply.startswith(("OK", "ERR")):
                    return reply
        except (serial.SerialException, OSError):
            self.close()
            raise

    def status(self):
        reply = self.cmd("?")  # "OK name=ESP Mouse 1 connected=1 secured=1 ..."
        m = re.match(r"OK name=(.*?) connected=(\d)", reply)
        if m:
            self.name, self.connected = m.group(1), m.group(2) == "1"
        return reply

    def move(self, dx, dy):
        reply = self.cmd(f"M {int(dx)} {int(dy)}")
        if reply.startswith("ERR"):
            raise RuntimeError(reply)
        return time.time()

    def report(self, dx, dy):
        reply = self.cmd(f"R {int(dx)} {int(dy)}")
        if reply.startswith("ERR"):
            raise RuntimeError(reply)

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.connected = False


def find_pointer_candidates(img):
    """Centres of solid grey discs the size of the AssistiveTouch pointer."""
    b, g, r = [c.astype(np.int16) for c in cv2.split(img)]
    mask = ((np.abs(b - g) < 10) & (np.abs(g - r) < 10) & (g >= 140) & (g <= 170)).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    found = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # Text under the pointer shows through and punches holes in the disc, so the fill
        # ratio can drop well below a full circle's 0.785; the bounding box stays intact.
        if 32 <= w <= 44 and 32 <= h <= 44 and abs(w - h) <= 4 and 0.45 < area / (w * h) < 0.85:
            found.append((x + w / 2, y + h / 2))
    return found


def find_moved_discs(before, after):
    """Pointer-sized blobs that changed between two frames (works on any background)."""
    diff = cv2.absdiff(cv2.cvtColor(before, cv2.COLOR_BGR2GRAY), cv2.cvtColor(after, cv2.COLOR_BGR2GRAY))
    mask = cv2.morphologyEx((diff > 18).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    found = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if 28 <= w <= 52 and 28 <= h <= 52 and abs(w - h) <= 8 and area >= 0.4 * w * h:
            found.append((x + w / 2, y + h / 2))
    return found


def ocr(img):
    """Text on screen as [{"text", "confidence", "x", "y", "w", "h"}] in screen pixels."""
    ok, jpeg = cv2.imencode(".jpg", img)
    out = subprocess.run([str(OCR_BIN)], input=jpeg.tobytes(), capture_output=True, timeout=15, check=True)
    return json.loads(out.stdout)


def text_centres(items, wanted):
    """Centres of OCR lines equal to `wanted` (case-insensitive)."""
    return [(i["x"] + i["w"] / 2, i["y"] + i["h"] / 2) for i in items if i["text"].strip().lower() == wanted.lower()]


def label_centres(items, wanted):
    """Like text_centres, but also finds a label wrapped onto two lines (small widgets);
    returns the centre of its first line."""
    hits = text_centres(items, wanted)
    wanted = wanted.lower()
    for a in items:
        first = a["text"].strip().lower()
        if not first or not wanted.startswith(first + " "):
            continue
        for b in items:
            below = 0 < b["y"] - a["y"] < 2.5 * a["h"] and abs((b["x"] + b["w"] / 2) - (a["x"] + a["w"] / 2)) < a["w"] + b["w"]
            if below and f'{first} {b["text"].strip().lower()}' == wanted:
                hits.append((a["x"] + a["w"] / 2, a["y"] + a["h"] / 2))
    return hits


# Other entries of the AssistiveTouch top-level menu, used to tell its "Home" apart from
# e.g. the Home app's label on the home screen.
ASSISTIVE_TOUCH_MENU_LABELS = ["Device", "Siri", "Control Center", "Notification Center", "Custom"]


def _load_assistive_touch_template():
    tpl = cv2.cvtColor(cv2.imread(str(ASSISTIVE_TOUCH_TEMPLATE)), cv2.COLOR_BGR2GRAY)
    # The button is translucent; the template was cut over a Settings row whose ">" shows
    # through on the left. Ignore that part when matching.
    mask = np.full(tpl.shape, 255, np.uint8)
    mask[12:50, 4:36] = 0
    return tpl, mask


AT_TEMPLATE, AT_MASK = _load_assistive_touch_template()


def find_assistive_touch(img, near=None):
    """Centre of the AssistiveTouch button, or None.

    The idle button fades out, and then icons like TikTok's comment bubble can match better,
    so a weak match only counts close to where the button was last seen (`near`)."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    scores = cv2.matchTemplate(gray, AT_TEMPLATE, cv2.TM_CCOEFF_NORMED, mask=AT_MASK)
    scores = np.nan_to_num(scores, nan=-1, posinf=-1, neginf=-1)
    half = (AT_TEMPLATE.shape[1] / 2, AT_TEMPLATE.shape[0] / 2)
    _, best, _, loc = cv2.minMaxLoc(scores)
    if best >= 0.9:
        return (loc[0] + half[0], loc[1] + half[1])
    if near is None:
        return None
    r = 40
    x0, y0 = max(0, int(near[0] - half[0]) - r), max(0, int(near[1] - half[1]) - r)
    window = scores[y0:y0 + 2 * r + 1, x0:x0 + 2 * r + 1]
    if window.size == 0:
        return None
    _, best, _, loc = cv2.minMaxLoc(window)
    if best < 0.6:
        return None
    return (x0 + loc[0] + half[0], y0 + loc[1] + half[1])


def nearest(points, target, max_dist):
    best = min(points, key=lambda p: math.dist(p, target), default=None)
    return best if best is not None and math.dist(best, target) <= max_dist else None


class Controller:
    """Moves the pointer to screen pixels using the video feed as feedback."""

    def __init__(self, esp, capture):
        self.esp = esp
        self.capture = capture
        self.pos = None  # last known pointer position in screen pixels
        self.kx = self.ky = 1.75  # screen pixels per mouse unit
        self.human_gain = 0.8  # glides travel this fraction of a constant-speed move's distance
        self.assistive_touch = (686.0, 999.0)  # last seen AssistiveTouch button centre
        if CALIBRATION_FILE.exists():
            cal = json.loads(CALIBRATION_FILE.read_text())
            self.kx, self.ky = cal["kx"], cal["ky"]
            self.assistive_touch = tuple(cal.get("assistive_touch", self.assistive_touch))
        # move_to() keeps adjusting kx/ky from what it sees, and misreads over busy videos can
        # drag them off. Dead reckoning (tap_exact) needs the measured value, so it gets its
        # own copy that only calibrate() changes; that's also what's saved.
        self.cal_kx, self.cal_ky = self.kx, self.ky

    def save(self):
        CALIBRATION_FILE.write_text(json.dumps(
            {"kx": self.cal_kx, "ky": self.cal_ky, "assistive_touch": list(self.assistive_touch)}))

    def latest_frame(self):
        return self.capture.frame_after(0)

    def fresh_frame(self, since):
        img = self.capture.frame_after(since + FRAME_LATENCY)
        if img is None:
            raise RuntimeError("no video from the iPhone; is it unlocked with the screen on?")
        return img

    def home(self, corner=None):
        """Push the pointer into a screen corner (random unless given as (sx, sy), each -1 or 1)."""
        sx, sy = corner or (random.choice((-1, 1)), random.choice((-1, 1)))
        w, h = self.capture.size if self.capture.size != (0, 0) else (750, 1334)
        self.esp.cmd(f"STEP {HOME_STEP}")
        try:
            self.esp.move(5000 * sx, 5000 * sy)
        finally:
            self.esp.cmd(f"STEP {PRECISE_STEP}")
        self.pos = (0.0 if sx < 0 else w - 1.0, 0.0 if sy < 0 else h - 1.0)

    def locate(self):
        """Current pointer position, or None if it can't be seen."""
        if self.pos is not None:
            p = nearest(find_pointer_candidates(self.fresh_frame(time.time() - FRAME_LATENCY)), self.pos, 15)
            if p:
                return p
        # Nudge it: this also reveals an auto-hidden pointer. Whatever shifted by the nudge is the pointer.
        nudge = 30  # big enough that the two positions don't overlap in a frame diff
        frame_a = self.fresh_frame(self.esp.move(nudge, 0))
        frame_b = self.fresh_frame(self.esp.move(-nudge, 0))
        a, b = find_pointer_candidates(frame_a), find_pointer_candidates(frame_b)
        shift = -nudge * self.kx
        for pb in b:
            for pa in a:
                if abs((pb[0] - pa[0]) - shift) < 12 and abs(pb[1] - pa[1]) < 6:
                    return pb
        # Not a grey disc on this background: the diff shows the pointer at both positions.
        moved = find_moved_discs(frame_a, frame_b)
        for p in moved:
            for q in moved:
                if abs((p[0] - q[0]) - shift) < 12 and abs(p[1] - q[1]) < 6:
                    return p
        return None

    def glide(self, points, start):
        """Send the pointer along points (screen pixels) with human timing; returns finish time.

        iOS gives less gain to the slow start and end of a human-like movement than to our
        constant-speed moves, so distances are scaled by the learned human_gain.
        """
        kx, ky = self.kx * self.human_gain, self.ky * self.human_gain
        rem_x = rem_y = 0.0
        prev = start
        next_report = time.perf_counter()
        for p in points:
            rem_x += (p[0] - prev[0]) / kx
            rem_y += (p[1] - prev[1]) / ky
            prev = p
            ux, uy = round(rem_x), round(rem_y)
            rem_x -= ux
            rem_y -= uy
            while ux or uy:
                sx, sy = max(-127, min(127, ux)), max(-127, min(127, uy))
                self.esp.report(sx, sy)
                ux -= sx
                uy -= sy
            next_report += humanize.REPORT_INTERVAL
            time.sleep(max(0, next_report - time.perf_counter()))
        return time.time()

    def move_to(self, x, y, human=False):
        w, h = self.capture.size
        target = (min(max(x, 2), w - 2), min(max(y, 2), h - 2))
        pos = self.locate()
        if pos is None:
            self.home()
            pos = self.pos
        for attempt in range(5):
            ex, ey = target[0] - pos[0], target[1] - pos[1]
            ux, uy = round(ex / self.kx), round(ey / self.ky)
            if math.hypot(ex, ey) <= 3 or (ux == 0 and uy == 0):
                break
            before = self.latest_frame()
            # Human mode: one curved glide, then small straight corrective moves like a person's.
            gliding = human and attempt == 0
            if gliding:
                done = self.glide(humanize.path(pos, target), pos)
                predicted = target
            else:
                done = self.esp.move(ux, uy)
                predicted = (pos[0] + ux * self.kx, pos[1] + uy * self.ky)
            after = self.fresh_frame(done)
            dist = math.hypot(ex, ey)
            radius = max(60, 0.5 * dist)
            found = nearest(find_pointer_candidates(after), predicted, radius)
            if found is None and before is not None and dist > 45:
                # The old position also shows in the diff, but it's far from the prediction.
                found = nearest(find_moved_discs(before, after), predicted, radius)
            if found is None:
                found = self.locate()
            if found is None:
                pos = predicted  # pointer not visible (e.g. at the screen edge); trust the estimate
                break
            if gliding and dist >= 60:
                # How much of the planned glide actually happened, along the planned direction.
                ratio = ((found[0] - pos[0]) * ex + (found[1] - pos[1]) * ey) / (dist * dist)
                self.human_gain = min(1.5, max(0.4, self.human_gain * (0.7 + 0.3 * ratio)))
            # Learn the gain from long constant-speed moves; short corrections behave differently.
            if not gliding and abs(ux) >= 60:
                self.kx = min(4, max(0.5, 0.8 * self.kx + 0.2 * (found[0] - pos[0]) / ux))
            if not gliding and abs(uy) >= 60:
                self.ky = min(4, max(0.5, 0.8 * self.ky + 0.2 * (found[1] - pos[1]) / uy))
            pos = found
        self.pos = pos
        return pos

    # With human=True (meant for scripts) actions get a reaction delay, imprecise aim,
    # curved/eased/trembling motion and variable press timing; see humanize.py.

    def tap(self, x, y, human=False):
        if human:
            time.sleep(humanize.reaction_delay())
            x, y = humanize.aim(x, y)
        self.move_to(x, y, human)
        if human:
            time.sleep(humanize.settle_delay())
            self.press(humanize.click_hold())
        else:
            self.esp.cmd("C")

    def tap_exact(self, x, y, human=False):
        """Tap by dead reckoning from the nearest screen corner. Slower than tap(), but it
        doesn't depend on seeing the pointer, which move_to() can lose over a busy video."""
        w, h = self.capture.size
        if human:
            time.sleep(humanize.reaction_delay())
            x, y = humanize.aim(x, y, spread=2)
        self.home((-1 if x < w / 2 else 1, -1 if y < h / 2 else 1))
        done = self.esp.move((x - self.pos[0]) / self.cal_kx, (y - self.pos[1]) / self.cal_ky)
        self.pos = (x, y)
        # Fix a small drift if the pointer is visible near where it should be.
        seen = nearest(find_pointer_candidates(self.fresh_frame(done)), (x, y), 40)
        if seen and math.dist(seen, (x, y)) > 4:
            self.esp.move((x - seen[0]) / self.cal_kx, (y - seen[1]) / self.cal_ky)
        if human:
            time.sleep(humanize.settle_delay())
            self.press(humanize.click_hold())
        else:
            self.esp.cmd("C")

    def long_press(self, x, y, seconds, human=False):
        if human:
            time.sleep(humanize.reaction_delay())
            x, y = humanize.aim(x, y)
            seconds *= random.uniform(0.9, 1.15)
        self.move_to(x, y, human)
        if human:
            time.sleep(humanize.settle_delay())
        self.press(seconds)

    def press(self, seconds):
        self.esp.cmd("D")
        try:
            time.sleep(seconds)
        finally:
            self.esp.cmd("U")

    def swipe(self, x1, y1, x2, y2, human=False):
        if human:
            time.sleep(humanize.reaction_delay())
            x1, y1 = humanize.aim(x1, y1)
            x2, y2 = humanize.aim(x2, y2, spread=8)
        start = self.move_to(x1, y1, human)
        self.esp.cmd("D")
        try:
            if human:
                time.sleep(humanize.settle_delay())
                self.glide(humanize.path(start, (x2, y2), target_width=120), start)
                time.sleep(humanize.settle_delay())
            else:
                self.esp.move((x2 - x1) / self.kx, (y2 - y1) / self.ky)
        finally:
            self.esp.cmd("U")
        self.pos = None

    def scroll(self, amount):
        self.esp.cmd(f"W {int(max(-127, min(127, amount)))}")

    def calibrate(self):
        self.home(corner=(-1, -1))  # the measuring moves below go right and down
        p1 = nearest(find_pointer_candidates(self.fresh_frame(self.esp.move(150, 250))), (150 * self.kx, 250 * self.ky), 250)
        if p1 is None:
            raise RuntimeError("pointer not found; open a light screen (e.g. Settings) and retry")
        p2 = nearest(find_pointer_candidates(self.fresh_frame(self.esp.move(200, 0))), (p1[0] + 200 * self.kx, p1[1]), 150)
        p3 = nearest(find_pointer_candidates(self.fresh_frame(self.esp.move(0, 300))), ((p2 or p1)[0], p1[1] + 300 * self.ky), 150)
        if p2 is None or p3 is None:
            raise RuntimeError("lost the pointer during calibration")
        self.kx, self.ky = (p2[0] - p1[0]) / 200, (p3[1] - p2[1]) / 300
        self.cal_kx, self.cal_ky = self.kx, self.ky
        self.pos = p3
        self.save()
        return self.kx, self.ky

    def wake(self):
        """Turn the screen on and go Home, which also unlocks a phone without a passcode."""
        started = time.time()
        self.esp.move(20, 0)
        self.esp.move(-20, 0)
        last = self.latest_frame()
        # No new frame can also mean the screen is on but still (e.g. a paused video); a click
        # would then hit whatever is under the pointer, so only click if it looks off.
        if not self.capture.wait_for_frame(started, 2.0) and (last is None or last.mean() < 8):
            # Movement alone didn't light the screen; a click does.
            started = time.time()
            self.esp.cmd("C")
            if not self.capture.wait_for_frame(started, 3.0):
                raise RuntimeError("iPhone screen didn't wake up")
        time.sleep(0.5)
        self.press_home()

    def press_home(self):
        """Home via AssistiveTouch: tap the button, then "Home" in its menu."""
        button = find_assistive_touch(self.fresh_frame(time.time()), near=self.assistive_touch)
        if button:
            self.assistive_touch = button
            self.save()
        self.tap_exact(*self.assistive_touch)  # tap() can lose the pointer over a playing video
        for _ in range(4):  # the menu animation sometimes takes longer, e.g. over a playing video
            time.sleep(0.5)
            items = ocr(self.fresh_frame(time.time()))
            anchors = [c for label in ASSISTIVE_TOUCH_MENU_LABELS for c in text_centres(items, label)]
            homes = text_centres(items, "Home")
            if len(anchors) >= 2 and homes:
                break
        else:
            raise RuntimeError("AssistiveTouch menu didn't open (is the button where it used to be?)")
        menu_centre = (sum(a[0] for a in anchors) / len(anchors), sum(a[1] for a in anchors) / len(anchors))
        home = min(homes, key=lambda h: math.dist(h, menu_centre))
        # The label sits under its icon; aim a little above the text.
        self.tap_exact(home[0], home[1] - 25)

    def open_home_screen_icon(self, label, pages=4):
        """Wake, go Home, and tap the app/Shortcut icon with this label, paging right to find it."""
        self.wake()
        time.sleep(0.8)
        w, h = self.capture.size
        for page in range(pages):
            # On the first miss press Home again: from the lock screen, Home unlocks into the
            # last app used rather than the home screen.
            for retry in range(2 if page == 0 else 1):
                if retry:
                    self.press_home()
                    time.sleep(0.8)
                hits = label_centres(ocr(self.fresh_frame(time.time())), label)
                if hits:
                    x, y = hits[0]
                    self.tap_exact(x, y - 60)  # icons sit above their labels
                    return
            self.swipe(w * 0.75, h * 0.55, w * 0.2, h * 0.55)
            time.sleep(0.8)
        raise RuntimeError(f'no "{label}" icon on the home screen')

    def import_media(self, store):
        """Get every queued file into Photos over the USB cable, in one Shortcut run."""
        items = store.take_queued()
        if not items:
            return
        try:
            if not usb_media.phone_connected():
                raise RuntimeError("the iPhone isn't connected over USB; plug it in and press Retry")
            self._import_over_usb(store, items)
        except Exception as e:
            for item in items:
                if store.get(item["id"]) and item["status"] != "downloaded":
                    store.set_status(item["id"], "failed", str(e))
            raise

    def _import_over_usb(self, store, items):
        for item in items:
            store.set_status(item["id"], "copying")
        before = usb_media.camera_roll()
        usb_media.push([(item["path"], item["path"].name) for item in items])
        for item in items:
            store.set_status(item["id"], "importing")
        self.open_home_screen_icon(USB_IMPORT_SHORTCUT)
        # Done once every save shows up as a new file in the camera roll.
        deadline = time.time() + 30 + 5 * len(items) + sum(i["size"] for i in items) / 20e6
        while (saved := len([p for p in usb_media.camera_roll() - before if not p.upper().endswith(".AAE")])) < len(items):
            if time.time() > deadline:
                raise RuntimeError(f'the "{USB_IMPORT_SHORTCUT}" Shortcut saved {saved} of {len(items)} files to Photos; '
                                   "is there a prompt on the phone's screen?")
            self.confirm_delete_prompt()
            time.sleep(1)
        # A Delete Files step in the Shortcut asks for confirmation right after the save.
        grace = time.time() + 6
        while time.time() < grace and not self.confirm_delete_prompt():
            time.sleep(0.5)
        usb_media.clear()
        for item in items:
            store.set_status(item["id"], "downloaded")

    def delete_videos(self):
        """Run the Delete Videos Shortcut and answer iOS's delete confirmation."""
        self.open_home_screen_icon(DELETE_VIDEOS_SHORTCUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.confirm_delete_prompt():
                return
            time.sleep(0.5)
        raise RuntimeError("no delete prompt appeared; maybe there are no videos to delete")

    def confirm_delete_prompt(self):
        """Older setups have a Delete Files step, which iOS confirms every run; answer Delete."""
        items = ocr(self.fresh_frame(time.time()))
        if any("to delete" in i["text"].lower() for i in items):
            hits = text_centres(items, "Delete")
            if hits:
                self.tap(*hits[0])
                return True
        return False

    def autoscroll(self, app, minutes, stop, progress):
        """Open TikTok or Instagram Reels and swipe to the next video every few seconds
        until `minutes` are up or `stop` is set. `progress` gets the live session info."""
        label = AUTOSCROLL_APPS[app]
        ends_at = time.time() + minutes * 60
        progress.update(app=app, ends_at=ends_at, videos=0, phase="opening")
        self.open_home_screen_icon(label)
        if stop.wait(4):  # app launch / feed load
            return
        if app == "instagram":
            self.leave_instagram_composer()
            self.open_instagram_reels()
        else:
            self.open_tiktok_feed()
        w, h = self.capture.size
        progress["phase"] = "watching"
        dismissed = 0
        while not stop.wait(min(humanize.watch_time(), max(0, ends_at - time.time()))) and time.time() < ends_at:
            # Swipe anyway if a "popup" won't go away; it may just be a video that looks like one.
            if dismissed < 2 and self.clear_popup():
                dismissed += 1
                continue
            dismissed = 0
            # Middle of the video, left of the like/comment column, and clear of the caption.
            x = w * random.uniform(0.35, 0.55)
            self.swipe(x, h * random.uniform(0.68, 0.76), x + random.uniform(-20, 20), h * random.uniform(0.22, 0.3), human=True)
            progress["videos"] += 1

    def clear_popup(self):
        """Dismiss a prompt covering the feed (e.g. TikTok's "Add phone" sheet); True if one was found."""
        img = self.fresh_frame(time.time())
        items = ocr(img)
        for label in POPUP_DISMISS_LABELS:
            hits = text_centres(items, label)
            if hits:
                log.info('autoscroll: dismissing popup via "%s"', label)
                self.tap(*hits[0], human=True)
                return True
        top = sheet_top(img)
        if top is not None:
            # Tapping the dimmed feed above a bottom sheet closes it (its ✕ has no text to find).
            log.info("autoscroll: dismissing sheet at y=%d", top)
            h = img.shape[0]
            self.tap(img.shape[1] / 2, (top + 0.1 * h) / 2, human=True)
            return True
        return False

    def open_tiktok_feed(self):
        """TikTok reopens where it was left (e.g. the Profile tab or a just-posted video after
        a post); get to Home -> For You before swiping."""
        w, h = self.capture.size
        for _ in range(4):
            items = ocr(self.fresh_frame(time.time()))
            if any("for you" in i["text"].lower() and i["y"] < 0.12 * h for i in items):
                return
            homes = [c for c in text_centres(items, "Home") if c[1] > 0.9 * h]
            if homes:
                self.tap_exact(homes[0][0], homes[0][1] - 25, human=True)  # icon above its label
            else:
                self.tap_exact(50 * w / 750, 80 * w / 750, human=True)  # back arrow at the top left
            time.sleep(2)
        log.warning("autoscroll: couldn't confirm the TikTok For You feed; scrolling anyway")

    def leave_instagram_composer(self):
        """Instagram can reopen in the New reel / drafts screens (after a post flow); close
        them with the ✕ at the top left, whose mode strip would otherwise take the Reels tab taps."""
        w, h = self.capture.size
        for _ in range(3):
            items = ocr(self.fresh_frame(time.time()))
            if not any(i["text"].strip() in ("New reel", "New post", "Reels drafts") for i in items):
                return
            back = next(((i["x"] + i["w"] / 2, i["y"] + i["h"] / 2) for i in items
                         if i["text"].strip() in ("X", "x", "<") and i["x"] < 0.16 * w and i["y"] < 0.12 * h),
                        (66 * w / 750, 110 * w / 750))
            self.tap_exact(*back, human=True)
            time.sleep(2)

    def open_instagram_reels(self):
        """Tap the Reels tab in Instagram's bottom bar. It moved from 4th to 2nd place in
        2025, so try both and check for the "Reels" title at the top."""
        w, h = self.capture.size
        scale = w / 375  # screen pixels per point
        bottom_inset = 34 if h / w > 2 else 0  # home-indicator iPhones
        y = h - (bottom_inset + 24) * scale
        for slot in INSTAGRAM_REELS_SLOTS:
            self.tap(w * (slot - 0.5) / 5, y, human=True)
            time.sleep(2)
            items = ocr(self.fresh_frame(time.time()))
            if any(i["text"].strip().lower().startswith("reels") and i["y"] < 0.15 * h for i in items):
                return
        log.warning("couldn't confirm Instagram Reels is open; scrolling anyway")


def sheet_top(img):
    """Top edge of a white bottom sheet over at least the lower 30% of the screen, or None."""
    h = img.shape[0]
    white_rows = (img.min(axis=2) > 225).mean(axis=1) > 0.7
    window = int(0.3 * h)
    for y in range(int(0.15 * h), int(0.7 * h), 4):
        # A sheet has an edge: the dimmed feed shows just above it, unlike a white video.
        if white_rows[y] and white_rows[y:y + window].mean() > 0.6 and white_rows[y - 16:y].mean() < 0.2:
            return y
    return None


AUTOSCROLL_APPS = {"tiktok": "TikTok", "instagram": "Instagram"}  # home-screen labels
POPUP_DISMISS_LABELS = ["Not now", "Skip", "Maybe later", "No thanks", "Dismiss", "Cancel", "Close"]
INSTAGRAM_REELS_SLOTS = [int(s) for s in os.environ.get("INSTAGRAM_REELS_SLOTS", "2,4").split(",")]


capture = Capture()
esp = ESP(SERIAL_PORT)
controller = Controller(esp, capture)
media = MediaStore()
commands: queue.Queue = queue.Queue()
state = {"busy": False, "last_error": None, "last_action": None, "autoscroll": None, "tiktok": None, "routine": None}
# Set from the websocket (not the queue, which is blocked while a session runs).
stop_autoscroll = threading.Event()
stop_routine = threading.Event()


def worker():
    """Executes commands one at a time; polls ESP status while idle."""
    while True:
        try:
            msg = commands.get(timeout=3)
        except queue.Empty:
            try:
                esp.status()
                # Reconnect while idle too, so a scheduled run finds the mouse ready.
                if esp.ser is not None and not esp.connected and time.time() - esp.last_reboot > 120:
                    log.warning("esp: iPhone not connected; rebooting the ESP to reconnect")
                    esp.reboot()
            except Exception as e:
                state["last_error"] = f"ESP: {e}"
                time.sleep(2)
            continue
        state["busy"] = True
        started = time.time()
        try:
            kind = msg["type"]
            execute(msg)
            state["last_action"] = f"{kind} in {time.time() - started:.2f}s"
            state["last_error"] = None
        except Exception as e:
            log.exception("command %s failed", msg)
            state["last_error"] = str(e)
        finally:
            state["busy"] = False


def execute(msg):
    """Run one command (from the websocket, the API or a routine step)."""
    kind = msg["type"]
    human = bool(msg.get("human"))
    if kind == "move":
        controller.move_to(msg["x"], msg["y"], human)
    elif kind == "tap":
        controller.tap(msg["x"], msg["y"], human)
    elif kind == "tap_exact":
        controller.tap_exact(msg["x"], msg["y"], human)
    elif kind == "long_press":
        controller.long_press(msg["x"], msg["y"], min(float(msg.get("seconds", 1)), 5), human)
    elif kind == "swipe":
        controller.swipe(msg["x1"], msg["y1"], msg["x2"], msg["y2"], human)
    elif kind == "scroll":
        controller.scroll(msg["amount"])
    elif kind == "calibrate":
        controller.calibrate()
    elif kind == "home_pointer":
        controller.home()
    elif kind == "wake":
        controller.wake()
    elif kind == "home_button":
        controller.press_home()
    elif kind == "import_media":
        controller.import_media(media)
    elif kind == "delete_videos":
        controller.delete_videos()
    elif kind == "autoscroll":
        stop_autoscroll.clear()
        state["autoscroll"] = progress = {}
        try:
            controller.autoscroll(msg["app"], min(max(float(msg["minutes"]), 1), 240), stop_autoscroll, progress)
        finally:
            state["autoscroll"] = None
    elif kind == "tiktok_draft":
        state["tiktok"] = progress = {}
        try:
            run_tiktok_flow(progress, msg)
            tiktok.save_state(last_result={"ok": True, "at": time.time(), "sound": progress.get("sound"),
                                           "finish": progress["finish"], "instagram": progress["instagram"],
                                           "skipped_chars": progress.get("skipped_chars")})
        except Exception as e:
            tiktok.save_state(last_result={"ok": False, "at": time.time(), "error": str(e),
                                           "phase": progress.get("phase")})
            raise
        finally:
            state["tiktok"] = None
    elif kind == "routine":
        run_routine(msg["id"])


def run_routine(routine_id):
    """Run a routine's steps in order; stops at the first failure or on Stop."""
    routine = schedule.get(routine_id)
    if routine is None:
        raise RuntimeError("that routine no longer exists")
    stop_routine.clear()
    steps = routine["steps"]
    state["routine"] = progress = {"id": routine_id, "name": routine["name"], "step": 0, "steps": len(steps)}
    result = {"at": time.time(), "ok": True}
    try:
        for i, step in enumerate(steps):
            if stop_routine.is_set():
                raise RuntimeError("stopped")
            progress.update(step=i + 1, label=schedule.describe_step(step))
            log.info("routine %s: step %d/%d %s", routine["name"], i + 1, len(steps), progress["label"])
            if step["type"] == "send_video":
                send_routine_video(routine_id, step, progress)
            elif step["type"] == "autoscroll":
                execute({"type": "autoscroll", "app": step["app"], "minutes": step["minutes"]})
            elif step["type"] == "upload":
                execute({"type": "tiktok_draft", "finish": step["finish"], "instagram": step["instagram"],
                         "description": step["description"]})
            elif step["type"] == "delete_videos":
                execute({"type": "delete_videos"})
            elif step["type"] == "wait":
                stop_routine.wait(step["minutes"] * 60)
        if stop_routine.is_set():
            raise RuntimeError("stopped")
    except Exception as e:
        result.update(ok=False, error=f'step {progress["step"]} ({progress.get("label", "")}): {e}')
        raise
    finally:
        result["finished"] = time.time()
        schedule.update(routine_id, last_result=result)
        state["routine"] = None


def send_routine_video(routine_id, step, progress):
    """Put the routine's next queued video into Photos (over USB), optionally clearing the
    old videos out first so it's the only one there."""
    nxt = schedule.next_video(routine_id)
    if nxt is None:
        raise RuntimeError("no videos left in this routine's queue; add more in the Schedule card")
    entry, path = nxt
    progress["video"] = entry["name"]
    if step.get("clear_first"):
        clear_photos_videos()
    with open(path, "rb") as f:
        media.add(entry["name"], f)
    controller.import_media(media)
    schedule.used_video(routine_id, entry)


def run_tiktok_flow(progress, options=None):
    """TikTok draft/post, then optionally the same on Instagram. Settings come from the
    TikTok / Instagram card, overridden by `options` (a routine's upload step)."""
    saved = tiktok.load_state()
    options = options or {}
    post = options.get("finish", saved.get("finish")) == "post"
    to_instagram = bool(options.get("instagram", saved.get("instagram")))
    description = options.get("description") or saved.get("description", "")
    if options.get("instagram_only"):
        # E.g. to finish a run whose TikTok part worked: the newest video in Photos goes to Instagram.
        # Used after a TikTok post, so TikTok's copy is there as the second video.
        progress.update(finish="post" if post else "draft", instagram=True)
        instagram.InstagramPoster(controller, ocr, find_pointer_candidates, progress).run(description, post=post, video=2)
        return
    progress.update(finish="post" if post else "draft", instagram=to_instagram)
    # A posted TikTok is saved to Photos with its sound; that copy is what goes to Instagram.
    # Drafts aren't saved, so then Instagram just gets the newest video there is.
    wait_for_saved = to_instagram and post and usb_media.phone_connected()
    before = usb_media.camera_roll() if wait_for_saved else None
    tiktok.TikTokDrafter(controller, ocr, find_pointer_candidates, progress).run(description, post=post)
    if not to_instagram:
        return
    if post:
        progress["phase"] = "TikTok: waiting for the posted video in Photos"
        wait_for_new_video(before)
    # After a post Photos holds the original and TikTok's copy (with the sound); in
    # Instagram's grid the copy is the second video.
    instagram.InstagramPoster(controller, ocr, find_pointer_candidates, progress).run(
        description, post=post, video=2 if post else 1)


def clear_photos_videos():
    """Delete every video in Photos; fine if there are none."""
    try:
        controller.delete_videos()
    except RuntimeError as e:
        if "no delete prompt" not in str(e):
            raise
        log.info("no videos in Photos to clear")


def wait_for_new_video(before, timeout=300):
    """Wait until TikTok's copy of the posted video lands in the camera roll (it saves it once
    the upload finishes) and return its camera-roll path. Without USB there's nothing to
    watch, so just give it time and return None."""
    if before is None:
        time.sleep(90)
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        new = sorted(p for p in usb_media.camera_roll() - before if p.upper().endswith((".MP4", ".MOV")))
        if new:
            log.info("tiktok: saved video appeared: %s", new)
            time.sleep(3)  # let Photos finish indexing it
            return new[-1]
        time.sleep(5)
    raise RuntimeError("TikTok's posted video didn't appear in Photos; is \"Save to device\" on in TikTok?")


def status_payload():
    return {
        "type": "status",
        "esp": {"name": esp.name, "connected": esp.connected, "serial": esp.ser is not None},
        "capture": {"fps": round(capture.fps, 1), "width": capture.size[0], "height": capture.size[1],
                    "frame_age": round(time.time() - capture.timestamp, 1) if capture.timestamp else None},
        "busy": state["busy"],
        "queued": commands.qsize(),
        "pointer": controller.pos,
        "gain": [round(controller.kx, 3), round(controller.ky, 3), round(controller.human_gain, 3)],
        "last_action": state["last_action"],
        "last_error": state["last_error"],
        "media": media.list()[:10],
        "usb_import_shortcut": USB_IMPORT_SHORTCUT,
        "delete_videos_shortcut": DELETE_VIDEOS_SHORTCUT,
        "tiktok": state["tiktok"],
        "routine": state["routine"],
        "autoscroll": state["autoscroll"] and {**state["autoscroll"],
                                               "remaining": max(0, round(state["autoscroll"].get("ends_at", 0) - time.time()))},
    }


@asynccontextmanager
async def lifespan(app):
    capture.start()
    threading.Thread(target=worker, daemon=True).start()
    schedule.start(lambda r: commands.put({"type": "routine", "id": r["id"]}))
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/media")
async def upload_media(file: UploadFile = File(...), send: bool = Form(True)):
    """Queue a photo/video for the phone; with send=true (default) start the import right away."""
    item = await asyncio.to_thread(media.add, file.filename, file.file)
    if send:
        commands.put({"type": "import_media"})
    return item


@app.post("/api/media/send")
async def send_queued_media():
    """Import everything queued, in one Shortcut run when the phone is on USB."""
    commands.put({"type": "import_media"})
    return {"ok": True}


@app.post("/api/usb/folder")
async def create_usb_folder():
    """Create the USB import folder on the phone so the Shortcut can be pointed at it."""
    try:
        await asyncio.to_thread(usb_media.ensure_folder)
    except Exception as e:
        raise HTTPException(409, f"{type(e).__name__}: {e} (is the phone plugged in, trusted, with VLC installed?)")
    return {"folder": usb_media.FOLDER}


@app.get("/api/media")
async def list_media():
    return media.list()


@app.get("/api/media/{item_id}")
async def get_media(item_id: str):
    item = media.get(item_id)
    if item is None:
        raise HTTPException(404)
    return media.public(item)


@app.post("/api/media/{item_id}/send")
async def send_media(item_id: str):
    if media.get(item_id) is None:
        raise HTTPException(404)
    media.set_status(item_id, "queued")
    commands.put({"type": "import_media"})
    return {"ok": True}


@app.delete("/api/media/{item_id}")
async def delete_media(item_id: str):
    if not media.remove(item_id):
        raise HTTPException(404)
    return {"ok": True}


@app.get("/api/tiktok")
async def get_tiktok():
    """Saved description, the last favorite sound used and the last run's result."""
    return tiktok.load_state()


@app.post("/api/tiktok")
async def set_tiktok(description: str = Form(""), finish: str = Form("draft"), instagram: bool = Form(False),
                     run: bool = Form(False), instagram_only: bool = Form(False)):
    """Save the description, whether to end with Drafts or Post (finish="draft"/"post") and
    whether to do the same on Instagram afterwards; with run=true also run it."""
    if finish not in ("draft", "post"):
        raise HTTPException(400, "finish must be draft or post")
    tiktok.save_state(description=description, finish=finish, instagram=instagram)
    if run:
        commands.put({"type": "tiktok_draft", "instagram_only": instagram_only})
    return tiktok.load_state()


def routine_payload(r):
    nxt = schedule.next_run(r)
    return {**r, "next_run": nxt.timestamp() if nxt else None, "summary": [schedule.describe_step(s) for s in r["steps"]]}


@app.get("/api/routines")
async def list_routines():
    return [routine_payload(r) for r in schedule.load()]


@app.post("/api/routines")
async def save_routine(request: Request):
    """Create (no id) or update a routine: {id?, name, enabled, time "HH:MM", days [0=Mon..6],
    jitter (minutes of random delay), steps [...]} (see schedule.py for the step types)."""
    try:
        return routine_payload(schedule.save_routine(await request.json()))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/routines/{routine_id}")
async def delete_routine(routine_id: str):
    if not schedule.delete(routine_id):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/routines/{routine_id}/videos")
async def add_routine_videos(routine_id: str, files: list[UploadFile] = File(...)):
    """Add videos to the end of a routine's queue; each run's send step uses the next one."""
    if schedule.get(routine_id) is None:
        raise HTTPException(404)
    for f in files:
        await asyncio.to_thread(schedule.add_video, routine_id, f.filename, f.file)
    return routine_payload(schedule.get(routine_id))


@app.delete("/api/routines/{routine_id}/videos/{video_id}")
async def remove_routine_video(routine_id: str, video_id: str):
    try:
        if not schedule.remove_video(routine_id, video_id):
            raise HTTPException(404)
    except KeyError:
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/routines/{routine_id}/videos/{video_id}/move")
async def move_routine_video(routine_id: str, video_id: str, delta: int = Form(...)):
    """Move a video earlier (delta=-1) or later (+1) in the queue."""
    try:
        schedule.move_video(routine_id, video_id, delta)
    except KeyError:
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/routines/{routine_id}/run")
async def run_routine_now(routine_id: str):
    if schedule.get(routine_id) is None:
        raise HTTPException(404)
    commands.put({"type": "routine", "id": routine_id})
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()

    async def send_frames():
        last_seq, last_status = 0, 0.0
        while True:
            if capture.seq != last_seq and capture.jpeg:
                last_seq = capture.seq
                await websocket.send_bytes(capture.jpeg)
            if time.time() - last_status > 0.5:
                last_status = time.time()
                await websocket.send_text(json.dumps(status_payload()))
            await asyncio.sleep(0.01)

    sender = asyncio.create_task(send_frames())
    try:
        while True:
            msg = json.loads(await websocket.receive_text())
            if msg.get("type") in {"move", "tap", "tap_exact", "long_press", "swipe", "scroll", "calibrate", "home_pointer", "wake", "home_button",
                                   "delete_videos", "tiktok_draft"}:
                commands.put(msg)
            elif msg.get("type") == "autoscroll" and msg.get("app") in AUTOSCROLL_APPS:
                commands.put(msg)
            elif msg.get("type") == "stop_autoscroll":
                stop_autoscroll.set()
            elif msg.get("type") == "stop_routine":
                # Ends the routine after the current step; a scroll step ends right away.
                stop_routine.set()
                stop_autoscroll.set()
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
