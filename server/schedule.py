"""Routines: a list of steps (send a video to Photos, scroll TikTok/Instagram, upload, delete
videos, wait) that run at a set time on chosen days, e.g. every day at 9:00 send the next
video, scroll TikTok for 15 min, then post it.

Each routine has its own queue of videos (files kept in routine_videos/<id>/); every run's
"send video" step takes the next one, so a batch picked once covers several days.

Stored in routines.json. A background thread checks every few seconds and queues a routine
once per day when its time comes (plus an optional random delay, so it isn't to the minute).
Times are this Mac's local time; if the Mac is asleep at that time the run is skipped unless
it wakes within GRACE of it.
"""

import datetime as dt
import json
import random
import shutil
import threading
import time
import uuid
from pathlib import Path

FILE = Path(__file__).resolve().parent / "routines.json"
VIDEO_DIR = Path(__file__).resolve().parent / "routine_videos"
GRACE = dt.timedelta(minutes=60)
STEP_TYPES = {"send_video", "autoscroll", "upload", "delete_videos", "wait"}
AUTOSCROLL_APPS = {"tiktok", "instagram"}

_lock = threading.Lock()
_offsets = {}  # (routine id, date) -> random delay in seconds, fixed for the day


def load():
    try:
        return json.loads(FILE.read_text()).get("routines", [])
    except (OSError, ValueError):
        return []


def _save(routines):
    FILE.write_text(json.dumps({"routines": routines}, indent=2))


def get(routine_id):
    return next((r for r in load() if r["id"] == routine_id), None)


def clean_step(step):
    """Validated copy of a step, or ValueError."""
    kind = step.get("type")
    if kind not in STEP_TYPES:
        raise ValueError(f"unknown step {kind!r}")
    if kind == "autoscroll":
        if step.get("app") not in AUTOSCROLL_APPS:
            raise ValueError("scroll step needs app tiktok or instagram")
        return {"type": kind, "app": step["app"], "minutes": min(max(float(step.get("minutes") or 10), 1), 240)}
    if kind == "upload":
        finish = step.get("finish", "draft")
        if finish not in ("draft", "post"):
            raise ValueError("upload step: finish must be draft or post")
        # An empty description means "use the one saved in the TikTok / Instagram card".
        return {"type": kind, "finish": finish, "instagram": bool(step.get("instagram")),
                "description": str(step.get("description") or "")}
    if kind == "wait":
        return {"type": kind, "minutes": min(max(float(step.get("minutes") or 1), 0.1), 240)}
    if kind == "send_video":
        # Clearing Photos first leaves only this video there, which is what the upload picks.
        return {"type": kind, "clear_first": bool(step.get("clear_first", True))}
    return {"type": kind}


def save_routine(data):
    """Create or update a routine from the web form; returns it."""
    try:
        hour, minute = (int(x) for x in str(data.get("time", "")).split(":"))
        assert 0 <= hour < 24 and 0 <= minute < 60
    except (ValueError, AssertionError):
        raise ValueError("time must be HH:MM")
    days = sorted({int(d) for d in data.get("days", []) if 0 <= int(d) <= 6})
    steps = [clean_step(s) for s in data.get("steps", [])]
    if not steps:
        raise ValueError("add at least one step")
    with _lock:
        routines = load()
        existing = next((r for r in routines if r["id"] == data.get("id")), None)
        routine = existing or {"id": uuid.uuid4().hex[:8], "last_run_date": None, "last_result": None, "videos": []}
        routine.update(
            name=str(data.get("name") or "Routine")[:60],
            enabled=bool(data.get("enabled", True)),
            time=f"{hour:02d}:{minute:02d}",
            days=days,
            jitter=min(max(int(data.get("jitter") or 0), 0), 120),
            steps=steps,
        )
        if not existing:
            # Created after today's time has passed: start tomorrow rather than right away.
            if _due(routine, dt.date.today()) <= dt.datetime.now():
                routine["last_run_date"] = dt.date.today().isoformat()
            routines.append(routine)
        _offsets.pop((routine["id"], dt.date.today().isoformat()), None)
        _save(routines)
        return routine


def delete(routine_id):
    with _lock:
        routines = load()
        kept = [r for r in routines if r["id"] != routine_id]
        _save(kept)
    shutil.rmtree(VIDEO_DIR / routine_id, ignore_errors=True)
    return len(kept) != len(routines)


# ---- video queue --------------------------------------------------------------------------

def _edit_videos(routine_id, change):
    """Apply change(videos) to a routine's queue under the lock; returns its result."""
    with _lock:
        routines = load()
        routine = next((r for r in routines if r["id"] == routine_id), None)
        if routine is None:
            raise KeyError(routine_id)
        videos = routine.setdefault("videos", [])
        result = change(videos)
        _save(routines)
        return result


def add_video(routine_id, filename, fileobj):
    """Append a file to the end of the routine's queue."""
    if get(routine_id) is None:
        raise KeyError(routine_id)
    video_id = uuid.uuid4().hex[:10]
    name = Path(filename or "video.mp4").name
    folder = VIDEO_DIR / routine_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{video_id}_{name}"
    with open(path, "wb") as out:
        shutil.copyfileobj(fileobj, out, 1024 * 1024)
    entry = {"id": video_id, "name": name, "size": path.stat().st_size, "added": time.time()}
    _edit_videos(routine_id, lambda videos: videos.append(entry))
    return entry


def video_path(routine_id, entry):
    return VIDEO_DIR / routine_id / f'{entry["id"]}_{entry["name"]}'


def remove_video(routine_id, video_id):
    def change(videos):
        entry = next((v for v in videos if v["id"] == video_id), None)
        if entry:
            videos.remove(entry)
        return entry
    entry = _edit_videos(routine_id, change)
    if entry:
        video_path(routine_id, entry).unlink(missing_ok=True)
    return entry is not None


def move_video(routine_id, video_id, delta):
    def change(videos):
        i = next((k for k, v in enumerate(videos) if v["id"] == video_id), None)
        if i is None:
            return False
        j = min(max(i + delta, 0), len(videos) - 1)
        videos.insert(j, videos.pop(i))
        return True
    return _edit_videos(routine_id, change)


def next_video(routine_id):
    """The first queued video (entry, path), or None. It stays queued until used_video()."""
    routine = get(routine_id)
    for entry in (routine or {}).get("videos", []):
        path = video_path(routine_id, entry)
        if path.exists():
            return entry, path
    return None


def used_video(routine_id, entry):
    """Take a video off the queue once it's in Photos; remembers the last one sent."""
    def change(videos):
        videos[:] = [v for v in videos if v["id"] != entry["id"]]
    _edit_videos(routine_id, change)
    update(routine_id, last_video={"name": entry["name"], "at": time.time()})
    video_path(routine_id, entry).unlink(missing_ok=True)


def update(routine_id, **changes):
    with _lock:
        routines = load()
        for r in routines:
            if r["id"] == routine_id:
                r.update(changes)
        _save(routines)


def _due(routine, day):
    hour, minute = (int(x) for x in routine["time"].split(":"))
    key = (routine["id"], day.isoformat())
    if key not in _offsets:
        _offsets[key] = random.uniform(0, routine.get("jitter", 0) * 60)
    return dt.datetime.combine(day, dt.time(hour, minute)) + dt.timedelta(seconds=_offsets[key])


def next_run(routine, now=None):
    """When it will next start (with today's random delay), or None if it never will."""
    if not routine.get("enabled") or not routine.get("days"):
        return None
    now = now or dt.datetime.now()
    for ahead in range(8):
        day = now.date() + dt.timedelta(days=ahead)
        if day.weekday() not in routine["days"] or routine.get("last_run_date") == day.isoformat():
            continue
        due = _due(routine, day)
        if due + GRACE >= now:
            return max(due, now)
    return None


def due_routines():
    """Routines whose time has come today and that haven't run today; marks them as run."""
    now = dt.datetime.now()
    today = now.date()
    started = []
    with _lock:
        routines = load()
        for r in routines:
            if not r.get("enabled") or today.weekday() not in r.get("days", []):
                continue
            if r.get("last_run_date") == today.isoformat():
                continue
            due = _due(r, today)
            if due <= now <= due + GRACE:
                r["last_run_date"] = today.isoformat()
                started.append(r)
        if started:
            _save(routines)
    return started


def start(enqueue):
    """Background thread that calls enqueue(routine) when a routine is due."""
    def loop():
        while True:
            try:
                for r in due_routines():
                    enqueue(r)
            except Exception:
                pass
            time.sleep(10)
    threading.Thread(target=loop, daemon=True, name="scheduler").start()


def describe_step(step):
    if step["type"] == "send_video":
        return "send next video to Photos" + (" (clear first)" if step.get("clear_first") else "")
    if step["type"] == "autoscroll":
        return f'scroll {"TikTok" if step["app"] == "tiktok" else "Instagram"} {step["minutes"]:g} min'
    if step["type"] == "upload":
        where = "TikTok + Instagram" if step["instagram"] else "TikTok"
        return f'{"post" if step["finish"] == "post" else "draft"} on {where}'
    if step["type"] == "wait":
        return f'wait {step["minutes"]:g} min'
    return "delete videos from Photos"
