#!/usr/bin/env python3
"""The agent overlay: who holds a headed browser the agent drives, over its window (macOS).

Started by presence.py with this source on stdin (`python -I - mcp-stealth-agent-overlay <json>`),
so nothing of the installed package is read after it starts. Standard library and PyObjC only.

Over the browser's window: a moving glow while the agent acts, a bar saying what it does, and,
while it acts, a lock over the page, so a stray click or scroll never reaches it. Only the bar's
button hands the browser to the person, and only the bar hands it back. The agent's cursor can
be shown from the bar (off by default). Nothing is drawn inside the page.

No blinking: nothing is taken off the screen and put back to change it. The bar keeps one size
and its text cross-fades; the glow dims between steps instead of switching off; the browser
window must stay missing for a moment before anything hides; the window followed is kept even
when the browser opens a popup; and while nothing covers the browser the overlay sits in the
floating level, so the browser coming to the front cannot cover it for a frame. An app switch is
handled when it happens, not at the next tick.

Cheap, measured against no overlay at all (WindowServer +10 points instead of +35, this process
3% CPU instead of 8 to 9%):
- The glow lives in four thin windows along the edges, so only those strips change each frame;
  it is one turning gradient under a nine-slice mask drawn once (no live blur), asked for at
  30 fps (displays run at up to 120: about +10 points against +5 at 20 fps, whose 9 s turn
  stepped visibly, and +19 at 60), turning once in 16 s, and stopped once it has faded out.
- The agent's cursor and click rings are small windows of their own, on screen only while there
  is something to show. A full-window layer for them cost WindowServer as much as the rest.
- The bar has a plain tinted background: a live blur over moving light is redrawn every frame.
  The lock is fully transparent and still takes every click (ignoresMouseEvents set to False).
- Reading the window list costs more in the Python bridge than in macOS, so it reads only the
  windows above the browser's, four times a second and at every app switch, converted once to
  plain tuples. The browser window's own bounds are read every tick only while it moves.
- WindowServer's cost follows the number of distinct frames more than their size, so the overlay
  updates 20 times a second (10 with nothing moving).
- Pausing the light while other windows cover the browser was measured: no difference
  (WindowServer already skips windows nobody can see), so it is not done.

Files, in the folder named by the config:
  agent.json   written by the server: pid, active_at, status, waiting, help, cursor (page
               coordinates), click_at, viewport ([left, top, width, height] of the page inside
               the window), window ([left, top, width, height] on screen), ended
  control.json written here: holder (agent|person), since, reason, person_last_input, help_done,
               show_cursor
SIGUSR1 toggles the cursor and SIGUSR2 presses the bar's button, for tests.
"""
import json
import math
import os
import shutil
import signal
import stat
import sys
import threading
import time
import traceback

import objc
from AppKit import (NSApplication, NSAttributedString, NSButton, NSColor, NSEvent, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSImage,
                    NSImageSymbolConfiguration, NSMakeRect, NSMenu, NSMenuItem,
                    NSMutableAttributedString,
                    NSMutableParagraphStyle, NSPanel, NSParagraphStyleAttributeName, NSScreen,
                    NSTextField, NSTrackingArea, NSView, NSWorkspace,
                    NSWorkspaceActiveSpaceDidChangeNotification,
                    NSWorkspaceDidActivateApplicationNotification)
from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from Quartz import (CAAnimationGroup, CABasicAnimation, CACurrentMediaTime, CAGradientLayer,
                    CALayer, CAMediaTimingFunction, CAShapeLayer, CATextLayer, CATransaction,
                    CATransition, CGBitmapContextCreate, CGBitmapContextCreateImage,
                    CGColorSpaceCreateDeviceRGB, CGContextAddPath,
                    CGContextEOFillPath, CGContextScaleCTM,
                    CGContextSetRGBFillColor,
                    CGEventSourceSecondsSinceLastEventType,
                    CGPathAddLineToPoint,
                    CGPathCloseSubpath, CGPathCreateMutable, CGPathCreateWithEllipseInRect,
                    CGPathMoveToPoint, CGRectMake,
                    CGWindowListCopyWindowInfo, kCAMediaTimingFunctionEaseInEaseOut,
                    kCATransitionFade, kCGEventKeyDown,
                    kCGEventLeftMouseDown, kCGEventScrollWheel,
                    kCGEventSourceStateHIDSystemState,
                    kCGImageAlphaPremultipliedLast, kCGNullWindowID,
                    kCGWindowListExcludeDesktopElements, kCGWindowListOptionIncludingWindow,
                    kCGWindowListOptionOnScreenAboveWindow, kCGWindowListOptionOnScreenOnly)

try:
    from Quartz import kCAGradientLayerConic
except ImportError:  # older PyObjC
    kCAGradientLayerConic = "conic"
try:
    from Quartz import CAFrameRateRangeMake
    RATE = CAFrameRateRangeMake(24.0, 30.0, 30.0)  # a slow glow needs no 60 or 120 fps
    RING_RATE = CAFrameRateRangeMake(15.0, 30.0, 30.0)
except ImportError:
    RATE = RING_RATE = None

BORDERLESS, NONACTIVATING, BUFFERED = 0, 1 << 7, 2
ABOVE = 1
NORMAL_LEVEL, FLOATING_LEVEL = 0, 3
ALL_SPACES, IGNORES_CYCLE, FULLSCREEN_AUX = 1 << 0, 1 << 6, 1 << 8
# NSWindowCollectionBehaviorTransient: hidden while Mission Control shows. Without it Mission Control
# drew the light's edge windows as windows of their own, left where they were, apart from the browser
# window it had moved (the person saw the top, bottom and left strips stuck on the desktop).
TRANSIENT = 1 << 3
TRACK = 0x01 | 0x80 | 0x200  # entered/exited, always, in visible rect
IMAGE_ONLY = 1
TRUNCATE_TAIL = 4
RADIUS = 12.0       # the bar's corners


def corner_shape():
    """(box, n): the window corner the light follows, a superellipse |x|^n + |y|^n = 1 over a
    box of `box` points (n=2 is a circle of that radius). macOS 26 draws windows with a continuous
    corner: on 26.6.1 Chrome's and Brave Beta's fit n=4 over 37 pt within 0.31 pt rms, where the
    best circle (21 pt) was 0.86 pt off and the old 12 pt one far more, so the light's corners
    stood off the window's. Earlier macOS: a 10 pt circle. MCP_STEALTH_OVERLAY_CORNER overrides
    with "<box>" or "<box>,<n>"."""
    given = os.environ.get("MCP_STEALTH_OVERLAY_CORNER", "").strip()
    if given:
        try:
            parts = [float(v) for v in given.split(",")]
            box, n = parts[0], (parts[1] if len(parts) > 1 else 4.0)
            if 0 <= box <= 200 and 1.0 <= n <= 12.0:
                return box, n
        except ValueError:
            pass
    try:
        import platform
        major = int((platform.mac_ver()[0] or "0").split(".")[0])
    except (ValueError, IndexError):
        major = 0
    return (37.0, 4.0) if major >= 26 else (10.0, 2.0)


CORNER = corner_shape()
TURN = 16.0         # seconds per turn of the light: at 30 fps a corner moves about 9 points a
                    # frame (9 s at 20 fps moved it 25, and the soft colours stepped visibly)
DEPTH = 64.0        # how far the light reaches into the window (34 read as too faint, 20 Nov 2026)
THIN = 32.0         # and at its thinnest: it breathes between the two (asked for, 21 Nov 2026)
BREATHE = 4.0       # seconds from thick to thin, and as long back
STRIP = DEPTH + 8   # thickness of the edge windows that carry it: the light is 0 past DEPTH
SWITCH_SETTLE = 0.3  # after another app comes to the front, stay out of the floating level this long
RAISE_FOR = 0.5     # after the browser comes to the front, put the overlay back over it on every tick
TICK = 1 / 20       # the glow's own pace: every update lands on a frame drawn anyway
TICK_MOVING = 1 / 60  # while the browser window moves: at 20 a second the light trailed a drag
CALM_EVERY = 2      # with nothing moving, do a tick's work every 2nd tick
SCAN_EVERY = 0.25   # seconds between reads of the windows above the browser's
MOVING_FOR = 1.0    # after the browser window moved, follow it at the full rate this long
QUIET_AFTER = 2.5   # no word from the agent for this long: it is thinking, the glow dims
MISSING_FOR = 0.6   # the browser window must be gone this long before anything hides
MISSING_QUICK = 0.1  # the same, when no Space changed: minimized or hidden, not moving Spaces
SPACE_GRACE = 1.5   # a Space change this recent may still be hiding the window
NUDGE_FOR = 3.0     # how long the bar explains a click or key that did not take over
WORKING = ("agent", "polite", "thinking")

CONFIG = json.loads(sys.argv[-1])
STATE = CONFIG["state"]
AGENT = os.path.join(STATE, "agent.json")
CONTROL = os.path.join(STATE, "control.json")
SERVER = int(CONFIG["server"])
HIDE_AFTER = float(CONFIG.get("hide_after", 90.0))  # no word for this long: everything fades out
# No tool call for this long: the agent is probably done (its steps came at most 14.7 s apart in a
# measured run, median 4.3 s). The lock goes, so the person can use the page without Take over,
# and what they do there is passed to the agent's next call.
IDLE_AFTER = float(CONFIG.get("idle_after", 20.0))
STYLE = CONFIG.get("style") or {}  # presence.style(): the person's choice of bar, light and lock
BAR_MODES = ("full", "compact", "hover", "off")
BAR_MODE = STYLE.get("bar", "full") if STYLE.get("bar") in BAR_MODES else "full"
LIGHT_ON = STYLE.get("light", True) is not False
LOCK_SETTING = STYLE.get("lock", True) is not False  # the person's choice; off anyway with no bar
LOCK_ON = LOCK_SETTING and BAR_MODE != "off"
# The settings file (settings.py) is read again while this runs, so a choice from the bar's menu,
# from `mcp-stealth-chrome settings` or from another overlay applies at once. A setting the
# server's environment fixed (`fixed`) wins over the file here too, and its menu item is disabled.
SETTINGS_FILE = CONFIG.get("settings_file") or ""
FIXED = CONFIG.get("fixed") or {}
SETTINGS_EVERY = 1.0
HOVER_LINGER = 1.2  # hover: the bar stays this long after the pointer left the window
ME = os.getpid()


def hexc(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a)


# Indigo, blue, cyan and violet: calm, and the same family as the button.
AGENT_GLOW = [hexc("#4F46E5"), hexc("#2563EB"), hexc("#06B6D4"), hexc("#8B5CF6"), hexc("#4F46E5")]
HELP_GLOW = [hexc("#F59E0B"), hexc("#F97316"), hexc("#FBBF24"), hexc("#F59E0B")]
DOT = {"agent": hexc("#3B82F6"), "thinking": hexc("#3B82F6"), "polite": hexc("#94A3B8"),
       "person": hexc("#22C55E"), "help": hexc("#F59E0B"), "resting": hexc("#64748B")}
BUTTON = {  # base, hover, pressed
    "primary": (hexc("#2563EB"), hexc("#1D4ED8"), hexc("#1E40AF")),
    "on": (hexc("#4F46E5"), hexc("#4338CA"), hexc("#3730A3")),
    "quiet": ((1, 1, 1, 0.14), (1, 1, 1, 0.22), (1, 1, 1, 0.30)),
}
WHITE = (1, 1, 1, 1.0)
MUTED = (1, 1, 1, 0.66)
SYMBOLS = NSImage.imageWithSystemSymbolName_accessibilityDescription_("cursorarrow", None) is not None


def cg(c):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*c).CGColor()


def ns(c):
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*c)


def read(path):
    """A state file, never through a link and only a regular file; {} when it cannot be read."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return {}
        with os.fdopen(fd) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def chrome_start(pid):
    """Start time of the process on this PID as ps prints it (lstart, UTC, C locale): the format
    the server recorded. None when nothing runs there, "" when ps cannot answer."""
    try:
        import subprocess
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except Exception:
        return ""
    text = " ".join(out.stdout.split())
    return text or (None if out.returncode == 1 else "")


def leave(remove_files):
    """End this process. When the browser or the server is gone, nobody else removes the files."""
    if remove_files:
        shutil.rmtree(STATE, ignore_errors=True)
    NSApplication.sharedApplication().terminate_(None)


def write(path, data):
    tmp = f"{path}.{ME}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read_settings():
    """The settings file as a dict, {} when missing, a link, not ours or not a JSON object."""
    if not SETTINGS_FILE:
        return {}
    try:
        fd = os.open(SETTINGS_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return {}
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)
    return data if isinstance(data, dict) else {}


def write_settings(changes):
    """Merge into the settings file as settings.write does: a fresh 0600 file, then a rename."""
    if not SETTINGS_FILE:
        return
    folder = os.path.dirname(SETTINGS_FILE)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    if os.path.islink(SETTINGS_FILE):
        return
    import fcntl
    lock = os.open(os.path.join(folder, f".{os.path.basename(SETTINGS_FILE)}.lock"),
                   os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)  # settings.write takes the same: no choice is lost
        _merge_settings(changes, folder)
    finally:
        os.close(lock)


def _merge_settings(changes, folder):
    data = read_settings()
    data.update(changes)
    tmp = os.path.join(folder, f".settings.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, SETTINGS_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _flag(value, default):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text in ("1", "true", "yes", "on") else False if text in ("0", "false", "no", "off") else default


def chosen_style(data):
    """(bar, light, lock setting) from the environment's fixed values, then the file, then the
    defaults: settings.resolve's order."""
    bar = FIXED.get("overlay_bar", data.get("overlay_bar", "full"))
    bar = str(bar).strip().lower() if str(bar).strip().lower() in BAR_MODES else "full"
    light = _flag(FIXED.get("overlay_light", data.get("overlay_light", True)), True)
    lock = _flag(FIXED.get("overlay_lock", data.get("overlay_lock", True)), True)
    return bar, light, lock


def screen_height():
    return NSScreen.screens()[0].frame().size.height


def full_screen(x, y, w, h, state=None, others=None):
    """Whether a window (global coordinates, top-left origin) is in full screen: its corners are
    square then, and the light's must be too. A full-screen Chrome is several windows on macOS:
    the content one starts below the toolbar strip (measured: 0,121 1728x996 on a 1728x1117
    screen), so the browser's own word counts (`state`, from CDP's windowState, read before each
    tool call), taken only while the window still spans its screen's width, so a word that went
    stale after the person left full screen never squares a normal window."""
    H = screen_height()
    for screen in NSScreen.screens():
        f = screen.frame()
        top = H - (f.origin.y + f.size.height)
        spans = abs(x - f.origin.x) < 1 and abs(w - f.size.width) < 1
        if spans and abs(y - top) < 1 and abs(h - f.size.height) < 1:
            return True
        if spans and state == "fullscreen":
            return True
        if spans and others is not None and y > top and abs((y + h) - (top + f.size.height)) < 1:
            # Down to the screen's bottom, and the browser's own window above it from the top of
            # the screen down to it: the tab strip and toolbar of a full-screen browser (a normal
            # window is one window). Without waiting for the server's word, which came 0.6 to
            # 0.8 s after the window had settled.
            for ox, oy, ow, oh in others():
                if abs(ox - x) < 1 and abs(ow - w) < 1 and oy <= top + 40 and oy + oh >= y - 2:
                    return True
    return False


def screen_frame_of(x, y, w, h):
    """The part of the screen holding a window's centre that a full-screen app draws in (global,
    top-left origin), or None: the whole screen less the camera housing's band on a display with
    one (its safe area, 33 pt on a 14-inch MacBook Pro), where macOS keeps full screen black."""
    H = screen_height()
    cx, cy = x + w / 2, y + h / 2
    for screen in NSScreen.screens():
        f = screen.frame()
        top = H - (f.origin.y + f.size.height)
        if f.origin.x <= cx < f.origin.x + f.size.width and top <= cy < top + f.size.height:
            band = 0.0
            if hasattr(screen, "safeAreaInsets"):
                try:
                    band = float(screen.safeAreaInsets().top)
                except Exception:
                    band = 0.0
            return (f.origin.x, top + band, f.size.width, f.size.height - band)
    return None


def entries(infos):
    """Window dictionaries as plain tuples, front to back: (number, pid, normal, bounds, onscreen).
    Each value read from a dictionary goes through the bridge, so read each once, and only the
    layer of a window that is not an ordinary one."""
    out = []
    for w in infos or ():
        layer = w.get("kCGWindowLayer")
        number, pid = int(w["kCGWindowNumber"]), int(w.get("kCGWindowOwnerPID", 0))
        if layer != 0:
            out.append((number, pid, False, None, True))
            continue
        b = w["kCGWindowBounds"]
        out.append((number, pid, float(w.get("kCGWindowAlpha", 1)) > 0,
                    (float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"])),
                    bool(w.get("kCGWindowIsOnscreen", True))))
    return out


def all_windows():
    return entries(CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements, kCGNullWindowID))


def above_and_including(wid):
    return entries(CGWindowListCopyWindowInfo(
        kCGWindowListOptionOnScreenAboveWindow | kCGWindowListOptionIncludingWindow, wid))


def one_window(wid):
    return entries(CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, wid))


def window_alpha(wid):
    """The window's alpha, or None when it no longer exists. A window being closed stays listed
    while macOS fades and shrinks it (about 0.3 s), its alpha falling."""
    infos = CGWindowListCopyWindowInfo(kCGWindowListOptionIncludingWindow, wid) or ()
    for w in infos:
        if int(w["kCGWindowNumber"]) == wid:
            return float(w.get("kCGWindowAlpha", 1))
    return None


def meets(a, b):
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


def inside(a, b):
    return a[0] >= b[0] and a[1] >= b[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]


def insets(window, viewport):
    """The page's distance from each edge of its window (left, top, right, bottom), from the
    server's last measurement (CDP window bounds and the page's place in them), or None."""
    if not (isinstance(window, list) and isinstance(viewport, list) and len(window) == 4 and len(viewport) == 4):
        return None
    try:
        width, height = float(window[2]), float(window[3])
        left, top, vw, vh = (float(v) for v in viewport)
    except (TypeError, ValueError):
        return None
    return (max(0.0, left), max(0.0, top), max(0.0, width - left - vw), max(0.0, height - top - vh))


def near(b, want, slack=4.0):
    return all(abs(float(p) - float(q)) <= slack for p, q in zip(b, want))


def choose_mode(help_text, holder, quiet, waiting, answered=False):
    """help and the person's hold first: their buttons are the only way back, so they outlast any
    quiet. A request the person answered with Done is `noted` until the agent's next call takes
    it: the bar kept asking for help after Done (measured). Then, by the seconds since the agent's
    last call (0 while one runs): idle past HIDE_AFTER (all faded), resting past IDLE_AFTER (the
    lock is off, the page is the person's), thinking past QUIET_AFTER, else working."""
    if help_text and not answered:
        return "help"
    if holder == "person":
        return "person"
    if quiet > HIDE_AFTER:
        return "idle"
    if waiting == "polite":
        return "polite"
    if help_text:
        return "noted"
    if quiet > IDLE_AFTER:
        return "resting"
    if quiet > QUIET_AFTER:
        return "thinking"
    return "agent"


def told(agent, control):
    """What the agent knows of the person's Take over: they see whether it noticed."""
    since = float(control.get("since") or 0)
    if agent.get("waiting_human"):
        return "The agent is waiting for you"
    if float(agent.get("outside_at") or 0) >= since:
        return f"The agent went on without the browser ({agent.get('outside_tool') or 'a tool'})"
    told_at = float(agent.get("told_at") or 0)
    if told_at >= since:
        return "The agent was told at " + time.strftime("%H:%M:%S", time.localtime(told_at))
    return "The agent learns it at its next step"


def contains(b, x, y):
    return b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]


def wants_floating(covered_now, browser_front, since_switch):
    """The floating level keeps the browser, brought to the front by a click, from covering the
    overlay for a frame. It also holds the overlay over whatever app comes to the front next,
    until a scan sees it: 163 and 179 ms of light over the app a Cmd+Tab went to, measured. So
    float only while that cannot happen: nothing covers the browser, another app is in front
    (with the browser in front, sitting just above its window looks the same, and the next app
    covers both at once), and no app has just come to the front, which the window list may not
    show yet. A rule that also left the floating level while Command was held (0.25.14) dropped
    it during a Cmd+Tab TO the browser too, which then covered the overlay until a scan: the
    person saw the tab first and the overlay late."""
    return not covered_now and not browser_front and since_switch >= SWITCH_SETTLE


def directly_above(wins, ours, target):
    """True when our windows sit in front of `target` with no other app's window between."""
    seen = False
    for number, _pid, ordinary, _b, _on in wins:
        if number in ours:
            seen = True
        elif number == target:
            return seen
        elif seen and ordinary:
            return False
    return False


def covered(wins, target, rect, pid):
    """Does any other window lie over the browser's window? The browser's own popups and bubbles
    (inside its window) do not count."""
    for number, owner, ordinary, b, _on in wins:  # front to back
        if number == target:
            return False
        if owner == ME or not ordinary:
            continue
        if owner == pid and inside(b, rect):
            continue
        if meets(b, rect):
            return True
    return False


def visible_at(wins, target, x, y):
    """Is the browser's window the one seen at this point?"""
    for number, owner, ordinary, b, _on in wins:
        if number == target:
            return contains(b, x, y)
        if owner != ME and ordinary and contains(b, x, y):
            return False
    return False


# The decisions of Overlay.tick, as plain functions of what it read.

def skip_tick(ticks, force, animating):
    """With nothing moving (no cursor shown, no click ring, the window not just moved) only every
    CALM_EVERY-th tick does its work: 10 a second are enough then. A forced tick always does."""
    return not animating and not force and ticks % CALM_EVERY != 0


def closing_over(alpha):
    """A window seen closing is followed again only once it is gone (None) or back at full alpha
    (one that dipped for another reason)."""
    return alpha is None or alpha >= 0.95


def window_going(alpha):
    """The browser's window is gone (None: the browser quit) or fading out as macOS closes it
    (under 0.5; about 0.3 s, still listed meanwhile): the overlay goes with it at once."""
    return alpha is None or alpha < 0.5


def window_read(pid, scan, moving, have_frame, missing):
    """How a tick finds the browser's window: "search" (Overlay.browser_window: the window
    followed, the windows above it on a scan, every window when none is followed), "followed"
    (that window's own bounds alone: 0.12 ms against 0.47 for every window on screen), or None.
    None with no browser, and between scans once a scan missed the window: cached bounds must
    not stand in then, or every tick reset the missing time and MISSING_FOR never ran out (after
    a Quit the light and the lock, which takes clicks, stayed 3.35 s)."""
    if not pid:
        return None
    if scan or moving or not have_frame:
        return "search"
    return None if missing else "followed"


def missing_for(since_space_change):
    """How long the window must stay missing before the light goes. MISSING_FOR keeps a Space
    change or a full-screen move (a Space of its own) from blinking the overlay; with no Space
    change around, a window gone from the screen was minimized or hidden: MISSING_QUICK."""
    return MISSING_FOR if since_space_change < SPACE_GRACE else MISSING_QUICK


def checks_start(start, missing, since_check, probing):
    """Read the start time of the browser's PID now? A window missing for 3 s on a PID that runs:
    that PID may now be another process's. At most every 5 s, one check at a time."""
    return bool(start) and missing > 3 and since_check > 5 and not probing


def pid_reused(current, start):
    """The browser's PID runs nothing now (None) or another process (another start time). ""
    is ps that could not answer, which proves nothing."""
    return current is None or bool(current and current != start)


def happened(since, before):
    """A key, click or scroll since the last look: the HID system's seconds since the last event
    of that type fell, as it starts again from 0 at each one."""
    return since + 0.005 < before


def quiet_for(agent, now):
    """Seconds since the agent's last call. A tool call still running is not quiet, however long
    it takes (a long wait_for): the server marks it busy from before the call to after it."""
    return 0.0 if agent.get("busy") else now - float(agent.get("active_at", 0))


def help_answered(agent, control):
    """The person pressed Done on the request for help shown now (help_done at or after its
    asked_at). With no request asked, nothing is answered."""
    return float(control.get("help_done") or 0) >= float(agent.get("asked_at") or 0) > 0


def used_now(clicked, scrolled, typed):
    """What the person did on this tick, in the words the agent's next result uses."""
    return [word for word, did in (("clicked", clicked), ("scrolled", scrolled), ("typed", typed)) if did]


def used_since_agent(control, agent, used):
    """What the person did since the agent's last call, in a fixed order: what was recorded
    before and what they did now. Recorded before the agent's last call: the agent has acted
    since, so it starts over."""
    before = control.get("person_used") or []
    if float(control.get("person_used_at") or 0) <= float(agent.get("active_at") or 0):
        before = []  # the agent has acted since: start over
    return [w for w in ("clicked", "scrolled", "typed") if w in before or w in used]


def light_level(mode, light_on):
    """The light: on while the agent acts or asks for help, dimmed while it thinks or waits for
    the person's typing to stop, out otherwise, and out when the person turned it off."""
    if not light_on:
        return "off"
    return "on" if mode in ("agent", "help") else "dim" if mode in WORKING else "off"


def cursor_point(cursor, page, zoom):
    """The agent's cursor on screen (global, top-left origin), or None without a cursor or a page,
    or when the point lies outside the page (its edges count as inside). The cursor is in CSS
    pixels, which the page's zoom scales. The point is the agent's last one: after
    set_viewport_size(390, 900) a click made at x 1227 on the desktop layout was drawn 724 pt
    outside the window, over another app (measured)."""
    if not (cursor and page):
        return None
    try:
        zoom = float(zoom or 1.0)
    except (TypeError, ValueError):
        zoom = 1.0
    x, y = page[0] + cursor[0] * zoom, page[1] + cursor[1] * zoom
    if not (page[0] <= x <= page[0] + page[2] and page[1] <= y <= page[1] + page[3]):
        return None
    return x, y


def bar_words(mode, agent, control, over, nudging, why):
    """What the bar says: (first words, the rest, its button, the button's kind, the dot as a key
    of DOT, whether the dot pulses), or None while idle (the bar keeps its words as it fades).
    A click, scroll or key that did not take over is explained first; with the pointer over the
    browser while the agent works, the bar says how to take over."""
    status = agent.get("status") or "Using the browser"
    if mode in WORKING and nudging:
        first = ("Typing does not take over" if why == "type"
                 else "The page is locked while the agent works")
        return first, "  ·  Press Take over to use it", "Take over", "primary", mode, mode == "agent"
    if mode in ("agent", "thinking"):
        hint = ("  ·  Press Take over to use the browser" if over
                else "  ·  Thinking about the next step" if mode == "thinking"
                else f"  ·  {status}")
        return "Agent working", hint, "Take over", "quiet", mode, mode == "agent"
    if mode == "polite":
        return "Agent paused", "  ·  It goes on in a moment", "Take over", "quiet", "polite", False
    if mode == "noted":
        return "Done", "  ·  The agent goes on at its next step", "Take over", "quiet", "person", False
    if mode == "resting":
        return ("Agent idle", "  ·  The page is yours until its next step", "Take over", "quiet",
                "resting", False)
    if mode == "person":
        return ("You have the browser", f"  ·  {told(agent, control)}", "Resume agent", "primary",
                "person", False)
    if mode == "help":
        return "Your help is needed", f"  ·  {agent.get('help')}", "Done", "primary", "help", True
    return None


def wants_lock(lock_on, mode, page):
    """The lock over the page: only while the agent works, and only where the page is known. The
    person's hold, help, an idle or resting agent leave the page to the person."""
    return lock_on and mode in WORKING and bool(page)


def wants_bar(mode, bar_mode, nudging, hovered):
    """Whether the bar shows. Help, the person's hold and a nudge always show it: their buttons
    are the way back. Otherwise never while idle; "off" not at all; "hover" while the pointer is
    over the browser or the bar (and HOVER_LINGER after)."""
    must_show = mode in ("help", "person") or nudging
    if bar_mode == "off":
        return must_show
    if bar_mode == "hover":
        return mode != "idle" and (must_show or hovered)
    return mode != "idle"


class Panel(NSPanel):
    """A panel that stays where it is put. AppKit moves a window out of the menu bar's band when
    it is ordered in: in full screen the light's top edge was pushed 33 pt down, off the tab
    strip it should outline."""

    def constrainFrameRect_toScreen_(self, rect, _screen):
        return rect


def panel(frame, takes_clicks, shadow=False):
    p = Panel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, BORDERLESS | NONACTIVATING, BUFFERED, False)
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setHasShadow_(shadow)
    p.setIgnoresMouseEvents_(not takes_clicks)  # set explicitly: transparent areas then count too
    p.setHidesOnDeactivate_(False)
    p.setBecomesKeyOnlyIfNeeded_(True)
    p.setCollectionBehavior_(ALL_SPACES | TRANSIENT | IGNORES_CYCLE | FULLSCREEN_AUX)
    p.setReleasedWhenClosed_(False)
    # No system animation on ordering in or out (a panel's default shrank the strips and the lock
    # for a third of a second after the browser had gone): the overlay goes when the window goes.
    p.setAnimationBehavior_(2)  # NSWindowAnimationBehaviorNone
    return p


def layered(p, size):
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, *size))
    view.setWantsLayer_(True)
    p.setContentView_(view)
    return view.layer()


def ease():
    return CAMediaTimingFunction.functionWithName_(kCAMediaTimingFunctionEaseInEaseOut)


def paced(anim, rate=None):
    rate = rate or RATE
    if rate is not None:
        try:
            anim.setPreferredFrameRateRange_(rate)
        except Exception:
            pass
    return anim


def pause(layer):
    if layer.speed() == 0:
        return
    at = layer.convertTime_fromLayer_(CACurrentMediaTime(), None)
    layer.setSpeed_(0.0)
    layer.setTimeOffset_(at)


def resume(layer):
    if layer.speed() != 0:
        return
    paused = layer.timeOffset()
    layer.setSpeed_(1.0)
    layer.setTimeOffset_(0.0)
    layer.setBeginTime_(0.0)
    layer.setBeginTime_(layer.convertTime_fromLayer_(CACurrentMediaTime(), None) - paused)


def attributed(parts, truncate=False):
    """[(text, size, weight, color), ...] as one attributed string."""
    out = NSMutableAttributedString.alloc().init()
    style = None
    if truncate:
        style = NSMutableParagraphStyle.alloc().init()
        style.setLineBreakMode_(TRUNCATE_TAIL)
    for text, size, weight, color in parts:
        attrs = {NSFontAttributeName: NSFont.systemFontOfSize_weight_(size, weight),
                 NSForegroundColorAttributeName: ns(color)}
        if style is not None:
            attrs[NSParagraphStyleAttributeName] = style
        out.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(text, attrs))
    return out


def corner_path(x, y, w, h, box, n, steps=20):
    """A rectangle whose corners follow the superellipse (box, n); box 0 is square."""
    path = CGPathCreateMutable()
    box = max(0.0, min(box, w / 2, h / 2))
    centers = ((x + w - box, y + box, -math.pi / 2), (x + w - box, y + h - box, 0.0),
               (x + box, y + h - box, math.pi / 2), (x + box, y + box, math.pi))
    first = True
    for cx, cy, start in centers:
        for i in range(steps + 1):
            t = start + (math.pi / 2) * i / steps
            c, s_ = math.cos(t), math.sin(t)
            px = cx + box * math.copysign(abs(c) ** (2 / n), c)
            py = cy + box * math.copysign(abs(s_) ** (2 / n), s_)
            if first:
                CGPathMoveToPoint(path, None, px, py)
                first = False
            else:
                CGPathAddLineToPoint(path, None, px, py)
    CGPathCloseSubpath(path)
    return path


def light_at(d, depth=DEPTH):
    """How bright the light is `d` points in from the window's edge: a crisp line on the edge, a
    short bright halo, and a long soft body that reaches exactly 0 at `depth`, so nothing is cut
    off at the strips' inner edges. One smooth curve: the blurred layers it replaces showed as
    separate bands, and their blur ran past the strips and ended in a straight line."""
    if d < 1.5:
        return 1.0
    t = min(1.0, (d - 1.5) / (depth - 1.5))
    halo = 0.85 * math.exp(-(d - 1.5) / 3.0)
    body = 0.55 * (1.0 - t) ** 2.2 if t < 1.0 else 0.0
    return 1.0 - (1.0 - halo) * (1.0 - body) if t < 1.0 else 0.0


def thin_at(d):
    return light_at(d, THIN)


def extra_at(d):
    """What the light adds over the thin one: shown at full strength over thin_at it is exactly
    light_at, so fading it out thins the light smoothly (a over b = a + b(1 - a))."""
    thin = thin_at(d)
    return 0.0 if thin >= 1.0 else max(0.0, (light_at(d) - thin) / (1.0 - thin))


def bands(profile, step):
    """The profile as nested bands [lo, hi] of depth, each with the alpha it adds, widest first.
    A profile with one peak (light_at, thin_at, extra_at) is exact this way: each band holds every
    depth at or above one level, and no two bands meet edge to edge, so no seam shows."""
    depths = [i * step for i in range(int(DEPTH / step) + 2)]
    values = [profile(d) for d in depths]
    out, below = [], 0.0
    for level in sorted({v for v in values if v > 0.0}):
        held = [i for i, v in enumerate(values) if v >= level]
        add = (level - below) / (1.0 - below) if below < 1.0 else 0.0
        below = level
        # A pixel takes the value of the first grid depth at or below it, on both flanks.
        lo = 0.0 if held[0] == 0 else depths[held[0] - 1]
        out.append((lo, depths[held[-1]], add))
    return out


def ring_image(scale, corner=None, profile=light_at):
    """The glow's shape, drawn once: `profile` inward from an edge whose corners follow the
    window's (`corner`, see corner_shape; (0, 2) draws square ones), as a nine-slice whose middle
    stretches over any window size without drawing again. Every profile shares the margin, so
    the layers carrying them line up."""
    box, n = corner or CORNER
    margin = math.ceil(box + DEPTH + 4)
    side = 2 * margin + 1
    px = int(side * scale)
    ctx = CGBitmapContextCreate(None, px, px, 8, px * 4, CGColorSpaceCreateDeviceRGB(),
                                kCGImageAlphaPremultipliedLast)
    CGContextScaleCTM(ctx, scale, scale)
    for lo, hi, add in bands(profile, 1.0 / scale):  # one device pixel per step
        outer = corner_path(lo, lo, side - 2 * lo, side - 2 * lo, max(box - lo, 0.0), n)
        inner = corner_path(hi, hi, side - 2 * hi, side - 2 * hi, max(box - hi, 0.0), n)
        CGContextAddPath(ctx, outer)
        CGContextAddPath(ctx, inner)
        CGContextSetRGBFillColor(ctx, 0, 0, 0, add)
        CGContextEOFillPath(ctx)
    return CGBitmapContextCreateImage(ctx), margin, side


def ring_masks(scale, corner=None):
    """The thin ring and what the full light adds over it, drawn once."""
    thin, margin, side = ring_image(scale, corner, thin_at)
    extra, _margin, _side = ring_image(scale, corner, extra_at)
    return thin, extra, margin, side


class Glow:
    """One slowly turning conic gradient seen through the ring mask, carried by four thin
    windows along the browser window's edges. Each strip shows its part of the same gradient,
    turning in step, so the light reads as one ring. The mask is the thin ring plus what the
    full one adds; fading that in and out lets the light breathe between THIN and DEPTH, on the
    GPU, without drawing again."""

    def __init__(self, still, scale):
        # Masks drawn once: the window's own corners, and square ones for a full-screen window.
        self.masks = {"round": ring_masks(scale), "square": ring_masks(scale, (0.0, 2.0))}
        self.shape = "round"
        self.out = False   # faded out and ordered off the screen
        self.back = False  # turned on again after that: the overlay must order it back in
        thin_image, extra_image, margin, side = self.masks["round"]
        center = ((margin / side, margin / side), (1 / side, 1 / side))
        self.strips = []
        CATransaction.begin()  # one transaction: every strip's animation starts at the same time
        for _ in range(4):
            p = panel(NSMakeRect(0, 0, 10, 10), takes_clicks=False)
            root = layered(p, (10, 10))
            group = CALayer.layer()
            group.setOpacity_(0.0)
            motion = CALayer.layer()  # paused and resumed as a whole
            grad = CAGradientLayer.layer()
            grad.setType_(kCAGradientLayerConic)
            grad.setStartPoint_((0.5, 0.5))
            grad.setEndPoint_((0.5, 1.0))
            motion.addSublayer_(grad)
            mask = CALayer.layer()  # carries the two rings; paused with `motion`, measured
            thin, extra = CALayer.layer(), CALayer.layer()
            for layer, image in ((thin, thin_image), (extra, extra_image)):
                layer.setContents_(image)
                layer.setContentsScale_(scale)
                layer.setContentsCenter_(center)
                mask.addSublayer_(layer)
            motion.setMask_(mask)
            group.addSublayer_(motion)
            root.addSublayer_(group)
            if not still:
                spin = CABasicAnimation.animationWithKeyPath_("transform.rotation.z")
                spin.setFromValue_(0.0)
                spin.setToValue_(-2 * math.pi)
                spin.setDuration_(TURN)
                spin.setRepeatCount_(1e9)
                grad.addAnimation_forKey_(paced(spin), "spin")
                breathe = CABasicAnimation.animationWithKeyPath_("opacity")
                breathe.setFromValue_(1.0)
                breathe.setToValue_(0.0)
                breathe.setDuration_(BREATHE)
                breathe.setAutoreverses_(True)
                breathe.setRepeatCount_(1e9)
                breathe.setTimingFunction_(ease())
                extra.addAnimation_forKey_(paced(breathe), "breathe")
            self.strips.append((p, group, motion, grad, mask, thin, extra))
        CATransaction.commit()
        for strip in self.strips:
            pause(strip[2])
        self.panels = [strip[0] for strip in self.strips]
        self.palette = None
        self.level = "off"
        self.pause_at = None

    def corners(self, shape):
        """'round' or 'square' (a full-screen window has square corners): swap the mask, no redraw."""
        if shape == self.shape:
            return
        self.shape = shape
        thin_image, extra_image, margin, side = self.masks[shape]
        center = ((margin / side, margin / side), (1 / side, 1 / side))
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        for strip in self.strips:
            for layer, image in ((strip[5], thin_image), (strip[6], extra_image)):
                layer.setContents_(image)
                layer.setContentsCenter_(center)
        CATransaction.commit()

    def layout(self, x, y_ns, w, h):
        """Place the strips on the window's edges; each one's layers keep the whole window's
        geometry, shifted, so its part of the ring lines up with the others."""
        t = min(STRIP, w / 2, h / 2)
        rects = ((0, h - t, w, t), (0, 0, w, t), (0, t, t, h - 2 * t), (w - t, t, t, h - 2 * t))
        side = math.hypot(w, h)
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        for (p, group, motion, grad, mask, thin, extra), (sx, sy, sw, sh) in zip(self.strips, rects):
            p.setFrame_display_(NSMakeRect(x + sx, y_ns + sy, sw, max(sh, 1)), False)
            group.setFrame_(((0, 0), (sw, max(sh, 1))))
            motion.setFrame_(((-sx, -sy), (w, h)))
            mask.setFrame_(((0, 0), (w, h)))
            thin.setFrame_(((0, 0), (w, h)))
            extra.setFrame_(((0, 0), (w, h)))
            grad.setBounds_(((0, 0), (side, side)))
            grad.setPosition_((w / 2, h / 2))
        CATransaction.commit()

    def colors(self, palette):
        if palette is self.palette:
            return
        self.palette = palette
        CATransaction.begin()
        CATransaction.setAnimationDuration_(0.6)
        for strip in self.strips:
            strip[3].setColors_([cg(c) for c in palette])
        CATransaction.commit()

    def set(self, level):
        """on: full; dim: between steps, at 70% (half read as switched off); off: hidden. The light keeps turning
        through on and dim, so a change of step never stops it with a jolt; it stops only once
        it has faded out (settle), and starts turning before it fades in."""
        if level == self.level:
            return
        self.level = level
        if level == "off":
            self.pause_at = time.time() + 0.8
        else:
            self.pause_at = None
            if self.out:
                self.out, self.back = False, True  # ordered out once faded: the overlay restacks
            for strip in self.strips:
                resume(strip[2])
        CATransaction.begin()
        CATransaction.setAnimationDuration_({"on": 0.6, "dim": 0.9, "off": 0.7}[level])
        CATransaction.setAnimationTimingFunction_(ease())
        for strip in self.strips:
            strip[1].setOpacity_({"on": 1.0, "dim": 0.7, "off": 0.0}[level])
        CATransaction.commit()

    def settle(self, now):
        """Stop the turning once the light has faded out, and take its windows off the screen:
        frames nobody can see still cost, and four transparent windows stayed listed."""
        if self.pause_at is not None and self.level == "off" and now >= self.pause_at:
            self.pause_at = None
            for strip in self.strips:
                pause(strip[2])
            for p in self.panels:
                p.orderOut_(None)
            self.out = True


class Pointer:
    """The agent's cursor, when shown: an arrow with a name tag, as in shared editors, in a small
    window of its own that moves with it. CDP input never moves the person's own cursor, so
    without this the agent's pointing is invisible."""

    # A classic arrow, tip at the top left, in a 13 x 21 box with y going up.
    ARROW = [(0, 20), (0, 3.4), (4.1, 7.3), (6.9, 0.8), (9.5, 1.9), (6.8, 8.3), (12.4, 8.3)]
    TIP = (4.0, 34.0)  # where the tip sits in the window

    def __init__(self, scale):
        text = attributed([("Agent", 11, 0.4, WHITE)])
        tw = math.ceil(text.size().width)
        size = (34 + tw, 38)
        self.panel = panel(NSMakeRect(0, 0, *size), takes_clicks=False)
        root = layered(self.panel, size)
        self.body = CALayer.layer()
        self.body.setBounds_(((0, 0), (13, 21)))
        self.body.setAnchorPoint_((0.0, 20 / 21))  # the tip sits on the point
        self.body.setPosition_(self.TIP)
        self.body.setOpacity_(0.0)
        path = CGPathCreateMutable()
        CGPathMoveToPoint(path, None, *self.ARROW[0])
        for x, y in self.ARROW[1:]:
            CGPathAddLineToPoint(path, None, x, y)
        CGPathCloseSubpath(path)
        fill = CAGradientLayer.layer()
        fill.setFrame_(((0, 0), (13, 21)))
        fill.setColors_([cg(hexc("#60A5FA")), cg(hexc("#4F46E5"))])
        fill.setStartPoint_((0.0, 1.0))
        fill.setEndPoint_((1.0, 0.0))
        shape = CAShapeLayer.layer()
        shape.setPath_(path)
        fill.setMask_(shape)
        edge = CAShapeLayer.layer()
        edge.setPath_(path)
        edge.setFillColor_(None)
        edge.setStrokeColor_(NSColor.whiteColor().CGColor())
        edge.setLineWidth_(1.5)
        edge.setLineJoin_("round")
        arrow = CALayer.layer()
        arrow.setFrame_(((0, 0), (13, 21)))
        arrow.setShadowPath_(path)
        arrow.setShadowOpacity_(0.35)
        arrow.setShadowRadius_(2.5)
        arrow.setShadowOffset_((0, -1.0))
        arrow.addSublayer_(fill)
        arrow.addSublayer_(edge)
        tag = CAGradientLayer.layer()
        tag.setColors_([cg(hexc("#4F46E5")), cg(hexc("#2563EB"))])
        tag.setStartPoint_((0.0, 0.5))
        tag.setEndPoint_((1.0, 0.5))
        tag.setFrame_(((10, -13), (tw + 14, 18)))
        tag.setCornerRadius_(9.0)
        tag.setBorderWidth_(1.0)
        tag.setBorderColor_(cg((1, 1, 1, 0.85)))
        label = CATextLayer.layer()
        label.setString_(text)
        label.setContentsScale_(scale)
        label.setFrame_(((7, 2), (tw + 2, 14)))
        tag.addSublayer_(label)
        self.body.addSublayer_(tag)
        self.body.addSublayer_(arrow)
        root.addSublayer_(self.body)
        self.at = None
        self.want = False    # should be seen
        self.visible = False  # its window is on screen
        self.hide_at = None

    def move(self, sx, sy_ns):
        """Put the tip on a screen point (AppKit coordinates)."""
        origin = (round(sx - self.TIP[0]), round(sy_ns - self.TIP[1]))
        if origin != self.at:
            self.panel.setFrameOrigin_(origin)
            self.at = origin

    def show(self, on):
        if on == self.want:
            return
        self.want = on
        CATransaction.begin()
        CATransaction.setAnimationDuration_(0.2)
        self.body.setOpacity_(1.0 if on else 0.0)
        CATransaction.commit()
        self.hide_at = None if on else time.time() + 0.25


class Ripples:
    """A ring where a click happened, in one small window placed there for a moment."""

    SIZE = 72.0

    def __init__(self):
        self.panel = panel(NSMakeRect(0, 0, self.SIZE, self.SIZE), takes_clicks=False)
        self.root = layered(self.panel, (self.SIZE, self.SIZE))
        self.ring = None
        self.until = 0.0
        self.visible = False

    def play(self, sx, sy_ns, color="#3B82F6"):
        c = self.SIZE / 2
        self.panel.setFrameOrigin_((round(sx - c), round(sy_ns - c)))
        if self.ring is not None:
            self.ring.removeFromSuperlayer()
        ring = CAShapeLayer.layer()
        r = 9.0
        ring.setPath_(CGPathCreateWithEllipseInRect(CGRectMake(-r, -r, 2 * r, 2 * r), None))
        ring.setFillColor_(cg(hexc(color, 0.12)))
        ring.setStrokeColor_(cg(hexc(color, 0.9)))
        ring.setLineWidth_(1.5)
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        ring.setPosition_((c, c))
        ring.setOpacity_(0.0)
        self.root.addSublayer_(ring)
        CATransaction.commit()
        grow = CABasicAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(0.7)
        grow.setToValue_(2.8)
        fade = CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(1.0)
        fade.setToValue_(0.0)
        both = CAAnimationGroup.animation()
        both.setAnimations_([grow, fade])
        both.setDuration_(0.6)
        both.setTimingFunction_(CAMediaTimingFunction.functionWithName_("easeOut"))
        ring.addAnimation_forKey_(paced(both, RING_RATE), "ripple")
        self.ring = ring
        self.until = time.time() + 0.7

    def active(self):
        return time.time() < self.until


class PillButton(NSButton):
    """A flat pill: filled for the main action, translucent for the quiet one."""

    def acceptsFirstMouse_(self, _event):  # one click works on a window of an inactive app
        return True

    @objc.python_method
    def setup(self, target, action):
        self.kind = "quiet"
        self.hover = False
        self.setTarget_(target)
        self.setAction_(action)
        self.setBordered_(False)
        self.setFocusRingType_(1)
        self.setWantsLayer_(True)
        self.layer().setCornerRadius_(13.0)
        self.addTrackingArea_(NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            ((0, 0), (0, 0)), TRACK, self, None))

    @objc.python_method
    def style(self, title, kind):
        self.kind = kind
        self.setAttributedTitle_(attributed([(title, 12.5, 0.3, WHITE)]))
        self.paint(False)

    @objc.python_method
    def icon(self, symbol, kind, tip):
        self.kind = kind
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, tip) if SYMBOLS else None
        if img is not None:
            img = img.imageWithSymbolConfiguration_(
                NSImageSymbolConfiguration.configurationWithPointSize_weight_(12.5, 0.3))
            self.setTitle_("")
            self.setImage_(img)
            self.setImagePosition_(IMAGE_ONLY)
            self.setContentTintColor_(ns(WHITE))
        else:
            self.setAttributedTitle_(attributed([("Cursor", 12.5, 0.3, WHITE)]))
        self.setToolTip_(tip)
        self.setAccessibilityLabel_(tip)
        self.paint(False)

    @objc.python_method
    def paint(self, down):
        base, hover, pressed = BUTTON[self.kind]
        self.layer().setBackgroundColor_(cg(pressed if down else hover if self.hover else base))

    def mouseEntered_(self, _event):
        self.hover = True
        self.paint(False)

    def mouseExited_(self, _event):
        self.hover = False
        self.paint(False)

    def mouseDown_(self, event):
        self.paint(True)
        objc.super(PillButton, self).mouseDown_(event)  # runs until the button is let go
        self.paint(False)


class Shield(NSView):
    """Over the page while the agent works: a click or a scroll stops here instead of reaching
    the page, and the bar says why. Only the bar's button hands the browser over."""

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, event):
        p = event.locationInWindow()
        OVERLAY.nudge("click", (p.x, p.y))

    def rightMouseDown_(self, event):
        self.mouseDown_(event)

    def otherMouseDown_(self, event):
        self.mouseDown_(event)

    def scrollWheel_(self, _event):
        OVERLAY.nudge("scroll")


class Bar:
    """One size for as long as the window keeps its width: text and buttons change in place."""

    H = 40.0
    MAX_W = 600.0
    TITLES = ("Take over", "Resume agent", "Done")

    def __init__(self, target, still):
        self.still = still
        self.compact = BAR_MODE == "compact"
        if self.compact:  # a small bar: dot, the first words of the status, the button
            self.H, self.MAX_W = 30.0, 340.0
        self.bh = self.H - 14 if not self.compact else self.H - 8
        self.panel = panel(NSMakeRect(0, 0, 400, self.H), takes_clicks=True, shadow=True)
        self.panel.setAlphaValue_(0.0)
        # A plain tinted background: a live blur behind the bar would be redrawn on every frame
        # of the light moving under it.
        self.fx = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, 400, self.H))
        self.fx.setWantsLayer_(True)
        # Opaque: at 0.92 the white text of a page behind the bar read through it, and at 0.98
        # it still showed when looked at closely (5 levels of 255 over the bar's colour).
        self.fx.layer().setBackgroundColor_(cg((0.07, 0.08, 0.11, 1.0)))
        self.fx.layer().setCornerRadius_(self.H / 2)
        self.fx.layer().setBorderWidth_(0.5)
        self.fx.layer().setBorderColor_(cg((1, 1, 1, 0.16)))
        self.panel.setContentView_(self.fx)
        self.dot = NSView.alloc().initWithFrame_(NSMakeRect(14, (self.H - 12) / 2, 12, 12))
        self.dot.setWantsLayer_(True)
        self.halo = CAShapeLayer.layer()
        self.core = CAShapeLayer.layer()
        for layer in (self.halo, self.core):
            layer.setPath_(CGPathCreateWithEllipseInRect(CGRectMake(-4, -4, 8, 8), None))
            layer.setPosition_((6, 6))
            self.dot.layer().addSublayer_(layer)
        self.halo.setOpacity_(0.0)
        self.label = NSTextField.labelWithString_("")
        self.label.setLineBreakMode_(TRUNCATE_TAIL)
        self.label.setMaximumNumberOfLines_(1)
        self.label.setWantsLayer_(True)
        self.toggle = PillButton.alloc().initWithFrame_(NSMakeRect(0, 0, 30, self.bh))
        self.toggle.setup(target, "onCursor:")
        self.more = PillButton.alloc().initWithFrame_(NSMakeRect(0, 0, 30, self.bh))
        self.more.setup(target, "onMenu:")
        self.more.icon("ellipsis", "quiet", "Overlay settings")
        self.button = PillButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, self.bh))
        self.button.setup(target, "onButton:")
        for b in (self.toggle, self.button, self.more):
            b.layer().setCornerRadius_(self.bh / 2)
        self.bw = max(math.ceil(attributed([(t, 12.5, 0.3, WHITE)]).size().width) for t in self.TITLES) + 28
        self.tw = 30 if SYMBOLS else math.ceil(attributed([("Cursor", 12.5, 0.3, WHITE)]).size().width) + 24
        if self.compact:
            self.tw = 0  # no cursor switch in the small bar (SIGUSR1 still toggles it)
            self.toggle.setHidden_(True)
        for v in (self.dot, self.label, self.toggle, self.more, self.button):
            self.fx.addSubview_(v)
        self.width = None
        self.origin = None
        self.shown = {}
        self.visible = False
        self.hide_at = None

    def fit(self, window_w):
        width = float(max(220 if self.compact else 300, min(self.MAX_W, window_w - 48)))
        if width == self.width:
            return
        self.width = width
        pad = (self.H - self.bh) / 2
        bx = width - pad - self.bw
        tx = bx - (6 if self.tw else 0) - self.tw
        mx = tx - 6 - 30
        self.button.setFrame_(NSMakeRect(bx, pad, self.bw, self.bh))
        self.toggle.setFrame_(NSMakeRect(tx, pad, self.tw, self.bh))
        self.more.setFrame_(NSMakeRect(mx, pad, 30, self.bh))
        self.label.setFrame_(NSMakeRect(34, (self.H - 17) / 2, mx - 12 - 34, 17))
        f = self.panel.frame()
        self.panel.setFrame_display_(NSMakeRect(f.origin.x, f.origin.y, width, self.H), True)
        self.fx.setFrame_(NSMakeRect(0, 0, width, self.H))
        self.origin = None

    def changed(self, name, value):
        if self.shown.get(name, ()) == value:
            return False
        self.shown[name] = value
        return True

    def set(self, parts, title, kind, dot, alive, cursor_on):
        if self.compact and title != "Done":
            # The small bar keeps the first words; a request for help keeps what is asked.
            parts = [(text, 12, weight, color) for text, _size, weight, color in parts[:1]]
        if self.changed("text", tuple(parts)):
            if self.label.attributedStringValue().length() and not self.still:
                fade = CATransition.animation()
                fade.setType_(kCATransitionFade)
                fade.setDuration_(0.2)
                self.label.layer().addAnimation_forKey_(fade, "text")
            self.label.setAttributedStringValue_(attributed(parts, truncate=True))
        if self.changed("dot", dot):
            self.core.setFillColor_(cg(dot))
            self.halo.setFillColor_(cg(dot))
        if self.changed("alive", alive):
            self.halo.removeAllAnimations()
            if alive and not self.still:
                grow = CABasicAnimation.animationWithKeyPath_("transform.scale")
                grow.setFromValue_(1.0)
                grow.setToValue_(2.6)
                fade = CABasicAnimation.animationWithKeyPath_("opacity")
                fade.setFromValue_(0.55)
                fade.setToValue_(0.0)
                both = CAAnimationGroup.animation()
                both.setAnimations_([grow, fade])
                both.setDuration_(1.6)
                both.setRepeatCount_(1e9)
                self.halo.addAnimation_forKey_(paced(both), "halo")
        if self.changed("button", (title, kind)):
            self.button.style(title, kind)
        if self.changed("cursor", cursor_on):
            if cursor_on:
                self.toggle.icon("cursorarrow.rays", "on", "Hide the agent's cursor")
            else:
                self.toggle.icon("cursorarrow", "quiet", "Show the agent's cursor")

    def place(self, x_ns, y_ns, w):
        origin = (round(x_ns + (w - self.width) / 2), round(y_ns + 20))
        if origin != self.origin:
            self.panel.setFrameOrigin_(origin)
            self.origin = origin

    def contains(self, x_ns, y_ns):
        if not self.visible or self.origin is None:
            return False
        return (self.origin[0] <= x_ns <= self.origin[0] + self.width
                and self.origin[1] <= y_ns <= self.origin[1] + self.H)

    def fade(self, on):
        self.panel.animator().setAlphaValue_(1.0 if on else 0.0)


class Overlay:
    def __init__(self, target, show_cursor):
        self.control = {"holder": "agent", "since": time.time(), "reason": "start",
                        "person_last_input": 0.0, "help_done": 0.0, "show_cursor": show_cursor}
        # Started again after a crash: who held the browser, and the person's choices, stay.
        before = read(CONTROL)
        if before.get("holder") in ("agent", "person"):
            for key in ("holder", "since", "reason", "person_last_input", "help_done", "show_cursor"):
                if key in before:
                    self.control[key] = before[key]
        scale = NSScreen.mainScreen().backingScaleFactor()
        still = bool(NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion())
        self.target, self.still = target, still
        self.settings_at, self.settings_seen = 0.0, None
        self.cursor_seen = bool(CONFIG.get("cursor"))
        self.glow = Glow(still, scale)
        self.pointer = Pointer(scale)
        self.ripples = Ripples()
        self.bar = Bar(target, still)
        # Fully transparent, yet it takes every click: ignoresMouseEvents is set to False
        # explicitly (checked with windowNumberAtPoint: such a window is the one a click hits).
        self.shield = panel(NSMakeRect(0, 0, 400, 300), takes_clicks=True)
        self.shield.setContentView_(Shield.alloc().initWithFrame_(NSMakeRect(0, 0, 400, 300)))
        self.panels = (self.shield, *self.glow.panels, self.ripples.panel, self.pointer.panel, self.bar.panel)
        self.shield_on = False
        self.shield_rect = None
        self.frame = None
        self.shown = False
        self.win = None
        self.above = None
        self.level = NORMAL_LEVEL
        self.floating = False
        self.covered = True         # until a scan says otherwise
        self.browser_front = False
        self.switched_at = 0.0      # when another app last came to the front
        self.raise_until = 0.0      # put the overlay back over the browser on every tick until then
        self.browser_pid = None
        self.wins = []
        self.scanned_at = 0.0
        self.ticks = 0
        self.window_state = None
        self.full = False       # the window followed is a full-screen content view
        self.insets = None      # the page's insets from its window, measured outside full screen
        self.want_seen = None   # the server's last window bounds already matched against
        self.closing = None     # a window seen fading out as it closes: not followed again
        self.space_changed_at = 0.0
        self.start_probe = None  # (thread, result) of a start-time check running off the main thread
        self.hover_until = 0.0
        self.missing_since = None
        self.gone_since = None
        self.agent = {}
        self.agent_stamp = None
        self.prev_key = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown)
        self.prev_click = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState,
                                                                 kCGEventLeftMouseDown)
        self.prev_wheel = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState,
                                                                 kCGEventScrollWheel)
        self.clicks_seen = 0.0
        self.mode = "idle"
        self.nudge_until = 0.0
        self.nudge_why = None
        self.moved_until = 0.0
        self.start_checked = 0.0

        # Ready: the windows exist. The server waits for this before it says the overlay is on.
        self.control["overlay_pid"] = ME
        write(CONTROL, self.control)

    def save(self):
        write(CONTROL, self.control)

    def read_agent(self):
        """Parse the agent's file only when it was written again."""
        try:
            st = os.stat(AGENT)
        except OSError:
            return self.agent
        stamp = (st.st_ino, st.st_mtime_ns)
        if stamp != self.agent_stamp:
            data = read(AGENT)
            if data:
                self.agent, self.agent_stamp = data, stamp
        return self.agent

    def followed_bounds(self, pid):
        """The followed window's bounds now, or None when it is gone or off screen."""
        if self.win is None:
            return None
        for number, owner, ordinary, bounds, onscreen in one_window(self.win):
            if number == self.win:
                return bounds if ordinary and onscreen and owner == pid else None
        return None

    def page_rect(self, b, a):
        """Where the page is on screen now (global, top-left origin). The server measures it at
        each tool call and about once a second while the overlay shows, but the person can move
        or resize the window in between: the light followed at once and the lock kept the old
        size (measured: a window widened from 1000 to 1250 left 250 pt of the page unlocked). So
        the page's insets from its window are applied to the window's live frame. In full screen
        the window followed is the content view itself (the toolbar is a window of its own), so
        it is the page."""
        x, y, w, h = b
        if self.full:
            return x, y, w, h
        measured = insets(a.get("window"), a.get("viewport"))
        if measured is not None and a.get("window_state") != "fullscreen":
            self.insets = measured  # full screen's insets belong to another window
        left, top, right, bottom = self.insets or measured or (0.0, 0.0, 0.0, 0.0)
        return x + left, y + top, max(0.0, w - left - right), max(0.0, h - top - bottom)

    def browser_window(self, pid, scan, want=None):
        """Bounds of the window followed, kept while it is on screen, so a popup the browser
        opens never moves the overlay. A scan reads the windows above it as well. `want` is
        where the server last saw the window of the agent's tab; another window of the browser
        matching it better is followed instead."""
        if self.win is not None and want and scan and want != self.want_seen:
            # Once per new word from the server: it saw the agent's tab in a window of another
            # size or place. Either the person moved or resized this one (keep following it), or
            # the tab is now in another window (follow that). Only a window matching says which;
            # dropping this one whenever its bounds differed jumped to the largest window, and
            # read every window on screen four times a second until the next tool call.
            self.want_seen = want
            current = next((e for e in one_window(self.win) if e[0] == self.win), None)
            if current is None or current[3] is None:
                self.win = None
            elif not near(current[3], want):
                match = next((e[0] for e in all_windows()
                              if e[1] == pid and e[2] and e[3] is not None and near(e[3], want)), None)
                if match is not None and match != self.win:
                    self.win = None
        if self.win is not None and self.win == self.closing:
            self.win = None
        if self.win is not None:
            got = above_and_including(self.win) if scan else one_window(self.win)
            for number, owner, ordinary, b, onscreen in got:
                if number == self.win:
                    if ordinary and onscreen and owner == pid:
                        if scan:
                            self.wins = got
                        return b
                    break
        # Lost or not chosen yet: the one where the agent's tab is, else the largest.
        best = None
        for number, owner, ordinary, b, _on in all_windows():
            if number == self.closing:
                continue
            if owner == pid and ordinary and b[2] >= 300 and b[3] >= 200:
                if want and near(b, want):
                    best = (number, b)
                    break
                if best is None or b[2] * b[3] > best[1][2] * best[1][3]:
                    best = (number, b)
        if best is None:
            self.win = None
            return None
        self.win = best[0]
        self.wins = above_and_including(self.win)
        return best[1]

    def stack(self):
        """Our windows bottom to top: lock, light, click ring, cursor, bar."""
        out = [self.shield] if self.shield_on else []
        if LIGHT_ON and not self.glow.out:
            out.extend(self.glow.panels)
        if self.ripples.visible:
            out.append(self.ripples.panel)
        if self.pointer.visible:
            out.append(self.pointer.panel)
        if self.bar.visible:
            out.append(self.bar.panel)
        return out

    def order(self, stack, wid):
        if self.level == NORMAL_LEVEL:
            below = wid
            for p in stack:
                p.orderWindow_relativeTo_(ABOVE, below)
                below = p.windowNumber()
        else:
            for p in stack:
                p.orderFrontRegardless()
        self.above = wid

    def hide(self):
        if self.shown:
            for p in self.panels:
                p.orderOut_(None)
            self.bar.panel.setAlphaValue_(0.0)
            self.bar.visible = self.pointer.visible = self.ripples.visible = False
            self.bar.hide_at = self.pointer.hide_at = None
            self.pointer.show(False)
            self.shield_on = False
            self.glow.set("off")
            self.shown = False

    def ring(self, sx, sy_ns, color):
        self.ripples.play(sx, sy_ns, color)
        if not self.ripples.visible and self.shown and self.above is not None:
            self.ripples.visible = True
            self.order(self.stack(), self.above)

    def nudge(self, why, point=None):
        """A click, scroll or key while the agent works: it does not take over; say so."""
        if self.mode not in WORKING:
            return
        self.nudge_until = time.time() + NUDGE_FOR
        self.nudge_why = why
        if point is not None:
            s = self.shield.frame()
            self.ring(s.origin.x + point[0], s.origin.y + point[1], "#94A3B8")

    def tick(self, force=False):
        """One look at the browser's window, the agent's file and the person's input, and the
        overlay made to match, in this order: the window closing and the end of the browser or
        the server; where the window is, or how long it has been missing; the light and the bar
        fitted to it and the level wanted; what the person did; the mode; then the light, the
        agent's cursor, the bar's words and place, the lock, and last the windows' order."""
        now = time.time()
        self.ticks += 1
        if skip_tick(self.ticks, force, self.pointer.want or self.ripples.visible or now < self.moved_until):
            return  # nothing moves: 10 ticks a second are enough
        a = self.read_agent()
        self.check_settings(now)
        self.go_with_closing_window(now)
        if self.leaves(a):
            return
        pid = self.browser_pid = a.get("pid")
        scan = force or now - self.scanned_at >= SCAN_EVERY
        b = self.find_window(pid, scan, a, now)
        if b is None:
            self.window_missing(pid, now)
            return
        self.missing_since = None
        wid, H = self.win, screen_height()
        self.fit_to_window(b, a, pid, wid, H, now)
        self.choose_level(b, pid, wid, scan, now)
        loc, new_key, new_click, new_wheel, over = self.read_input(wid, H)
        mode = self.mode_now(a, now)
        self.note_person(a, mode, pid, new_key, new_click, new_wheel, over, now)
        show_cursor = bool(self.control.get("show_cursor"))
        self.light(mode, now)
        page = self.page_rect(b, a) if a.get("viewport") else None
        self.agent_cursor(a, page, mode, show_cursor, H)
        self.say(a, mode, over, show_cursor, now)
        self.bar.place(b[0], H - b[1] - b[3], b[2])
        want_lock = wants_lock(LOCK_ON, mode, page)
        if want_lock:
            self.place_lock(page, H)
        restack = self.show_parts(want_lock, wid, now)
        bar_restack, fade_in = self.show_bar(mode, wid, loc, H, now)
        self.stack_up(restack or bar_restack, scan, wid, now)
        if fade_in:
            self.bar.fade(True)

    def go_with_closing_window(self, now):
        """The browser's window gone (the browser quit) or fading out as macOS closes it: go with
        it now. MISSING_FOR is for a window that is still there but not seen (another Space, a
        minimize), where hiding at once would blink. A window seen closing is never followed
        again while it fades: it stays listed and "on screen" for the 0.3 s of its close
        animation, and taking it back showed the light and the lock over it again until it was
        gone. Only gone, or fully back (an alpha that dipped for another reason), ends that."""
        if self.closing is not None and closing_over(window_alpha(self.closing)):
            self.closing = None
        alpha = window_alpha(self.win) if self.shown and self.win is not None else 1.0
        if window_going(alpha):
            self.hide()
            self.frame = None
            self.missing_since = self.missing_since or now
            self.closing = self.win if alpha is not None else None
            self.win = None

    def leaves(self, a):
        """The server said the browser ended, or the server is gone: leave, removing the folder
        (the server removes it too; whichever comes first). True when leaving."""
        if a.get("ended") or os.getppid() != SERVER:
            leave(True)
            return True
        return False

    def find_window(self, pid, scan, a, now):
        """The browser window's bounds now, or None (window_read says how they are read)."""
        how = window_read(pid, scan, now < self.moved_until, self.frame is not None,
                          self.missing_since is not None)
        if how == "search":
            b = self.browser_window(pid, scan, a.get("window"))
            if scan:
                self.scanned_at = now
            return b
        if how == "followed":
            # Between scans, so a drag or a resize the person starts is followed from its first
            # frame instead of the next scan (it lagged 0.2 to 0.3 s).
            return self.followed_bounds(pid)
        return None

    def window_missing(self, pid, now):
        """No window of the browser this tick. The lock over the page is invisible, so it goes at
        once: nothing on screen changes, and clicks where the closed window was are not
        swallowed during MISSING_FOR. The light still waits it out (no blinking), and the lock
        comes back with the window."""
        if self.shield_on:
            self.shield.orderOut_(None)
            self.shield_on = False
        self.missing_since = self.missing_since or now
        if now - self.missing_since > missing_for(now - self.space_changed_at):
            self.hide()
            self.frame = None
        if pid:
            self.check_browser(pid, now)

    def check_browser(self, pid, now):
        """Leave once the browser's PID has been gone 3 s, or runs another process now (its start
        time says). ps runs on a thread: the tick, which also hides the overlay, never waits for
        a shell."""
        try:
            os.kill(pid, 0)
            self.gone_since = None
            start = CONFIG.get("start")
            if checks_start(start, now - self.missing_since, now - self.start_checked,
                            self.start_probe is not None):
                self.start_checked = now
                box = {}
                probe = threading.Thread(target=lambda: box.update(value=chrome_start(pid)), daemon=True)
                probe.start()
                self.start_probe = (probe, box)
            if self.start_probe is not None and not self.start_probe[0].is_alive():
                box, self.start_probe = self.start_probe[1], None
                if pid_reused(box.get("value"), start):
                    leave(True)
        except OSError:
            self.gone_since = self.gone_since or now
            if now - self.gone_since > 3:
                leave(True)  # the browser is gone

    def fit_to_window(self, b, a, pid, wid, H, now):
        """Fit the light and the bar to the window when it moved, was resized, or entered or left
        full screen, and follow it at the full rate for MOVING_FOR after."""
        x, y, w, h = b
        state = a.get("window_state")
        relayout = b != self.frame
        if b != self.frame or state != self.window_state:
            self.window_state = state
            full = full_screen(x, y, w, h, state, others=lambda: [
                e[3] for e in all_windows() if e[1] == pid and e[2] and e[3] is not None and e[0] != wid])
            relayout = relayout or full != self.full
            self.full = full
            self.glow.corners("square" if self.full else "round")
        if relayout:
            # The light goes round the whole browser. In full screen the window followed is the
            # page's content view, and the tab strip and toolbar are windows of their own above
            # it, so the light took in the page alone; there it goes round the screen.
            lx, ly, lw, lh = (screen_frame_of(x, y, w, h) or b) if self.full else b
            self.glow.layout(lx, H - ly - lh, lw, lh)
            self.bar.fit(w)
            self.frame = b
            self.moved_until = now + MOVING_FOR  # follow a dragged window at the full rate

    def choose_level(self, b, pid, wid, scan, now):
        """Whether the overlay floats (wants_floating), from what the last scan saw: another
        window over the browser, the browser in front."""
        if scan:
            self.covered = covered(self.wins, wid, b, pid)
            self.browser_front = self.browser_in_front(pid)
        self.floating = wants_floating(self.covered, self.browser_front, now - self.switched_at)

    def browser_in_front(self, pid):
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        return front is not None and front.processIdentifier() == pid

    def read_input(self, wid, H):
        """The pointer, whether a key, a click or a scroll happened since the last tick, and
        whether the pointer is over the browser's window (not over the bar)."""
        loc = NSEvent.mouseLocation()
        key = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown)
        new_key = happened(key, self.prev_key)
        self.prev_key = key
        click = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventLeftMouseDown)
        new_click = happened(click, self.prev_click)
        self.prev_click = click
        wheel = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventScrollWheel)
        new_wheel = happened(wheel, self.prev_wheel)
        self.prev_wheel = wheel
        over = not self.bar.contains(loc.x, loc.y) and visible_at(self.wins, wid, loc.x, H - loc.y)
        return loc, new_key, new_click, new_wheel, over

    def mode_now(self, a, now):
        """The mode (choose_mode). A request for help and the person's hold outlast any quiet:
        their buttons (Done, Resume agent) are the only way back, so they never fade out."""
        self.mode = choose_mode(a.get("help"), self.control["holder"], quiet_for(a, now), a.get("waiting"),
                                help_answered(a, self.control))
        return self.mode

    def note_person(self, a, mode, pid, new_key, new_click, new_wheel, over, now):
        """What the person did with the browser. A key cannot be held back without taking focus
        from the browser, and typing never hands the browser over: while the agent works it
        pauses, then puts its own text back. While the agent is not working the page is open to
        the person, and what they do there goes to the agent's next call, so it reads the page
        again rather than act on what it saw before. Keys count only with the browser in front,
        the pointer only over it."""
        if new_key and mode in WORKING and self.browser_in_front(pid):
            self.control["person_last_input"] = now
            self.save()
            self.nudge("type")
        if mode in ("resting", "idle", "noted") and (new_key or new_click or new_wheel):
            in_front = self.browser_in_front(pid)
            used = used_now(new_click and over, new_wheel and over, new_key and in_front)
            if used:
                self.control["person_used"] = used_since_agent(self.control, a, used)
                self.control["person_used_at"] = now
                if "typed" in used:
                    self.control["person_last_input"] = now
                self.save()

    def light(self, mode, now):
        """The light's colours and level for the mode (light_level)."""
        self.glow.colors(HELP_GLOW if mode == "help" else AGENT_GLOW)
        self.glow.set(light_level(mode, LIGHT_ON))
        self.glow.settle(now)
        if self.glow.back:
            self.glow.back = False
            self.above = None  # order the light's windows back in on this tick

    def agent_cursor(self, a, page, mode, show_cursor, H):
        """The agent's cursor where the server last put it, shown while the agent works if the
        person asked for it, and a ring at each new click of the agent's. A point outside the page
        hides the cursor until the next one inside, and a click there is taken without a ring."""
        cursor = a.get("cursor")
        point = cursor_point(cursor, page, a.get("zoom"))
        if point is not None:
            self.pointer.move(point[0], H - point[1])
        self.pointer.show(point is not None and show_cursor and mode in WORKING)
        if cursor and page and float(a.get("click_at", 0)) > self.clicks_seen:
            self.clicks_seen = float(a["click_at"])
            if point is not None and mode == "agent" and show_cursor:
                self.ring(point[0], H - point[1], "#3B82F6")

    def say(self, a, mode, over, show_cursor, now):
        """The bar's words, button and dot (bar_words)."""
        words = bar_words(mode, a, self.control, over, now < self.nudge_until, self.nudge_why)
        if words is not None:
            first, rest, title, kind, dot, alive = words
            self.bar.set([(first, 13, 0.3, WHITE), (rest, 13, 0.0, MUTED)], title, kind, DOT[dot], alive,
                         show_cursor)

    def place_lock(self, page, H):
        """The lock's frame over the page, set only when it changed."""
        px, py, pw, ph = page
        rect = (px, H - py - ph, pw, ph)
        if rect != self.shield_rect:
            self.shield.setFrame_display_(NSMakeRect(*rect), False)
            self.shield_rect = rect

    def show_parts(self, want_lock, wid, now):
        """The overlay is shown from here on: the lock in or out, and the cursor and the click
        ring on screen while there is something to show. True when the windows must be ordered
        again over the browser's."""
        restack = not self.shown or wid != self.above
        self.shown = True
        if want_lock != self.shield_on:
            self.shield_on = want_lock
            if not want_lock:
                self.shield.orderOut_(None)
            restack = True
        if self.pointer.want and not self.pointer.visible:
            self.pointer.visible = restack = True
        if self.pointer.hide_at is not None and now >= self.pointer.hide_at:
            self.pointer.panel.orderOut_(None)
            self.pointer.visible = False
            self.pointer.hide_at = None
        if self.ripples.visible and not self.ripples.active():
            self.ripples.panel.orderOut_(None)
            self.ripples.visible = False
        return restack

    def show_bar(self, mode, wid, loc, H, now):
        """The bar in, or faded out and then taken off the screen (wants_bar). Returns (restack,
        fade_in): it came on screen, and it fades in once the windows are ordered."""
        if BAR_MODE == "hover" and (visible_at(self.wins, wid, loc.x, H - loc.y)
                                    or self.bar.contains(loc.x, loc.y)):
            self.hover_until = now + HOVER_LINGER
        want_bar = wants_bar(mode, BAR_MODE, now < self.nudge_until, now < self.hover_until)
        restack = fade_in = False
        if want_bar and not self.bar.visible:
            self.bar.visible = True
            fade_in = restack = True
        if want_bar and self.bar.hide_at is not None:
            self.bar.hide_at = None
            fade_in = True
        elif not want_bar and self.bar.visible and self.bar.hide_at is None:
            self.bar.fade(False)
            self.bar.hide_at = now + 0.35
        if self.bar.hide_at is not None and now >= self.bar.hide_at:
            self.bar.panel.orderOut_(None)
            self.bar.visible = False
            self.bar.hide_at = None
        return restack, fade_in

    def stack_up(self, restack, scan, wid, now):
        """The level, and the windows ordered over the browser's when needed. While nothing covers
        the browser the overlay sits in the floating level: the browser coming to the front then
        cannot cover it, not even for a frame. Covered, it sits just above it."""
        level = FLOATING_LEVEL if self.floating else NORMAL_LEVEL
        if level != self.level:
            for p in self.panels:
                p.setLevel_(level)
            self.level = level
            restack = True
        stack = self.stack()
        if scan and level == NORMAL_LEVEL and not directly_above(
                self.wins, {p.windowNumber() for p in stack}, wid):
            restack = True
        if now < self.raise_until:
            restack = True  # the browser came to the front over the overlay: back over it now
        if restack:
            self.order(stack, wid)

    def button(self):
        now = time.time()
        if self.mode in WORKING or self.mode in ("resting", "noted"):
            self.control.update(holder="person", since=now, reason="button")
        elif self.mode in ("person", "help"):
            if self.mode == "person":
                self.control.update(holder="agent", since=now, reason="button")
            else:
                self.control.update(holder="agent", since=now, reason="help done", help_done=now)
        self.save()

    def toggle_cursor(self):
        self.control["show_cursor"] = not self.control.get("show_cursor")
        self.save()

    def check_settings(self, now, force=False):
        """Read the settings file again about once a second (a stat), and apply what changed."""
        if not force and now - self.settings_at < SETTINGS_EVERY:
            return
        self.settings_at = now
        try:
            seen = os.stat(SETTINGS_FILE).st_mtime_ns if SETTINGS_FILE else None
        except OSError:
            seen = None
        if seen == self.settings_seen and not force:
            return
        self.settings_seen = seen
        data = read_settings()
        self.apply_style(*chosen_style(data))
        self.apply_timing_and_cursor(data)

    def apply_timing_and_cursor(self, data):
        """overlay_hide_after and agent_cursor from the file (the environment's fixed value wins),
        applied as they change: they were read only when the overlay started."""
        global HIDE_AFTER
        try:
            hide = float(FIXED.get("overlay_hide_after", data.get("overlay_hide_after", CONFIG.get("hide_after", 90.0))))
            if 1.0 <= hide <= 86400.0:
                HIDE_AFTER = hide
        except (TypeError, ValueError):
            pass
        cursor = _flag(FIXED.get("agent_cursor", data.get("agent_cursor", CONFIG.get("cursor", False))), False)
        if cursor != self.cursor_seen:
            # Only a change of the setting: the bar's arrow button toggles the cursor for this
            # browser alone, and a file left as it was must not undo that.
            self.cursor_seen = cursor
            if bool(self.control.get("show_cursor")) != cursor:
                self.control["show_cursor"] = cursor
                self.save()

    def apply_style(self, bar, light, lock):
        global BAR_MODE, LIGHT_ON, LOCK_SETTING, LOCK_ON
        rebuild = (bar == "compact") != (BAR_MODE == "compact")
        light_was = LIGHT_ON
        BAR_MODE, LIGHT_ON, LOCK_SETTING = bar, light, lock
        LOCK_ON = LOCK_SETTING and BAR_MODE != "off"
        if rebuild:
            self.bar.panel.orderOut_(None)
            self.bar = Bar(self.target, self.still)
            # The level the others are at: a new panel starts at the normal level, and while the
            # overlay floats it sat under the (transparent) lock, which then took its clicks.
            self.bar.panel.setLevel_(self.level)
            self.panels = (self.shield, *self.glow.panels, self.ripples.panel, self.pointer.panel, self.bar.panel)
            if self.frame is not None:
                self.bar.fit(self.frame[2])
        if light_was and not LIGHT_ON:
            for p in self.glow.panels:
                p.orderOut_(None)
        self.above = None  # restack on the next tick

    def choose(self, key, value):
        """A choice from the bar's menu: into the settings file, applied now."""
        if key in FIXED:
            return
        try:
            write_settings({key: value})
        except OSError:
            traceback.print_exc()
        self.check_settings(time.time(), force=True)

    def menu(self, sender):
        menu = NSMenu.alloc().initWithTitle_("Overlay")
        menu.setAutoenablesItems_(False)

        def item(title, action, state=None, enabled=True, value=None, tip=None):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
            if tip:
                it.setToolTip_(tip)
            if action:
                it.setTarget_(self.target)
            if state is not None:
                it.setState_(1 if state else 0)
            if value is not None:
                it.setRepresentedObject_(value)
            it.setEnabled_(enabled)
            menu.addItem_(it)
            return it

        fixed_note = "  (fixed by the MCP config)"
        item("Bar" + (fixed_note if "overlay_bar" in FIXED else ""), None, enabled=False)
        # Hidden takes this menu away with the bar; it shows again only for a request for help or
        # the person's hold, so the way back is said where it is chosen.
        hidden_tip = "The bar and this menu stay hidden while the agent works. To bring them back: mcp-stealth-chrome settings reset overlay_bar"
        for mode, title in (("full", "Full"), ("compact", "Compact"), ("hover", "Show on hover"), ("off", "Hidden")):
            item("    " + title, "pickBar:", BAR_MODE == mode, "overlay_bar" not in FIXED, mode,
                 hidden_tip if mode == "off" else None)
        menu.addItem_(NSMenuItem.separatorItem())
        item("Light" + (fixed_note if "overlay_light" in FIXED else ""), "toggleLight:",
             LIGHT_ON, "overlay_light" not in FIXED)
        lock_title = ("Lock the page" + (fixed_note if "overlay_lock" in FIXED else "")
                      + ("  (off while the bar is hidden)" if BAR_MODE == "off" else ""))
        item(lock_title, "toggleLock:", LOCK_ON, "overlay_lock" not in FIXED and BAR_MODE != "off")
        if "agent_cursor" in FIXED:
            item("Agent's cursor" + fixed_note, "onCursor:", bool(self.control.get("show_cursor")))
        else:
            item("Agent's cursor", "pickCursor:", bool(self.control.get("show_cursor")))
        menu.addItem_(NSMenuItem.separatorItem())
        item("Saved for every agent browser", None, enabled=False)
        # Above the button: the bar sits at the bottom of the window, and a menu opened below it
        # fell off the window. The location is the menu's top-left corner in the button's view.
        # Its right edge on the button's, so it opens over the window rather than past its edge.
        width, height = menu.size().width, menu.size().height
        y = -(height + 6) if sender.isFlipped() else sender.bounds().size.height + 6 + height
        menu.popUpMenuPositioningItem_atLocation_inView_(None, (sender.bounds().size.width - width, y), sender)


TIMER = None
FAST = False


def schedule(interval):
    """(Re)start the tick timer at `interval`: TICK normally, TICK_MOVING while the window moves."""
    global TIMER
    if TIMER is not None:
        TIMER.invalidate()
    TIMER = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        interval, TARGET, "tick:", None, True)
    TIMER.setTolerance_(interval / 4)
    # Common modes: the default mode alone stops while the bar's menu is open (event tracking),
    # and a browser closed meanwhile left the overlay standing until the menu closed.
    NSRunLoop.currentRunLoop().addTimer_forMode_(TIMER, NSRunLoopCommonModes)


class Target(NSObject):
    def tick_(self, _timer):
        global FAST
        try:
            OVERLAY.tick()
            fast = time.time() < OVERLAY.moved_until
            if fast != FAST:
                FAST = fast
                schedule(TICK_MOVING if fast else TICK)
        except Exception:
            traceback.print_exc()
            sys.stderr.flush()

    def switched_(self, note):
        """Another app came to the front, or the Space changed: settle at once."""
        try:
            now = time.time()
            if note is not None and note.name() == NSWorkspaceActiveSpaceDidChangeNotification:
                OVERLAY.space_changed_at = now
            else:
                OVERLAY.switched_at = now
                app = note.userInfo().get("NSWorkspaceApplicationKey") if note is not None and note.userInfo() else None
                if app is not None and OVERLAY.browser_pid and app.processIdentifier() == OVERLAY.browser_pid:
                    # Activating the browser raises its windows over the overlay's: order it
                    # back over them now and on every tick for a moment, at the full rate,
                    # rather than when the next scan of the window list sees it.
                    OVERLAY.raise_until = now + RAISE_FOR
                    OVERLAY.moved_until = max(OVERLAY.moved_until, now + RAISE_FOR)
            OVERLAY.tick(force=True)
        except Exception:
            traceback.print_exc()

    def onButton_(self, _sender):
        try:
            OVERLAY.button()
        except Exception:
            traceback.print_exc()

    def onCursor_(self, _sender):
        try:
            OVERLAY.toggle_cursor()
        except Exception:
            traceback.print_exc()

    def onMenu_(self, sender):
        try:
            OVERLAY.menu(sender)
        except Exception:
            traceback.print_exc()

    def pickBar_(self, sender):
        try:
            OVERLAY.choose("overlay_bar", str(sender.representedObject()))
        except Exception:
            traceback.print_exc()

    def pickCursor_(self, _sender):
        try:
            OVERLAY.choose("agent_cursor", not bool(OVERLAY.control.get("show_cursor")))
        except Exception:
            traceback.print_exc()

    def toggleLight_(self, _sender):
        try:
            OVERLAY.choose("overlay_light", not LIGHT_ON)
        except Exception:
            traceback.print_exc()

    def toggleLock_(self, _sender):
        try:
            OVERLAY.choose("overlay_lock", not LOCK_SETTING)
        except Exception:
            traceback.print_exc()


if __name__ in ("__main__", "<stdin>"):
    # The stdio server ignores SIGTERM and children inherit that: without this, terminate()
    # from the server would do nothing.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(1)  # accessory: no Dock icon, never the active app
    TARGET = Target.alloc().init()
    OVERLAY = Overlay(TARGET, bool(CONFIG.get("cursor")))
    signal.signal(signal.SIGUSR1, lambda *_: OVERLAY.toggle_cursor())  # for tests: no click
    signal.signal(signal.SIGUSR2, lambda *_: OVERLAY.button())
    center = NSWorkspace.sharedWorkspace().notificationCenter()
    for name in (NSWorkspaceDidActivateApplicationNotification, NSWorkspaceActiveSpaceDidChangeNotification):
        center.addObserver_selector_name_object_(TARGET, "switched:", name, None)
    OVERLAY.tick(force=True)  # the first tick now: the timer's first one was skipped as calm
    schedule(TICK)
    app.run()
