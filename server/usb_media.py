"""Copy files onto the iPhone over the USB cable, no network involved.

Files go into a folder inside VLC's Documents (VLC exposes its Documents over USB and
shows up in Files under On My iPhone). The "USB Import" Shortcut on the phone saves
everything in that folder to Photos in one run. New files in the camera roll (DCIM)
mean it's done; the Mac then clears the folder itself.

Needs the phone plugged in and trusted ("Trust This Computer"); no Developer Mode.
"""

import asyncio
import os
import posixpath
import time
from contextlib import asynccontextmanager

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.house_arrest import HouseArrestService
from pymobiledevice3.usbmux import list_devices

APP_BUNDLE_ID = os.environ.get("USB_IMPORT_APP", "org.videolan.vlc-ios")
FOLDER = "/Documents/" + os.environ.get("USB_IMPORT_FOLDER", "Mac Import")


def _run(coro):
    return asyncio.run(coro)


def phone_connected():
    try:
        devices = _run(list_devices())
    except Exception:
        return False
    return any(d.connection_type == "USB" for d in devices)


@asynccontextmanager
async def _session():
    """AFC access to the import app's container."""
    lockdown = await create_using_usbmux(connection_type="USB")
    try:
        # App Store apps only allow access to their Documents, not the whole container.
        afc = await HouseArrestService.create(lockdown, APP_BUNDLE_ID, documents_only=True)
        try:
            yield afc
        finally:
            await afc.close()
    finally:
        await lockdown.close()


@asynccontextmanager
async def _media_session():
    """AFC access to the camera roll (DCIM)."""
    lockdown = await create_using_usbmux(connection_type="USB")
    afc = AfcService(lockdown)
    try:
        yield afc
    finally:
        await afc.close()
        await lockdown.close()


async def _clear(afc):
    for name in await afc.listdir(FOLDER):
        await afc.rm(posixpath.join(FOLDER, name), force=True)


async def _push(files):
    async with _session() as afc:
        await afc.makedirs(FOLDER)
        # Clear leftovers from an interrupted run so the Shortcut imports only these files.
        await _clear(afc)
        for local_path, remote_name in files:
            await afc.push(str(local_path), posixpath.join(FOLDER, remote_name), progress_bar=False)


def push(files):
    """Copy [(local_path, remote_name), ...] into the import folder on the phone."""
    _run(_push(files))


def ensure_folder():
    """Create the import folder so the Shortcut can be pointed at it."""
    async def make():
        async with _session() as afc:
            await afc.makedirs(FOLDER)
    _run(make())


def clear():
    """Empty the import folder."""
    async def run():
        async with _session() as afc:
            if await afc.exists(FOLDER):
                await _clear(afc)
    _run(run())


def camera_roll():
    """Paths of everything in the camera roll."""
    async def run():
        async with _media_session() as afc:
            paths = set()
            for d in await afc.listdir("/DCIM"):
                if d[:1].isdigit():
                    paths.update(f"/DCIM/{d}/{f}" for f in await afc.listdir(f"/DCIM/{d}"))
            return paths
    return _run(run())

