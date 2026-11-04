"""Native desktop input, never CDP. Windows and X11 backends are not yet field-tested.

Coordinates are screen points (logical pixels). On macOS Quartz uses a top-left
origin; on Windows conversion to physical pixels happens at the SendInput boundary.
Only the caller can ensure another person is not moving the shared desktop cursor.
"""
from __future__ import annotations

import contextlib
import ctypes
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time


def _accessibility_trusted(prompt: bool = False) -> bool:
    # ApplicationServices is an OS framework, not an additional Python dependency.
    services = ctypes.CDLL(
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    if not prompt:
        services.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(services.AXIsProcessTrusted())
    import objc
    from Foundation import NSDictionary
    options = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
    services.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    services.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    return bool(services.AXIsProcessTrustedWithOptions(objc.pyobjc_id(options)))


def is_available(prompt: bool = False) -> tuple[bool, str]:
    """Check imports and desktop access without input; prompt optionally requests macOS AX."""
    try:
        if sys.platform == "darwin":
            import Quartz
            import AppKit  # noqa: F401
            if not _accessibility_trusted(prompt):
                return False, ("macOS Accessibility permission is required for the controlling "
                               "terminal/app in System Settings > Privacy & Security > Accessibility; "
                               "without it CGEvent input is silently dropped")
            if Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState) is None:
                return False, "macOS has no accessible HID event source"
            return True, "macOS Quartz HID input available"
        if sys.platform == "win32":
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.OpenInputDesktop.restype = ctypes.c_void_p
            desktop = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
            if not desktop:
                return False, "Windows input desktop is inaccessible (locked or noninteractive session)"
            user32.CloseDesktop.argtypes = [ctypes.c_void_p]
            user32.CloseDesktop(desktop)
            return True, "Windows SendInput available; backend not yet field-tested; UIPI may block elevated apps"
        if sys.platform.startswith("linux"):
            if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE") == "wayland":
                return False, "native Wayland input is unsupported; use an X11 desktop session"
            if not os.environ.get("DISPLAY"):
                return False, "Linux OS input requires an accessible X11 DISPLAY"
            if shutil.which("xdotool"):
                result = subprocess.run(["xdotool", "getmouselocation"], capture_output=True, timeout=3)
                if result.returncode == 0:
                    return True, "Linux xdotool available; backend not yet field-tested"
            from Xlib import display
            with contextlib.closing(display.Display()) as conn:
                if not conn.has_extension("XTEST"):
                    return False, "X11 server does not permit XTEST input"
            return True, "Linux python-xlib XTEST available; backend not yet field-tested"
        return False, f"OS cursor input is unsupported on {sys.platform}"
    except Exception as exc:
        return False, f"{sys.platform} OS input unavailable ({type(exc).__name__}); install the os-cursor extra and enable desktop permissions"


@contextlib.contextmanager
def _windows_dpi(aware: bool):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    old = user32.SetThreadDpiAwarenessContext(-4 if aware else -1)
    if not old:
        raise OSError("cannot set Windows thread DPI context")
    try:
        yield user32
    finally:
        user32.SetThreadDpiAwarenessContext(old)


def _windows_physical_point(x: float, y: float) -> tuple[int, int]:
    from ctypes import wintypes
    with _windows_dpi(False) as user32:
        point = wintypes.POINT(round(x), round(y))
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        hwnd = user32.WindowFromPoint(point)
        user32.LogicalToPhysicalPointForPerMonitorDPI.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        if not hwnd or not user32.LogicalToPhysicalPointForPerMonitorDPI(hwnd, ctypes.byref(point)):
            raise OSError("cannot map logical screen point to physical pixels")
        return point.x, point.y


def _windows_mouse(flags: int, x: float, y: float) -> None:
    from ctypes import wintypes

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KeyInput(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HardwareInput(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class InputUnion(ctypes.Union):
        _fields_ = [("mi", MouseInput), ("ki", KeyInput), ("hi", HardwareInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", InputUnion)]

    px, py = _windows_physical_point(x, y)
    with _windows_dpi(True) as user32:
        left, top, width, height = (user32.GetSystemMetrics(n) for n in (76, 77, 78, 79))
        dx = round((px - left) * 65535 / max(1, width - 1))
        dy = round((py - top) * 65535 / max(1, height - 1))
        event = Input(0, InputUnion(mi=MouseInput(dx, dy, 0, flags | 0x8000 | 0x4000, 0, 0)))
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        if user32.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
            raise OSError("SendInput was refused (check desktop access and application integrity level)")


def move_and_click(x: float, y: float, approach_from: tuple[float, float] | None = None,
                   hold_ms: float = 60, steps: int = 40) -> None:
    """Approach from ~300 points away, then press/hold/release the real left button."""
    if not all(math.isfinite(n) for n in (x, y, hold_ms)) or hold_ms < 0 or steps < 2:
        raise ValueError("coordinates and hold_ms must be finite, hold_ms >= 0 and steps >= 2")
    available, reason = is_available()
    if not available:
        raise RuntimeError(reason)
    fx, fy = approach_from if approach_from is not None else (x + 300, y)
    if not all(math.isfinite(n) for n in (fx, fy)) or math.hypot(fx - x, fy - y) < 50:
        raise ValueError("approach_from must start at least 50 screen points from the target")
    conn = None
    if sys.platform == "darwin":
        import Quartz
        source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)

        def post(kind, px, py):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, Quartz.CGEventCreateMouseEvent(
                source, kind, (px, py), Quartz.kCGMouseButtonLeft))

        move, down, up = Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp
    elif sys.platform == "win32":
        post = _windows_mouse
        move, down, up = 0x0001, 0x0002, 0x0004
    elif shutil.which("xdotool"):
        def post(kind, px, py):
            args = ["mousemove", str(round(px)), str(round(py))] if kind == "move" else [kind, "1"]
            subprocess.run(["xdotool", *args], check=True, capture_output=True, timeout=3)
        move, down, up = "move", "mousedown", "mouseup"
    else:
        from Xlib import X, display
        from Xlib.ext import xtest
        conn = display.Display()

        def post(kind, px, py):
            if kind == X.MotionNotify:
                xtest.fake_input(conn, kind, x=round(px), y=round(py))
            else:
                xtest.fake_input(conn, kind, detail=1)
            conn.sync()
        move, down, up = X.MotionNotify, X.ButtonPress, X.ButtonRelease
    try:
        post(move, fx, fy)
        lo = 1 / (1 + math.exp(6))
        for index in range(1, steps + 1):
            eased = (1 / (1 + math.exp(-12 * (index / steps - 0.5))) - lo) / (1 - 2 * lo)
            post(move, fx + (x - fx) * eased, fy + (y - fy) * eased)
            time.sleep(0.012)
        # A human settles on the target before pressing. Clicking the instant the
        # pointer arrives reads as synthetic to a behavioural challenge like Turnstile.
        time.sleep(0.15)
        try:
            post(down, x, y)
            time.sleep(hold_ms / 1000)
        finally:
            post(up, x, y)
    finally:
        if conn is not None:
            conn.close()


def _windows_window(pid: int):
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    @callback_type
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    if not found:
        raise RuntimeError("sidecar has no visible Windows window")
    return found[0]


def _x11_window(conn, pid: int):
    root = conn.screen().root
    clients = root.get_full_property(conn.intern_atom("_NET_CLIENT_LIST"), 0)
    if clients is not None:
        for wid in clients.value:
            window = conn.create_resource_object("window", int(wid))
            prop = window.get_full_property(conn.intern_atom("_NET_WM_PID"), 0)
            if prop is not None and int(prop.value[0]) == pid:
                return window
    raise RuntimeError("sidecar has no discoverable X11 window (_NET_WM_PID)")


def bring_app_frontmost(pid: int) -> None:
    """Activate only this process's app. Refuse when the window manager denies it."""
    if sys.platform == "darwin":
        from AppKit import NSRunningApplication
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None or not app.activateWithOptions_(1 << 1):
            raise RuntimeError("could not activate the sidecar application")
        time.sleep(0.2)
        if not app.isActive():
            raise RuntimeError("sidecar application is not frontmost")
    elif sys.platform == "win32":
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        hwnd = _windows_window(pid)
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        if not user32.SetForegroundWindow(hwnd):
            raise RuntimeError("Windows refused to bring the sidecar to the foreground")
    elif sys.platform.startswith("linux"):
        if shutil.which("xdotool"):
            result = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(pid)],
                                    check=True, capture_output=True, text=True, timeout=3)
            wid = result.stdout.splitlines()[0]
            subprocess.run(["xdotool", "windowactivate", "--sync", wid],
                           check=True, capture_output=True, timeout=3)
        else:
            from Xlib import X, display, protocol
            with contextlib.closing(display.Display()) as conn:
                window = _x11_window(conn, pid)
                root = conn.screen().root
                root.send_event(protocol.event.ClientMessage(
                    window=window, client_type=conn.intern_atom("_NET_ACTIVE_WINDOW"),
                    data=(32, [2, X.CurrentTime, 0, 0, 0])),
                    event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
                conn.sync()
                time.sleep(0.2)
                active = root.get_full_property(conn.intern_atom("_NET_ACTIVE_WINDOW"), 0)
                if active is None or int(active.value[0]) != window.id:
                    raise RuntimeError("X11 window manager refused sidecar activation")
    else:
        raise RuntimeError(f"unsupported platform: {sys.platform}")


def window_bounds(pid: int) -> tuple[int, int, int, int]:
    """Actual visible window bounds in screen points, measured without a CDP client."""
    if sys.platform == "darwin":
        import Quartz
        windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, 0)
        for window in windows:
            if window.get("kCGWindowOwnerPID") == pid and window.get("kCGWindowLayer") == 0:
                bounds = window["kCGWindowBounds"]
                if bounds["Width"] > 200 and bounds["Height"] > 200:
                    return tuple(round(bounds[key]) for key in ("X", "Y", "Width", "Height"))
        raise RuntimeError("sidecar has no visible macOS window")
    if sys.platform == "win32":
        from ctypes import wintypes
        with _windows_dpi(False) as user32:
            rect = wintypes.RECT()
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            if not user32.GetWindowRect(_windows_window(pid), ctypes.byref(rect)):
                raise OSError("could not measure the sidecar window")
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    if shutil.which("xdotool"):
        result = subprocess.run(["xdotool", "search", "--onlyvisible", "--pid", str(pid)],
                                check=True, capture_output=True, text=True, timeout=3)
        wid = result.stdout.splitlines()[0]
        result = subprocess.run(["xdotool", "getwindowgeometry", "--shell", wid],
                                check=True, capture_output=True, text=True, timeout=3)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return tuple(int(fields[key]) for key in ("X", "Y", "WIDTH", "HEIGHT"))
    from Xlib import display
    with contextlib.closing(display.Display()) as conn:
        window = _x11_window(conn, pid)
        geometry = window.get_geometry()
        position = conn.screen().root.translate_coords(window, 0, 0)
        return position.x, position.y, geometry.width, geometry.height


def capture_region(bounds: tuple[int, int, int, int], path: Path) -> None:
    """Capture the desktop, not the browser. Caller owns the private output directory."""
    left, top, width, height = bounds
    if sys.platform == "darwin":
        import Quartz
        if hasattr(Quartz, "CGPreflightScreenCaptureAccess") and not Quartz.CGPreflightScreenCaptureAccess():
            raise RuntimeError("macOS Screen Recording permission is required for the controlling terminal/app")
        subprocess.run(["/usr/sbin/screencapture", "-x", "-R", f"{left},{top},{width},{height}", str(path)],
                       check=True, capture_output=True, timeout=5)
    else:
        import mss
        import mss.tools
        if sys.platform == "win32":
            x2, y2 = _windows_physical_point(left + width - 1, top + height - 1)
            left, top = _windows_physical_point(left, top)
            width, height = x2 - left + 1, y2 - top + 1
        with mss.mss() as capture:
            image = capture.grab({"left": left, "top": top, "width": width, "height": height})
            mss.tools.to_png(image.rgb, image.size, output=str(path))
    path.chmod(0o600)
