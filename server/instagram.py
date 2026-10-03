"""Post the newest video in Photos as an Instagram reel: + -> pick it -> Next -> Next ->
caption -> Save draft (or Share).

Meant to run after a TikTok post, which saves the posted video (with its sound) to Photos,
so the newest video is the one to use. Instagram's suggested audio is left alone so that
sound stays. Same approach as tiktok.py: OCR to find things, tap_exact to tap them.
"""

import time

from tiktok import DISCARD_LABELS, ScreenFlow, clean_text


class InstagramPoster(ScreenFlow):
    APP = "Instagram"

    def run(self, description, post=False, video=1):
        """video: which video in the gallery grid to use, 1 = the first one."""
        self.open_new_reel()
        self.pick_video(video)
        self.to_share_page()
        self.type_caption(description)
        self.finish(post)
        self.step("done")

    def bottom(self, left=0.0, right=1.0):
        return (left * self.w, 0.85 * self.h, right * self.w, self.h)

    def gallery_showing(self, items):
        return self.find(items, "Recents") and (self.find(items, "New reel") or self.find(items, "New post"))

    def share_page_showing(self, items):
        return self.find(items, "Share", exact=True, region=self.bottom(0.5)) and self.find(items, "Save draft")

    def open_new_reel(self):
        self.step("opening Instagram")
        self.ctl.open_home_screen_icon("Instagram")
        time.sleep(4)
        # Instagram reopens where it was left: maybe another tab, or a half-made reel from a
        # failed run. Work back to the home feed, whose + (top left) opens the new reel screen.
        for attempt in range(8):
            items = self.read()
            if self.gallery_showing(items):
                break
            if self.reopen_from_home_screen(items):
                continue
            discard = next((self.find(items, label, exact=True) for label in DISCARD_LABELS
                            if self.find(items, label, exact=True)), None)
            if self.find(items, "Your story"):
                self.tap(56 * self.s, 91 * self.s)
            elif discard:
                self.tap(*discard)
            elif self.find(items, "Next") or self.find(items, "Share", exact=True) or self.find(items, "Caption"):
                self.tap(*self.back_button(items, attempt))  # leave the reel screens
            elif self.back_button(items, guess=False):
                self.tap(*self.back_button(items))  # e.g. the drafts list
            else:
                self.tap(75 * self.s, self.h - 50 * self.s)  # Home tab
            time.sleep(2.5)
        else:
            raise RuntimeError("Instagram: the new reel screen didn't open")
        self.make_reel()

    def make_reel(self):
        """Switch the STORY / POST / REEL mode strip at the bottom to REEL if needed."""
        items = self.read()
        if self.find(items, "New reel"):
            return
        self.step("switching to Reel")
        for i in items:
            t = i["text"].upper()
            if "REEL" in t and i["y"] > 0.85 * self.h:
                # The strip is often read as one line: aim at the word inside it.
                k = t.index("REEL")
                x = i["x"] + i["w"] * (k + 2) / max(1, len(t))
                self.tap(x, i["y"] + i["h"] / 2)
                self.wait_for(lambda it: self.find(it, "New reel"), 8, "the Reel mode")
                return
        raise RuntimeError("Instagram: couldn't find the REEL mode")

    def pick_video(self, video=1):
        self.step("picking video %d in the gallery" % video)
        time.sleep(2.5)  # thumbnails ignore taps for a moment after the gallery opens
        # In multi-select mode ("Cancel" next to Recents) a selection survives from earlier
        # runs, and a tap would add a second video; leave that mode so a tap opens just one.
        items = self.read()
        cancel = self.find(items, "Cancel", exact=True, region=(0.6 * self.w, 250 * self.s, self.w, 400 * self.s))
        if cancel:
            self.tap(*cancel)
            self.wait_for(lambda it: self.find(it, "Select", exact=True), 8, "single-select mode")
        # The grid is 3 across and starts with a camera tile, so video n is cell n + 1.
        cell = video + 1
        col, row = (cell - 1) % 3, (cell - 1) // 3
        x, y = (124 + 250 * col) * self.s, (594 + 440 * row) * self.s
        for attempt in range(3):
            self.tap(x, y)
            time.sleep(3)
            if not self.gallery_showing(self.read()):
                return
        raise RuntimeError("Instagram: tapping the video didn't open it")

    def to_share_page(self):
        self.step("opening the share page")
        # The editor; its Next is at the bottom right.
        nxt = self.wait_for(lambda it: self.find(it, "Next", region=self.bottom(0.6))
                            and self.find(it, "Edit video"), 30, "the editor")
        time.sleep(1.5)
        items = self.read()
        self.tap(*(self.find(items, "Next", region=self.bottom(0.6)) or nxt))
        self.wait_for(self.share_page_showing, 30, "the share page")
        time.sleep(1)

    def type_caption(self, text):
        text = clean_text(text)
        if not text:
            return
        self.step("typing the caption")
        # Opens a separate Caption screen with the keyboard up and an OK button.
        self.focus(lambda it: self.find(it, "Add a caption") or (135 * self.s, 800 * self.s),
                   lambda it: self.find(it, "Caption", exact=True) and self.keyboard_open(it), "the caption screen")
        self.type_text(text)
        time.sleep(1.5)  # hashtag/mention suggestions settle
        items = self.read()
        ok = self.find(items, "OK", exact=True, region=(0.7 * self.w, 0, self.w, 0.12 * self.h))
        self.tap(*(ok or (676 * self.s, 84 * self.s)))
        self.wait_for(self.share_page_showing, 10, "the share page after the caption")

    def finish(self, post):
        label = "Share" if post else "Save draft"
        self.step("sharing" if post else "saving the draft")
        items = self.read()
        button = self.find(items, label, exact=True, region=self.bottom(0.5 if post else 0.0, 1.0 if post else 0.5))
        if not button:
            raise RuntimeError(f"Instagram: no {label} button")
        self.tap(*button)
        # Instagram leaves the share page once it's saved / the upload starts.
        deadline = time.time() + 30
        while self.share_page_showing(self.read()):
            if time.time() > deadline:
                raise RuntimeError(f"Instagram: still on the share page after {label}")
            time.sleep(1)
