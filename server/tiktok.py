"""Save the video in Photos as a TikTok draft: + -> pick the video -> swap the sound for the
next one from the Favorites tab (cycling) -> type the description -> Drafts (or Post).

Everything is found on screen with OCR, and taps use Controller.tap_exact (dead reckoning
from a corner) because the pointer tracker can lose the pointer over a playing video.
Coordinates below were measured on a 750x1334 screen (iPhone SE/8) and are scaled for others.
"""

import json
import logging
import math
import random
import re
import time
from pathlib import Path

import humanize

STATE_FILE = Path(__file__).resolve().parent / "tiktok_state.json"
log = logging.getLogger("iphone-control")


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**changes):
    state = {**load_state(), **changes}
    STATE_FILE.write_text(json.dumps(state, indent=2))
    return state


def norm(title):
    """Comparable form of a sound title: OCR adds a "playing" icon in front of the selected
    one and TikTok truncates long titles with "..."."""
    t = title.lower().replace("…", "...").split("...")[0]
    t = re.sub(r"^[^a-z0-9.]{1,3}\s+", "", t.strip())  # "ıl Next Please" -> "next please"
    return re.sub(r"\s+", " ", t).strip()


def same_title(a, b):
    a, b = norm(a), norm(b)
    n = min(len(a), len(b), 18)
    return n >= 3 and a[:n] == b[:n]


# On-screen keyboard (TikTok's and Instagram's caption fields use the Twitter-style layout: @ and # instead
# of return on the letters page). Key centres in 750-wide pixels; rows are found from the
# bottom-left "123"/"ABC" key.
KEY_PITCH = 73.4
ROW_PITCH = 108.5
LETTER_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
LETTER_ROW_X0 = [45, 81, 155]
NUM_ROWS = ["1234567890", "-/:;()$&@\""]
SYM_ROWS = ["[]{}#%^*+=", "_\\|~<>€£¥•"]
PUNCT_ROW = ".,?!'"  # third row of both the 123 and #+= pages
PUNCT_X = [169, 272, 376, 479, 582]
SHIFT_X, MODE_X, SPACE_X, BACKSPACE_X = 55, 54, 411, 695
LETTERS_AT_X, LETTERS_HASH_X, RETURN_X = 606, 698, 650
# Buttons that confirm leaving a half-made video (when backing out to the feed).
DISCARD_LABELS = ["Discard", "Discard edits", "Discard video", "Start over", "Exit"]
# Smart punctuation turns ' and " into curly quotes; accept those too.
CHAR_ALIASES = {"’": "'", "‘": "'", "“": '"', "”": '"'}


class ScreenFlow:
    """OCR-driven steps through an app's screens (shared by the TikTok and Instagram flows)."""

    APP = "app"

    def __init__(self, ctl, ocr, pointer_candidates, progress):
        self.ctl = ctl
        self.ocr_fn = ocr
        self.pointer_candidates = pointer_candidates
        self.progress = progress
        w, h = ctl.capture.size
        self.w, self.h = w, h
        self.s = w / 750  # scale from the measured layout

    # ---- screen helpers -------------------------------------------------------------

    def frame(self):
        return self.ctl.fresh_frame(time.time())

    def read(self):
        return self.ocr_fn(self.frame())

    @staticmethod
    def find(items, text, exact=False, region=None):
        """Centre of the first OCR line matching text (substring unless exact), or None."""
        text = text.lower()
        for i in items:
            t = i["text"].strip().lower()
            if (t == text) if exact else (text in t):
                c = (i["x"] + i["w"] / 2, i["y"] + i["h"] / 2)
                if region is None or (region[0] <= c[0] <= region[2] and region[1] <= c[1] <= region[3]):
                    return c
        return None

    def wait_for(self, check, timeout, what):
        """Poll OCR until check(items) is truthy; returns its value."""
        deadline = time.time() + timeout
        while True:
            items = self.read()
            found = check(items)
            if found:
                return found
            if time.time() > deadline:
                raise RuntimeError(f"{self.APP}: {what} didn't show up")
            time.sleep(0.6)

    def tap(self, x, y):
        self.ctl.tap_exact(x, y, human=True)

    def step(self, phase):
        log.info("%s: %s", self.APP.lower(), phase)
        self.progress["phase"] = f"{self.APP}: {phase}"

    def type_text(self, text):
        """Type into the focused field with the on-screen keyboard; records skipped characters."""
        skipped = Keyboard(self).type(text)
        if skipped:
            chars = set(self.progress.get("skipped_chars", "")) | set(skipped)
            self.progress["skipped_chars"] = "".join(sorted(chars))
            log.warning("%s: couldn't type %r", self.APP.lower(), skipped)

    def keyboard_open(self, items):
        return any(i["text"].strip() in ("123", "12", "ABC") and i["x"] + i["w"] / 2 < 110 * self.s
                   and i["y"] > 0.85 * self.h for i in items)

    def focus(self, where, ready, what="the keyboard"):
        """Tap where(items) until ready(items) (e.g. the keyboard is up); taps sometimes
        don't register while a screen is still settling."""
        for attempt in range(3):
            self.tap(*where(self.read()))
            deadline = time.time() + 4
            while time.time() < deadline:
                if ready(self.read()):
                    time.sleep(0.5)
                    return
                time.sleep(0.5)
            log.info("%s: %s didn't open, tapping again", self.APP.lower(), what)
        raise RuntimeError(f"{self.APP}: {what} didn't open")

    def back_out(self, is_start, what):
        """Apps reopen where they were left, e.g. in an editor after a failed run: tap the
        back arrow / ✕ at the top left (throwing the unfinished video away) until
        is_start(items) is true."""
        for attempt in range(8):
            items = self.read()
            if is_start(items):
                time.sleep(1)
                return
            if self.reopen_from_home_screen(items):
                continue
            button = next((self.find(items, label, exact=True) for label in DISCARD_LABELS
                           if self.find(items, label, exact=True)), None)
            self.tap(*(button or self.back_button(items, attempt)))
            time.sleep(2)
        raise RuntimeError(f"{self.APP}: couldn't get to {what}")

    def on_home_screen(self, items):
        return sum(bool(self.find(items, label, exact=True)) for label in ("TikTok", "Instagram", "Settings", "Photos")) >= 3

    def reopen_from_home_screen(self, items):
        """If the app didn't open (still on the iPhone home screen), tap its icon again."""
        if not self.on_home_screen(items):
            return False
        label = self.find(items, self.APP, exact=True)
        if not label:
            return False
        log.info("%s: still on the home screen; tapping the icon again", self.APP.lower())
        self.ctl.tap_exact(label[0], label[1] - 60, human=True)  # icons sit above their labels
        time.sleep(4)
        return True

    def back_button(self, items, attempt=0, guess=True):
        """The back arrow / ✕ at the top left: where OCR sees one, else (with guess) one of
        the usual spots, else None."""
        for i in items:
            cx, cy = i["x"] + i["w"] / 2, i["y"] + i["h"] / 2
            if i["text"].strip() in ("X", "x", "<", "×", "‹") and cx < 120 * self.s and 50 * self.s < cy < 160 * self.s:
                return cx, cy
        if not guess:
            return None
        return [(50 * self.s, 80 * self.s), (66 * self.s, 110 * self.s)][attempt % 2]


def clean_text(text):
    """Description as typed: curly quotes straightened, and a trailing space after a final
    hashtag/mention so its suggestion list (which covers the page) closes."""
    text = "".join(CHAR_ALIASES.get(c, c) for c in text.strip())
    if re.search(r"[#@]\w+$", text):
        text += " "
    return text


class TikTokDrafter(ScreenFlow):
    APP = "TikTok"

    # ---- the flow ---------------------------------------------------------------------

    def run(self, description, post=False):
        self.progress.update(started=time.time(), sound=None)
        self.open_app()
        self.open_create()
        self.pick_video()
        self.choose_sound()
        self.open_post_page()
        self.type_description(description)
        self.finish(post)
        self.step("done")

    def open_app(self):
        self.step("opening TikTok")
        self.ctl.open_home_screen_icon("TikTok")
        time.sleep(4)
        self.back_out(lambda it: self.find(it, "Profile") and self.find(it, "Inbox"), "the feed")

    def open_create(self):
        self.step("opening the + screen")
        camera = lambda it: self.find(it, "POST", exact=True) or self.find(it, "Recents")
        for attempt in range(3):
            items = self.read()
            if camera(items):
                return
            plus = self.find(items, "+", exact=True, region=(0.3 * self.w, 0.9 * self.h, 0.7 * self.w, self.h))
            self.tap(*(plus or (self.w / 2, self.h - 51 * self.s)))
            try:
                self.wait_for(camera, 8, "the camera screen")
                return
            except RuntimeError:
                log.info("tiktok: + didn't open the camera, retrying")
        raise RuntimeError("TikTok: the camera screen didn't show up")

    def pick_video(self):
        self.step("picking the video")
        items = self.read()
        if not self.find(items, "Recents"):
            # Gallery thumbnail, bottom-left of the camera screen.
            self.tap(62 * self.s, self.h - 64 * self.s)
            items = self.wait_for(lambda it: it if self.find(it, "Recents") else None, 10, "the gallery")
        time.sleep(1)
        # First cell of the grid, top-left under the All/Videos/Photos tabs. With "Select
        # multiple" on it has a selection circle in its top-right corner; tap that.
        self.tap(212 * self.s, 262 * self.s)
        time.sleep(1.5)
        items = self.read()
        if self.find(items, "Recents"):
            # Still in the gallery: selection mode, press Next.
            nxt = self.find(items, "Next", exact=True, region=(0.5 * self.w, 0.85 * self.h, self.w, self.h))
            self.tap(*(nxt or (556 * self.s, self.h - 76 * self.s)))
        self.wait_for(self.editor_sound_pill, 25, "the video editor")
        time.sleep(1)

    def editor_sound_pill(self, items):
        """Where to tap the sound pill at the top of the editor ("Add sound" or the name of
        the sound TikTok picked); None if the editor isn't showing."""
        if not self.find(items, "Next", exact=True, region=(0.5 * self.w, 0.85 * self.h, self.w, self.h)):
            return None
        top = [i for i in items if i["y"] + i["h"] / 2 < 130 * self.s and 0.2 * self.w < i["x"] + i["w"] / 2 < 0.8 * self.w]
        if not top:
            return None
        pill = min(top, key=lambda i: abs(i["x"] + i["w"] / 2 - self.w / 2))
        # Its left part: the right end of a picked sound's pill is an ✕ that removes it.
        return (pill["x"] + min(60 * self.s, pill["w"] / 2), pill["y"] + pill["h"] / 2)

    # ---- sound -------------------------------------------------------------------------

    def choose_sound(self):
        self.step("choosing a sound")
        self.open_favorites()
        last = load_state().get("last_sound")
        title, pos = self.next_favorite(last)
        for attempt in range(3):
            # Tapping the row of the sound that's already on turns it off, so look first.
            # Rows shift when the sheet expands, so find the row again each time.
            pos = self.row_position(title) or pos
            if self.row_selected(pos):
                break
            self.tap(*pos)
            time.sleep(2)
            if self.row_selected(self.row_position(title) or pos):
                break
            log.info("tiktok: sound tap didn't select %r, retrying", title)
        else:
            raise RuntimeError(f'TikTok: couldn\'t select the sound "{title}"')
        save_state(last_sound=title, last_sound_at=time.time())
        self.progress["sound"] = title
        self.close_sound_sheet()

    def close_sound_sheet(self):
        """Tap the preview above the sheet."""
        self.tap(self.w / 2, 230 * self.s)
        time.sleep(1)

    def open_favorites(self):
        """Open the sound picker from the editor's sound pill and switch to Favorites. It
        always opens scrolled to the top."""
        for attempt in range(3):
            # TikTok swaps "Add sound" for a sound it picks a moment after the editor opens,
            # which moves the pill; tap once it holds still.
            pill = self.wait_for(self.editor_sound_pill, 10, "the sound button")
            time.sleep(1.5)
            again = self.editor_sound_pill(self.read())
            if again is None or math.dist(pill, again) > 10 * self.s:
                continue
            self.tap(*again)
            try:
                tabs = self.wait_for(lambda it: self.find(it, "Favorites"), 8, "the sound picker")
                break
            except RuntimeError:
                log.info("tiktok: sound picker didn't open, retrying")
        else:
            raise RuntimeError("TikTok: the sound picker didn't open")
        # Picking from the wrong tab would post with a random sound, so make sure Favorites
        # is the selected one (its label turns black) before going on.
        for attempt in range(3):
            tab = self.find_word(self.read(), "Favorites") or tabs
            self.tap(*tab)
            time.sleep(2)
            tab = self.find_word(self.read(), "Favorites") or tab
            if self.tab_selected(tab):
                return
            log.info("tiktok: Favorites tab not selected yet, tapping again")
        raise RuntimeError("TikTok: couldn't switch the sound picker to Favorites")

    def find_word(self, items, word):
        """Centre of `word` on screen, even when OCR joins it with its neighbours into one
        line (e.g. the sound tabs read as "Favorites Recent")."""
        for i in items:
            text = i["text"]
            k = text.lower().find(word.lower())
            if k < 0:
                continue
            frac = (k + len(word) / 2) / len(text)
            return (i["x"] + i["w"] * frac, i["y"] + i["h"] / 2)
        return None

    def tab_selected(self, pos):
        """The selected tab's label is near-black; the others are grey."""
        img = self.frame()
        x, y = int(pos[0]), int(pos[1])
        band = img[max(0, y - int(15 * self.s)):y + int(15 * self.s), max(0, x - int(55 * self.s)):x + int(55 * self.s)]
        return int((band.max(axis=2) < 90).sum()) > 200 * self.s * self.s

    def favorite_rows(self, items):
        """[(title, (x, y))] of sound rows on screen, top to bottom. A row is a title line
        with a "<artist> · N posts · 0:15" line under it."""
        rows = []
        for sub in items:
            if "posts" not in sub["text"].lower():
                continue
            sy = sub["y"] + sub["h"] / 2
            parts = [i for i in items if 25 * self.s < sy - (i["y"] + i["h"] / 2) < 70 * self.s
                     and i["x"] > 120 * self.s and "posts" not in i["text"].lower()]
            if not parts:
                continue
            parts.sort(key=lambda i: i["x"])
            title = " ".join(p["text"].strip() for p in parts)
            ty = sum(p["y"] + p["h"] / 2 for p in parts) / len(parts)
            rows.append((title, (parts[0]["x"] + min(parts[0]["w"], 200 * self.s) / 2, ty)))
        rows.sort(key=lambda r: r[1][1])
        return rows

    def next_favorite(self, last):
        """(title, position) of the favorite after `last`, wrapping to the first one.
        Scrolls down through the list as needed."""
        seen = []
        take_next = last is None
        stale = 0
        for page in range(20):
            rows = self.favorite_rows(self.read())
            if not rows:
                raise RuntimeError("TikTok: no sounds in the Favorites tab (or the sound picker closed)")
            new = False
            for title, pos in rows:
                if any(same_title(title, t) for t in seen):
                    continue
                new = True
                if take_next:
                    return title, pos
                seen.append(title)
                if last and same_title(title, last):
                    take_next = True
            # The first drag can just expand the sheet, so only call it the end of the list
            # after two drags that show nothing new.
            stale = 0 if new else stale + 1
            if stale >= 2:
                break
            self.scroll_list()
        self.progress["favorites"] = seen
        # Past the end (or `last` isn't a favorite any more): back to the first one. Reopen
        # the picker rather than dragging back up, since a drag down at the top closes it.
        self.close_sound_sheet()
        self.open_favorites()
        rows = self.favorite_rows(self.read())
        if not rows:
            raise RuntimeError("TikTok: no sounds in the Favorites tab")
        return rows[0]

    def scroll_list(self):
        self.ctl.swipe(260 * self.s, 1150 * self.s, 260 * self.s, 850 * self.s, human=True)
        time.sleep(1.5)  # let the scroll momentum die down, or the next tap only stops it

    def row_position(self, title):
        return next((pos for t, pos in self.favorite_rows(self.read()) if same_title(t, title)), None)

    def row_selected(self, pos):
        """A selected sound row has its title in TikTok red."""
        img = self.frame()
        y0, y1 = int(pos[1] - 20 * self.s), int(pos[1] + 20 * self.s)
        band = img[max(0, y0):y1, int(140 * self.s):int(560 * self.s)].astype(int)
        b, g, r = band[..., 0], band[..., 1], band[..., 2]
        red = (r > 200) & (g < 110) & (b < 140)
        return red.sum() > 120 * self.s * self.s  # even a short title like "Vibin" has ~300

    # ---- post page ---------------------------------------------------------------------

    def open_post_page(self):
        self.step("opening the post page")
        items = self.read()
        nxt = self.find(items, "Next", exact=True, region=(0.5 * self.w, 0.85 * self.h, self.w, self.h))
        self.tap(*(nxt or (556 * self.s, self.h - 76 * self.s)))
        self.wait_for(lambda it: self.find(it, "Drafts") and self.find(it, "Post"), 30, "the post page")
        time.sleep(1)

    def type_description(self, text):
        text = clean_text(text)
        if not text:
            return
        self.step("typing the description")
        self.focus(lambda it: self.find(it, "Add description") or (160 * self.s, 177 * self.s), self.keyboard_open)
        self.type_text(text)

    def overlay_showing(self, img):
        """While typing, the page under the Hashtags row is dimmed grey; tapping it closes
        the keyboard. (A hashtag/mention suggestion list shows there in white instead.)"""
        strip = img[int(615 * self.s):int(645 * self.s), int(380 * self.s):int(600 * self.s)]
        return 170 < strip.mean() < 230 and strip.std() < 15

    def finish(self, post):
        """Press Drafts, or Post when post is true."""
        label = "Post" if post else "Drafts"
        self.step("posting" if post else "saving the draft")
        # Only the bottom-row buttons: while typing there's another Post at the top right.
        bottom = (0.4 * self.w, 0.8 * self.h, self.w, self.h) if post else (0, 0.8 * self.h, 0.6 * self.w, self.h)
        for attempt in range(12):
            img = self.frame()
            items = self.ocr_fn(img)
            if self.keyboard_open(items):
                if self.overlay_showing(img):
                    self.tap(420 * self.s, 630 * self.s)
                # else a suggestion list is up; it goes away shortly after the trailing space.
                time.sleep(1.5)
                continue
            button = self.find(items, label, exact=True, region=bottom)
            if button:
                # The keyboard can pop back up a moment later (it did after the preview), and
                # its keys sit right where the buttons are; check again before tapping.
                time.sleep(1)
                if not self.keyboard_open(self.read()):
                    self.tap(*button)
                    break
            time.sleep(1)
        else:
            raise RuntimeError(f"TikTok: couldn't get to the {label} button (keyboard wouldn't close?)")
        # Done once the post page is gone: a draft goes back to the feed, but a post opens
        # the just-posted video (views, "Privacy settings") or the profile instead.
        bottom_bar = (0, 0.8 * self.h, self.w, self.h)
        on_post_page = lambda it: (self.find(it, "Drafts", exact=True, region=bottom_bar)
                                   and self.find(it, "Post", exact=True, region=bottom_bar))
        self.wait_for(lambda it: not on_post_page(it), 30, "the next screen after " + ("posting" if post else "saving"))
        time.sleep(2)


class Keyboard:
    """Types text by tapping the iOS on-screen keyboard."""

    def __init__(self, tt):
        self.tt = tt
        self.ctl = tt.ctl
        self.s = tt.s
        self.page = None  # "abc" | "123" | "#+=" | None (unknown)
        self.shift = None  # True/False/None (unknown)
        self.bottom_y = None
        self.moves_since_home = 99

    def locate(self):
        """Find which keyboard page is showing and where the bottom row is."""
        time.sleep(0.35)  # let a page switch finish
        for _ in range(6):
            items = self.tt.read()
            # The bottom-left key says "123" on the letters page and "ABC" on the others. The
            # #+= page also has a "123" key one row up, so take the lowest match.
            left = [i for i in items if i["x"] + i["w"] / 2 < 110 * self.s and i["y"] > 0.7 * self.tt.h]
            bottom = max((i for i in left if i["text"].strip() in ("123", "ABC")), key=lambda i: i["y"], default=None)
            if bottom:
                self.bottom_y = bottom["y"] + bottom["h"] / 2
                if bottom["text"].strip() == "123":
                    self.page = "abc"
                else:
                    third = self.row_y(2)
                    is_sym = any(abs(j["y"] + j["h"] / 2 - third) < 30 * self.s and "12" in j["text"] for j in left)
                    self.page = "#+=" if is_sym else "123"
                log.info("keyboard page %s", self.page)
                return
            time.sleep(0.5)
        raise RuntimeError(f"{self.tt.APP}: the keyboard didn't open")

    def row_y(self, row):
        return self.bottom_y - (3 - row) * ROW_PITCH * self.s

    def shift_on(self):
        img = self.tt.frame()
        x, y = int(SHIFT_X * self.s), int(self.row_y(2))
        crop = img[y - int(20 * self.s):y + int(20 * self.s), x - int(18 * self.s):x + int(18 * self.s)]
        # The arrow is filled black when shift is on, an outline when off.
        return (crop.max(axis=2) < 80).mean() > 0.25

    def press(self, x, y):
        """Tap a key; after one exact tap, hop between keys with relative moves."""
        x *= self.s
        ctl = self.ctl
        time.sleep(random.uniform(0.05, 0.25))
        if self.moves_since_home >= 12 or ctl.pos is None:
            ctl.tap_exact(x, y)
            self.moves_since_home = 0
            return
        done = ctl.esp.move((x - ctl.pos[0]) / ctl.cal_kx, (y - ctl.pos[1]) / ctl.cal_ky)
        ctl.pos = (x, y)
        seen = min(self.tt.pointer_candidates(ctl.fresh_frame(done)), key=lambda p: math.dist(p, (x, y)), default=None)
        if seen and 4 < math.dist(seen, (x, y)) < 45 * self.s:
            ctl.esp.move((x - seen[0]) / ctl.cal_kx, (y - seen[1]) / ctl.cal_ky)
        elif seen is None or math.dist(seen, (x, y)) >= 45 * self.s:
            # Lost track of the pointer: start again from a corner.
            ctl.tap_exact(x, y)
            self.moves_since_home = 0
            return
        ctl.press(humanize.click_hold())
        self.moves_since_home += 1

    def to_page(self, page):
        if self.page is None:
            self.locate()
        if self.page == page:
            return
        if page == "abc":
            self.press(MODE_X, self.row_y(3))
        elif page == "123":
            if self.page == "abc":
                self.press(MODE_X, self.row_y(3))
            else:
                self.press(MODE_X, self.row_y(2))
        else:
            if self.page == "abc":
                self.press(MODE_X, self.row_y(3))
                time.sleep(0.2)
            self.press(MODE_X, self.row_y(2))
        time.sleep(0.25)
        self.page = page
        self.shift = None

    def key_for(self, ch):
        """(page, x, row) for a character, or None if the keyboard can't type it."""
        low = ch.lower()
        for r, row in enumerate(LETTER_ROWS):
            if low in row and ch.isascii() and ch.isalpha():
                return "abc", LETTER_ROW_X0[r] + row.index(low) * KEY_PITCH, r
        if ch == "@":
            return "abc", LETTERS_AT_X, 3
        if ch == "#":
            return "abc", LETTERS_HASH_X, 3
        if ch == " ":
            return self.page or "abc", SPACE_X, 3
        if ch == "\n":
            return "123", RETURN_X, 3
        if ch in PUNCT_ROW:
            return (self.page if self.page in ("123", "#+=") else "123"), PUNCT_X[PUNCT_ROW.index(ch)], 2
        for page, rows in (("123", NUM_ROWS), ("#+=", SYM_ROWS)):
            for r, row in enumerate(rows):
                if ch in row:
                    return page, 45 + row.index(ch) * KEY_PITCH, r
        return None

    def type(self, text):
        self.locate()
        skipped = []
        for i, ch in enumerate(text):
            if self.page is None:
                self.locate()
            key = self.key_for(ch)
            if key is None:
                skipped.append(ch)
                continue
            page, x, row = key
            self.to_page(page)
            if page == "abc" and ch.isalpha():
                want = ch.isupper()
                if self.shift is None:
                    self.shift = self.shift_on()
                if self.shift != want:
                    self.press(SHIFT_X, self.row_y(2))
                    time.sleep(0.15)
            self.press(x, self.row_y(row))
            self.tt.progress["typed"] = i + 1
            # What iOS does next isn't always predictable (auto-capitalisation after ". ",
            # jumping back to letters after a space or '), so re-check when it matters.
            if ch.isalpha():
                self.shift = False
            else:
                self.shift = None
                if page != "abc" and (ch in " '\n"):
                    self.page = None
        return skipped
