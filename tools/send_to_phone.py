#!/usr/bin/env python3
"""Put photos/videos from this computer into the iPhone's Photos library.

    .venv/bin/python tools/send_to_phone.py video.mp4 [more files...] [--server URL] [--no-wait]

Uploads the files to the iPhone control server, which copies them over the USB cable
(the phone must be plugged in) and runs the "USB Import" Shortcut once for all of them.
Waits for the imports to finish and exits non-zero if any failed.
"""

import argparse
import sys
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument("--no-wait", action="store_true", help="queue and return without waiting")
    parser.add_argument("--timeout", type=float, default=180, help="seconds to wait per file")
    args = parser.parse_args()

    failed = 0
    items = {}
    with httpx.Client(base_url=args.server, timeout=60) as client:
        for path in args.files:
            if not path.is_file():
                print(f"{path}: not a file", file=sys.stderr)
                failed += 1
                continue
            with open(path, "rb") as f:
                resp = client.post("/api/media", files={"file": (path.name, f)}, data={"send": "false"})
            resp.raise_for_status()
            item = resp.json()
            items[item["id"]] = item
            print(f"{path.name}: queued ({item['size'] / 1e6:.1f} MB)", flush=True)
        if not items:
            sys.exit(1)
        # One import for all of them: over USB that's a single Shortcut run.
        client.post("/api/media/send").raise_for_status()
        if args.no_wait:
            sys.exit(1 if failed else 0)

        deadline = time.time() + args.timeout * len(items)
        pending = dict(items)
        while pending and time.time() < deadline:
            for item_id, old in list(pending.items()):
                item = client.get(f"/api/media/{item_id}").json()
                if item["status"] != old["status"]:
                    print(f"{item['name']}: {item['status']}", flush=True)
                pending[item_id] = item
                if item["status"] == "failed":
                    print(f"{item['name']}: FAILED: {item['error']}", file=sys.stderr)
                    failed += 1
                if item["status"] in ("downloaded", "failed"):
                    del pending[item_id]
            time.sleep(0.5)
        for item in pending.values():
            print(f"{item['name']}: timed out (status {item['status']})", file=sys.stderr)
            failed += 1
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
