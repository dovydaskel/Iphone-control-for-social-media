"""Files waiting to be imported into the iPhone's Photos library.

They go over the USB cable (see usb_media.py); the server taps the "USB Import"
Shortcut with the ESP mouse to save them to Photos.

Item status: queued -> sending -> copying -> importing -> downloaded (in Photos), or failed.
"""

import mimetypes
import shutil
import threading
import time
import uuid
from pathlib import Path

MEDIA_DIR = Path(__file__).resolve().parent / "media"
CHUNK = 1024 * 1024


class MediaStore:
    def __init__(self):
        MEDIA_DIR.mkdir(exist_ok=True)
        self.items = {}
        self._lock = threading.Lock()

    def add(self, filename, fileobj):
        item_id = uuid.uuid4().hex[:12]
        name = Path(filename or "file").name
        path = MEDIA_DIR / f"{item_id}_{name}"
        with open(path, "wb") as out:
            shutil.copyfileobj(fileobj, out, CHUNK)
        item = {
            "id": item_id,
            "name": name,
            "size": path.stat().st_size,
            "type": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "status": "queued",
            "error": None,
            "added": time.time(),
            "path": path,
        }
        with self._lock:
            self.items[item_id] = item
        return self.public(item)

    def get(self, item_id):
        with self._lock:
            return self.items.get(item_id)

    def remove(self, item_id):
        with self._lock:
            item = self.items.pop(item_id, None)
        if item:
            item["path"].unlink(missing_ok=True)
        return item is not None

    def set_status(self, item_id, status, error=None):
        with self._lock:
            item = self.items.get(item_id)
            if item:
                item["status"], item["error"] = status, error

    def take_queued(self):
        """Mark every queued item as sending; returns them oldest first."""
        with self._lock:
            items = sorted((i for i in self.items.values() if i["status"] == "queued"), key=lambda i: i["added"])
            for item in items:
                item["status"], item["error"] = "sending", None
            return items

    @staticmethod
    def public(item):
        return {k: v for k, v in item.items() if k != "path"}

    def list(self):
        with self._lock:
            return [self.public(i) for i in sorted(self.items.values(), key=lambda i: i["added"], reverse=True)]
