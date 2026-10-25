"""Find an X11 window by title and grab its pixels.

Uses libX11 through ctypes for window discovery/geometry (no python-xlib
dependency) and Pillow's ImageGrab for the actual pixels.

X11 only: on a Wayland session the same code works against XWayland, as long as
the target application is an X11 client (Avalonia apps usually are).
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass

from PIL import Image, ImageGrab

IsViewable = 2


class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("width", ctypes.c_int),
        ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("class_", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XClientMessageData(ctypes.Union):
    _fields_ = [
        ("b", ctypes.c_char * 20),
        ("s", ctypes.c_short * 10),
        ("l", ctypes.c_long * 5),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", _XClientMessageData),
    ]


class _XEvent(ctypes.Union):
    """XEvent is a union of at least 24 longs; Xlib reads the whole thing."""

    _fields_ = [
        ("xclient", _XClientMessageEvent),
        ("pad", ctypes.c_long * 24),
    ]


@dataclass(frozen=True)
class WindowInfo:
    id: int
    title: str
    x: int
    y: int
    width: int
    height: int
    viewable: bool
    override_redirect: bool = False

    @property
    def area(self) -> int:
        return self.width * self.height


class CaptureError(RuntimeError):
    pass


class _X:
    """Minimal Xlib binding, opened once per process."""

    def __init__(self) -> None:
        try:
            self.lib = ctypes.CDLL("libX11.so.6")
        except OSError as exc:  # pragma: no cover - host dependent
            raise CaptureError(f"libX11 not available: {exc}") from exc

        # Captures may run on a worker thread while GTK owns the main thread.
        try:
            self.lib.XInitThreads.restype = ctypes.c_int
            self.lib.XInitThreads()
        except AttributeError:
            pass

        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.dpy = self.lib.XOpenDisplay(os.environ.get("DISPLAY", ":0").encode())
        if not self.dpy:
            raise CaptureError("cannot open X display (DISPLAY unset or not X11)")

        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.root = self.lib.XDefaultRootWindow(self.dpy)

        self.lib.XQueryTree.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.lib.XFetchName.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                        ctypes.POINTER(ctypes.c_char_p)]
        self.lib.XGetWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_XWindowAttributes)]
        self.lib.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        # Every signature is declared up front. Leaving one to the implicit
        # default makes ctypes pass a 64-bit Display* as a 32-bit int, which
        # crashes inside libX11 as soon as that function is reached.
        self.lib.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.lib.XFlush.argtypes = [ctypes.c_void_p]
        self.lib.XFree.argtypes = [ctypes.c_void_p]
        self.lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                        ctypes.c_int, ctypes.c_long,
                                        ctypes.c_void_p]
        self.lib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
        ]
        self.lib.XInternAtom.restype = ctypes.c_ulong
        self.lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                         ctypes.c_int]

    def children(self, window: int) -> list[int]:
        root = ctypes.c_ulong()
        parent = ctypes.c_ulong()
        kids = ctypes.POINTER(ctypes.c_ulong)()
        count = ctypes.c_uint(0)
        if not self.lib.XQueryTree(self.dpy, window, ctypes.byref(root),
                                   ctypes.byref(parent), ctypes.byref(kids),
                                   ctypes.byref(count)):
            return []
        try:
            return [kids[i] for i in range(count.value)]
        finally:
            if kids:
                self.lib.XFree(kids)

    def name(self, window: int) -> str:
        ptr = ctypes.c_char_p()
        if not self.lib.XFetchName(self.dpy, window, ctypes.byref(ptr)):
            return ""
        if not ptr.value:
            return ""
        try:
            return ptr.value.decode(errors="replace")
        finally:
            self.lib.XFree(ptr)

    def info(self, window: int) -> WindowInfo | None:
        attrs = _XWindowAttributes()
        if not self.lib.XGetWindowAttributes(self.dpy, window, ctypes.byref(attrs)):
            return None
        abs_x, abs_y = ctypes.c_int(0), ctypes.c_int(0)
        child = ctypes.c_ulong()
        self.lib.XTranslateCoordinates(self.dpy, window, self.root, 0, 0,
                                       ctypes.byref(abs_x), ctypes.byref(abs_y),
                                       ctypes.byref(child))
        return WindowInfo(
            id=window,
            title=self.name(window),
            x=abs_x.value,
            y=abs_y.value,
            width=attrs.width,
            height=attrs.height,
            viewable=attrs.map_state == IsViewable,
            override_redirect=bool(attrs.override_redirect),
        )

    def raise_window(self, window: int) -> None:
        self.lib.XRaiseWindow(self.dpy, window)
        self.lib.XFlush(self.dpy)

    def map_raised(self, window: int) -> None:
        """Un-minimise / map a window and put it on top."""
        self.lib.XMapRaised(self.dpy, window)
        self.lib.XFlush(self.dpy)

    def frame_extents(self, window: int) -> tuple[int, int, int, int]:
        """Client-side-decoration shadow margins (left, right, top, bottom).

        GTK3 windows declare their shadow through _GTK_FRAME_EXTENTS. The X
        window geometry includes that margin, but the margin shows whatever is
        behind the window, so it must be cut off before measuring pixels.
        """
        try:
            atom = self.lib.XInternAtom(self.dpy, b"_GTK_FRAME_EXTENTS", 1)
            if not atom:
                return (0, 0, 0, 0)
            actual_type = ctypes.c_ulong()
            actual_format = ctypes.c_int()
            nitems = ctypes.c_ulong()
            bytes_after = ctypes.c_ulong()
            prop = ctypes.POINTER(ctypes.c_ulong)()
            status = self.lib.XGetWindowProperty(
                self.dpy, window, atom, 0, 4, 0, 6,  # XA_CARDINAL
                ctypes.byref(actual_type), ctypes.byref(actual_format),
                ctypes.byref(nitems), ctypes.byref(bytes_after),
                ctypes.byref(prop))
            if status != 0 or not prop or nitems.value < 4:
                return (0, 0, 0, 0)
            try:
                values = [prop[i] for i in range(4)]
            finally:
                self.lib.XFree(prop)
            return tuple(int(v) for v in values)  # type: ignore[return-value]
        except Exception:
            return (0, 0, 0, 0)

    def is_topmost(self, window: int) -> bool:
        """True when nothing visible covers `window`.

        Stacking order alone is not enough: docks and panels sit above normal
        windows but do not overlap them, so only windows that geometrically
        intersect the target count as covering it. Very thin/edge windows
        (panels, taskbars) are ignored so a maximised target still passes.
        """
        infos: list[tuple[int, WindowInfo]] = []
        for wid in self.children(self.root):
            info = self.info(wid)
            if info is not None:
                infos.append((wid, info))

        index = None
        for i, (wid, info) in enumerate(infos):
            if wid == window or window in self.children(wid):
                index = i
                break
        if index is None:
            return False

        target = infos[index][1]
        tx0, ty0 = target.x, target.y
        tx1, ty1 = tx0 + target.width, ty0 + target.height
        for _wid, info in infos[index + 1:]:
            if not info.viewable or info.override_redirect:
                continue
            if info.width < 48 or info.height < 48:
                continue  # panel / taskbar / drag icon
            if (info.x < tx1 and info.x + info.width > tx0
                    and info.y < ty1 and info.y + info.height > ty0):
                return False
        return True

    def activate(self, window: int) -> None:
        """Ask the window manager to activate (raise + focus) a window.

        Plain XRaiseWindow loses against the WM's stacking policy, so send the
        EWMH _NET_ACTIVE_WINDOW client message the way pagers do. This is what
        makes a capture reliable when other windows overlap the target.
        """
        try:
            event = _XEvent()
            msg = event.xclient
            msg.type = 33  # ClientMessage
            msg.window = window
            msg.message_type = self.lib.XInternAtom(
                self.dpy, b"_NET_ACTIVE_WINDOW", 0)
            msg.format = 32
            msg.data.l[0] = 2  # source indication: pager
            msg.data.l[1] = 0  # CurrentTime

            mask = (1 << 20) | (1 << 19)  # SubstructureRedirect | SubstructureNotify
            self.lib.XSendEvent(self.dpy, self.root, 0, mask, ctypes.byref(event))
            self.lib.XRaiseWindow(self.dpy, window)
            self.lib.XFlush(self.dpy)
        except Exception:
            self.lib.XRaiseWindow(self.dpy, window)
            self.lib.XFlush(self.dpy)


_X_SINGLETON: _X | None = None


def _get_x() -> _X:
    global _X_SINGLETON
    if _X_SINGLETON is None:
        _X_SINGLETON = _X()
    return _X_SINGLETON


def find_windows(title: str) -> list[WindowInfo]:
    """All windows whose title contains `title` (case-insensitive)."""
    x = _get_x()
    needle = title.casefold()
    found: list[WindowInfo] = []

    def walk(window: int, depth: int) -> None:
        if depth > 12:
            return
        for kid in x.children(window):
            info = x.info(kid)
            if info and needle in info.title.casefold():
                found.append(info)
            walk(kid, depth + 1)

    walk(x.root, 0)
    return found


def find_window(title: str) -> WindowInfo | None:
    """Best match: the largest viewable window with that title."""
    matches = find_windows(title)
    if not matches:
        return None
    viewable = [w for w in matches if w.viewable and w.width > 50 and w.height > 50]
    pool = viewable or matches
    return max(pool, key=lambda w: w.area)


def _grab(info: WindowInfo) -> Image.Image:
    """Copy the window's screen rectangle as RGB."""
    # Re-read the geometry: activating a window can make the WM move it, and a
    # stale origin shifts the crop into whatever is next to the window.
    fresh = _get_x().info(info.id)
    if fresh is not None:
        info = fresh
    shadow_left, shadow_right, shadow_top, shadow_bottom = \
        _get_x().frame_extents(info.id)
    screen = ImageGrab.grab(xdisplay=os.environ.get("DISPLAY", ":0"))
    sw, sh = screen.size
    left = max(0, info.x + shadow_left)
    top = max(0, info.y + shadow_top)
    right = min(sw, info.x + info.width - shadow_right)
    bottom = min(sh, info.y + info.height - shadow_bottom)
    if right - left < 10 or bottom - top < 10:
        raise CaptureError(
            f"window {info.title!r} is off-screen "
            f"({info.x},{info.y} {info.width}x{info.height})")
    return screen.crop((left, top, right, bottom))


def bring_to_front(info: WindowInfo, attempts: int = 4) -> WindowInfo:
    """Make sure the window is mapped, on top and settled, then return it.

    Screen grabbing only sees the top window, so a target hidden behind the
    verifier (or minimised by the WM) has to be raised and given time to
    repaint, otherwise the capture silently returns someone else's pixels.
    """
    x = _get_x()
    fresh = info
    for _ in range(attempts):
        if not fresh.viewable:
            x.map_raised(info.id)
            time.sleep(0.2)
        x.activate(info.id)
        time.sleep(0.35)  # let the WM restack and the app repaint
        current = x.info(info.id)
        if current is not None:
            fresh = current
        if fresh.viewable and x.is_topmost(info.id):
            return fresh
    if not fresh.viewable:
        raise CaptureError(
            f"window {info.title!r} is minimised and could not be restored")
    raise CaptureError(
        f"window {info.title!r} is covered by another window; bring it to "
        f"the front (or close the window on top of it) and retry")


def capture(window: WindowInfo, raise_first: bool = True) -> Image.Image:
    """Grab the window's pixels as RGB."""
    info = bring_to_front(window) if raise_first else window
    return _grab(info)


def capture_by_title(title: str) -> tuple[Image.Image, WindowInfo]:
    info = find_window(title)
    if info is None:
        raise CaptureError(f"no window matching {title!r}")
    info = bring_to_front(info)
    return _grab(info), info


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="List or capture X11 windows")
    ap.add_argument("--title", default="Performance Monitor")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    for w in find_windows(args.title):
        state = "viewable" if w.viewable else "hidden"
        print(f"0x{w.id:08x}  {w.width}x{w.height}+{w.x}+{w.y}  {state}  {w.title!r}")

    if args.out:
        image, info = capture_by_title(args.title)
        image.save(args.out)
        print(f"saved {args.out} ({image.size[0]}x{image.size[1]}) from "
              f"{info.title!r} at +{info.x}+{info.y}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
