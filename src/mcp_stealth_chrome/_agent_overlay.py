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
import time
import traceback

import objc
from AppKit import (NSApplication, NSAttributedString, NSButton, NSColor, NSEvent, NSFont,
                    NSFontAttributeName, NSForegroundColorAttributeName, NSImage,
                    NSImageSymbolConfiguration, NSMakeRect, NSMutableAttributedString,
                    NSMutableParagraphStyle, NSPanel, NSParagraphStyleAttributeName, NSScreen,
                    NSTextField, NSTrackingArea, NSView, NSWorkspace,
                    NSWorkspaceActiveSpaceDidChangeNotification,
                    NSWorkspaceDidActivateApplicationNotification)
from Foundation import NSObject, NSTimer
from Quartz import (CAAnimationGroup, CABasicAnimation, CACurrentMediaTime, CAGradientLayer,
                    CALayer, CAMediaTimingFunction, CAShapeLayer, CATextLayer, CATransaction,
                    CATransition, CGBitmapContextCreate, CGBitmapContextCreateImage,
                    CGColorSpaceCreateDeviceRGB, CGContextAddPath, CGContextRestoreGState,
                    CGContextEOFillPath, CGContextSaveGState, CGContextScaleCTM,
                    CGContextSetLineWidth, CGContextSetRGBFillColor,
                    CGContextSetRGBStrokeColor, CGContextSetShadowWithColor, CGContextStrokePath,
                    CGEventSourceSecondsSinceLastEventType, CGPathAddLineToPoint,
                    CGPathCloseSubpath, CGPathCreateMutable, CGPathCreateWithEllipseInRect,
                    CGPathMoveToPoint, CGRectMake,
                    CGWindowListCopyWindowInfo, kCAMediaTimingFunctionEaseInEaseOut,
                    kCATransitionFade, kCGEventKeyDown, kCGEventSourceStateHIDSystemState,
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
STRIP = DEPTH + 8   # thickness of the edge windows that carry it: the light is 0 past DEPTH
TICK = 1 / 20       # the glow's own pace: every update lands on a frame drawn anyway
TICK_MOVING = 1 / 60  # while the browser window moves: at 20 a second the light trailed a drag
CALM_EVERY = 2      # with nothing moving, do a tick's work every 2nd tick
SCAN_EVERY = 0.25   # seconds between reads of the windows above the browser's
MOVING_FOR = 1.0    # after the browser window moved, follow it at the full rate this long
QUIET_AFTER = 2.5   # no word from the agent for this long: it is thinking, the glow dims
MISSING_FOR = 0.6   # the browser window must be gone this long before anything hides
NUDGE_FOR = 3.0     # how long the bar explains a click or key that did not take over
WORKING = ("agent", "polite", "thinking")

CONFIG = json.loads(sys.argv[-1])
STATE = CONFIG["state"]
AGENT = os.path.join(STATE, "agent.json")
CONTROL = os.path.join(STATE, "control.json")
SERVER = int(CONFIG["server"])
HIDE_AFTER = float(CONFIG.get("hide_after", 90.0))  # no word for this long: everything fades out
STYLE = CONFIG.get("style") or {}  # presence.style(): the person's choice of bar, light and lock
BAR_MODE = STYLE.get("bar", "full") if STYLE.get("bar") in ("full", "compact", "hover", "off") else "full"
LIGHT_ON = STYLE.get("light", True) is not False
LOCK_ON = STYLE.get("lock", True) is not False and BAR_MODE != "off"
HOVER_LINGER = 1.2  # hover: the bar stays this long after the pointer left the window
ME = os.getpid()


def hexc(h, a=1.0):
    h = h.lstrip("#")
    return (int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255, a)


# Indigo, blue, cyan and violet: calm, and the same family as the button.
AGENT_GLOW = [hexc("#4F46E5"), hexc("#2563EB"), hexc("#06B6D4"), hexc("#8B5CF6"), hexc("#4F46E5")]
HELP_GLOW = [hexc("#F59E0B"), hexc("#F97316"), hexc("#FBBF24"), hexc("#F59E0B")]
DOT = {"agent": hexc("#3B82F6"), "thinking": hexc("#3B82F6"), "polite": hexc("#94A3B8"),
       "person": hexc("#22C55E"), "help": hexc("#F59E0B")}
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


def screen_height():
    return NSScreen.screens()[0].frame().size.height


def full_screen(x, y, w, h, state=None):
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
    return False


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


def near(b, want, slack=4.0):
    return all(abs(float(p) - float(q)) <= slack for p, q in zip(b, want))


def contains(b, x, y):
    return b[0] <= x < b[0] + b[2] and b[1] <= y < b[1] + b[3]


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


def panel(frame, takes_clicks, shadow=False):
    p = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        frame, BORDERLESS | NONACTIVATING, BUFFERED, False)
    p.setOpaque_(False)
    p.setBackgroundColor_(NSColor.clearColor())
    p.setHasShadow_(shadow)
    p.setIgnoresMouseEvents_(not takes_clicks)  # set explicitly: transparent areas then count too
    p.setHidesOnDeactivate_(False)
    p.setBecomesKeyOnlyIfNeeded_(True)
    p.setCollectionBehavior_(ALL_SPACES | IGNORES_CYCLE | FULLSCREEN_AUX)
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


def light_at(d):
    """How bright the light is `d` points in from the window's edge: a crisp line on the edge, a
    short bright halo, and a long soft body that reaches exactly 0 at DEPTH, so nothing is cut
    off at the strips' inner edges. One smooth curve: the blurred layers it replaces showed as
    separate bands, and their blur ran past the strips and ended in a straight line."""
    if d < 1.5:
        return 1.0
    t = min(1.0, (d - 1.5) / (DEPTH - 1.5))
    halo = 0.85 * math.exp(-(d - 1.5) / 3.0)
    body = 0.55 * (1.0 - t) ** 2.2 if t < 1.0 else 0.0
    return 1.0 - (1.0 - halo) * (1.0 - body) if t < 1.0 else 0.0


def ring_image(scale, corner=None):
    """The glow's shape, drawn once: light_at's curve inward from an edge whose corners follow
    the window's (`corner`, see corner_shape; (0, 2) draws square ones), as a nine-slice whose
    middle stretches over any window size without drawing again.

    Drawn as nested frames, each from the edge to one depth, from the deepest out, every frame
    adding just enough alpha that the total at each depth is light_at: the frames share their
    outer edge and their inner edges never coincide, so no seam appears between steps."""
    box, n = corner or CORNER
    margin = math.ceil(box + DEPTH + 4)
    side = 2 * margin + 1
    px = int(side * scale)
    ctx = CGBitmapContextCreate(None, px, px, 8, px * 4, CGColorSpaceCreateDeviceRGB(),
                                kCGImageAlphaPremultipliedLast)
    CGContextScaleCTM(ctx, scale, scale)
    step = 1.0 / scale  # one device pixel
    depths = [DEPTH - i * step for i in range(int(DEPTH / step) + 1)]
    below = 0.0  # the alpha already laid at the previous, deeper frame
    for d in depths:
        want = light_at(max(d, 0.0))
        if want <= below:
            continue
        add = (want - below) / (1.0 - below) if below < 1.0 else 0.0
        below = want
        path = corner_path(0, 0, side, side, box, n)
        inner = corner_path(d, d, side - 2 * d, side - 2 * d, max(box - d, 0.0), n)
        CGContextAddPath(ctx, path)
        CGContextAddPath(ctx, inner)
        CGContextSetRGBFillColor(ctx, 0, 0, 0, add)
        CGContextEOFillPath(ctx)
    return CGBitmapContextCreateImage(ctx), margin, side


class Glow:
    """One slowly turning conic gradient seen through the ring mask, carried by four thin
    windows along the browser window's edges. Each strip shows its part of the same gradient,
    turning in step, so the light reads as one ring."""

    def __init__(self, still, scale):
        # Two masks drawn once: the window's own corners, and square ones for a full-screen window.
        self.masks = {"round": ring_image(scale), "square": ring_image(scale, (0.0, 2.0))}
        self.shape = "round"
        image, margin, side = self.masks["round"]
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
            mask = CALayer.layer()
            mask.setContents_(image)
            mask.setContentsScale_(scale)
            mask.setContentsCenter_(((margin / side, margin / side), (1 / side, 1 / side)))
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
                breathe.setFromValue_(0.82)
                breathe.setToValue_(1.0)
                breathe.setDuration_(2.6)
                breathe.setAutoreverses_(True)
                breathe.setRepeatCount_(1e9)
                breathe.setTimingFunction_(ease())
                motion.addAnimation_forKey_(paced(breathe), "breathe")
            self.strips.append((p, group, motion, grad, mask))
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
        image, margin, side = self.masks[shape]
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        for strip in self.strips:
            strip[4].setContents_(image)
            strip[4].setContentsCenter_(((margin / side, margin / side), (1 / side, 1 / side)))
        CATransaction.commit()

    def layout(self, x, y_ns, w, h):
        """Place the strips on the window's edges; each one's layers keep the whole window's
        geometry, shifted, so its part of the ring lines up with the others."""
        t = min(STRIP, w / 2, h / 2)
        rects = ((0, h - t, w, t), (0, 0, w, t), (0, t, t, h - 2 * t), (w - t, t, t, h - 2 * t))
        side = math.hypot(w, h)
        CATransaction.begin()
        CATransaction.setDisableActions_(True)
        for (p, group, motion, grad, mask), (sx, sy, sw, sh) in zip(self.strips, rects):
            p.setFrame_display_(NSMakeRect(x + sx, y_ns + sy, sw, max(sh, 1)), False)
            group.setFrame_(((0, 0), (sw, max(sh, 1))))
            motion.setFrame_(((-sx, -sy), (w, h)))
            mask.setFrame_(((0, 0), (w, h)))
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
            for strip in self.strips:
                resume(strip[2])
        CATransaction.begin()
        CATransaction.setAnimationDuration_({"on": 0.6, "dim": 0.9, "off": 0.7}[level])
        CATransaction.setAnimationTimingFunction_(ease())
        for strip in self.strips:
            strip[1].setOpacity_({"on": 1.0, "dim": 0.7, "off": 0.0}[level])
        CATransaction.commit()

    def settle(self, now):
        """Stop the turning once the light has faded out: frames nobody can see still cost."""
        if self.pause_at is not None and self.level == "off" and now >= self.pause_at:
            self.pause_at = None
            for strip in self.strips:
                pause(strip[2])


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
        self.fx.layer().setBackgroundColor_(cg((0.07, 0.08, 0.11, 0.92)))
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
        self.button = PillButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, self.bh))
        self.button.setup(target, "onButton:")
        for b in (self.toggle, self.button):
            b.layer().setCornerRadius_(self.bh / 2)
        self.bw = max(math.ceil(attributed([(t, 12.5, 0.3, WHITE)]).size().width) for t in self.TITLES) + 28
        self.tw = 30 if SYMBOLS else math.ceil(attributed([("Cursor", 12.5, 0.3, WHITE)]).size().width) + 24
        if self.compact:
            self.tw = 0  # no cursor switch in the small bar (SIGUSR1 still toggles it)
            self.toggle.setHidden_(True)
        for v in (self.dot, self.label, self.toggle, self.button):
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
        self.button.setFrame_(NSMakeRect(bx, pad, self.bw, self.bh))
        self.toggle.setFrame_(NSMakeRect(tx, pad, self.tw, self.bh))
        self.label.setFrame_(NSMakeRect(34, (self.H - 17) / 2, tx - 12 - 34, 17))
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
        self.wins = []
        self.scanned_at = 0.0
        self.ticks = 0
        self.window_state = None
        self.hover_until = 0.0
        self.missing_since = None
        self.gone_since = None
        self.agent = {}
        self.agent_stamp = None
        self.prev_key = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown)
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

    def browser_window(self, pid, scan, want=None):
        """Bounds of the window followed, kept while it is on screen, so a popup the browser
        opens never moves the overlay. A scan reads the windows above it as well. `want` is
        where the server last saw the window of the agent's tab; another window of the browser
        matching it better is followed instead."""
        if self.win is not None and want and scan:
            current = next((e for e in one_window(self.win) if e[0] == self.win), None)
            if current is None or current[3] is None or not near(current[3], want):
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
        if LIGHT_ON:
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
        now = time.time()
        self.ticks += 1
        calm = not (self.pointer.want or self.ripples.visible or now < self.moved_until)
        if calm and not force and self.ticks % CALM_EVERY:
            return  # nothing moves: 10 ticks a second are enough
        a = self.read_agent()
        alpha = window_alpha(self.win) if self.shown and self.win is not None else 1.0
        if alpha is None or alpha < 0.5:
            # The browser's window no longer exists (the browser quit), or is fading out as macOS
            # closes it: go with it now. MISSING_FOR is for a window that is still there but not
            # seen (another Space, a minimize), where hiding at once would blink.
            self.hide()
            self.frame = None
            self.missing_since = self.missing_since or now
        if a.get("ended"):
            leave(True)  # the server removes the folder too; whichever comes first
            return
        if os.getppid() != SERVER:
            leave(True)  # the server is gone
            return
        pid = a.get("pid")
        scan = force or now - self.scanned_at >= SCAN_EVERY
        need_bounds = scan or now < self.moved_until or self.frame is None
        b = None
        if pid and need_bounds:
            b = self.browser_window(pid, scan, a.get("window"))
            if scan:
                self.scanned_at = now
        elif pid and self.frame is not None and self.missing_since is None:
            # The last bounds stand in between scans only while the window is there. Once a scan
            # missed it they must not, or every tick in between reset the missing time and
            # MISSING_FOR never ran out: after a Quit the light and the lock over the page (which
            # takes clicks) stayed 3.35 s, until this process left with the browser.
            b = self.frame
        if b is None:
            # The lock over the page is invisible, so it goes at once: nothing on screen changes,
            # and clicks where the closed window was are not swallowed during MISSING_FOR. The
            # light still waits it out (no blinking), and the lock comes back with the window.
            if self.shield_on:
                self.shield.orderOut_(None)
                self.shield_on = False
            self.missing_since = self.missing_since or now
            if now - self.missing_since > MISSING_FOR:
                self.hide()
                self.frame = None
            if pid:
                try:
                    os.kill(pid, 0)
                    self.gone_since = None
                    # A window missing for a while on a PID that runs: that PID may now be
                    # another process's. Its start time says.
                    start = CONFIG.get("start")
                    if start and now - self.missing_since > 3 and now - self.start_checked > 5:
                        self.start_checked = now
                        current = chrome_start(pid)
                        if current is None or (current and current != start):
                            leave(True)
                            return
                except OSError:
                    self.gone_since = self.gone_since or now
                    if now - self.gone_since > 3:
                        leave(True)  # the browser is gone
            return
        self.missing_since = None
        x, y, w, h = b
        wid = self.win
        H = screen_height()
        y_ns = H - y - h
        state = a.get("window_state")
        if b != self.frame or state != self.window_state:
            self.window_state = state
            self.glow.corners("square" if full_screen(x, y, w, h, state) else "round")
        if b != self.frame:
            self.glow.layout(x, y_ns, w, h)
            self.bar.fit(w)
            self.frame = b
            self.moved_until = now + MOVING_FOR  # follow a dragged window at the full rate
        if scan:
            self.floating = not covered(self.wins, wid, b, pid)

        loc = NSEvent.mouseLocation()
        key = CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateHIDSystemState, kCGEventKeyDown)
        new_key = key + 0.005 < self.prev_key
        self.prev_key = key
        over = not self.bar.contains(loc.x, loc.y) and visible_at(self.wins, wid, loc.x, H - loc.y)
        quiet = now - float(a.get("active_at", 0))
        help_text = a.get("help")

        # A request for help and the person's hold outlast any quiet: their buttons (Done,
        # Resume agent) are the only way back, so they never fade out.
        if help_text:
            mode = "help"
        elif self.control["holder"] == "person":
            mode = "person"
        elif quiet > HIDE_AFTER:
            mode = "idle"
        elif a.get("waiting") == "polite":
            mode = "polite"
        elif quiet > QUIET_AFTER:
            mode = "thinking"
        else:
            mode = "agent"
        self.mode = mode

        if new_key and mode in WORKING:
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front is not None and front.processIdentifier() == pid:
                # A key cannot be held back without taking focus from the browser. Typing never
                # hands the browser over: the agent pauses, then puts its own text back.
                self.control["person_last_input"] = now
                self.save()
                self.nudge("type")

        show_cursor = bool(self.control.get("show_cursor"))
        self.glow.colors(HELP_GLOW if mode == "help" else AGENT_GLOW)
        self.glow.set(("on" if mode in ("agent", "help") else "dim" if mode in WORKING else "off")
                      if LIGHT_ON else "off")
        self.glow.settle(now)
        cursor, vp = a.get("cursor"), a.get("viewport")
        if cursor and vp:
            cx, cy = x + vp[0] + cursor[0], y + vp[1] + cursor[1]  # screen, top-left origin
            self.pointer.move(cx, H - cy)
        else:
            cursor = None
        self.pointer.show(bool(cursor) and show_cursor and mode in WORKING)
        if cursor and float(a.get("click_at", 0)) > self.clicks_seen:
            self.clicks_seen = float(a["click_at"])
            if mode == "agent" and show_cursor:
                self.ring(cx, H - cy, "#3B82F6")

        status = a.get("status") or "Using the browser"
        if mode in WORKING and now < self.nudge_until:
            first = ("Typing does not take over" if self.nudge_why == "type"
                     else "The page is locked while the agent works")
            self.bar.set([(first, 13, 0.3, WHITE), ("  \u00b7  Press Take over to use it", 13, 0.0, MUTED)],
                         "Take over", "primary", DOT[mode], mode == "agent", show_cursor)
        elif mode in ("agent", "thinking"):
            hint = ("  \u00b7  Press Take over to use the browser" if over
                    else "  \u00b7  Thinking about the next step" if mode == "thinking"
                    else f"  \u00b7  {status}")
            self.bar.set([("Agent working", 13, 0.3, WHITE), (hint, 13, 0.0, MUTED)],
                         "Take over", "quiet", DOT[mode], mode == "agent", show_cursor)
        elif mode == "polite":
            self.bar.set([("Agent paused", 13, 0.3, WHITE), ("  \u00b7  It goes on in a moment", 13, 0.0, MUTED)],
                         "Take over", "quiet", DOT["polite"], False, show_cursor)
        elif mode == "person":
            self.bar.set([("You have the browser", 13, 0.3, WHITE), ("  \u00b7  The agent is paused", 13, 0.0, MUTED)],
                         "Resume agent", "primary", DOT["person"], False, show_cursor)
        elif mode == "help":
            self.bar.set([("Your help is needed", 13, 0.3, WHITE), (f"  \u00b7  {help_text}", 13, 0.0, MUTED)],
                         "Done", "primary", DOT["help"], True, show_cursor)
        self.bar.place(x, y_ns, w)

        want_shield = LOCK_ON and mode in WORKING and bool(vp)
        if want_shield:
            left, top, vw, vh = vp
            rect = (x + left, H - (y + top) - vh, vw, vh)
            if rect != self.shield_rect:
                self.shield.setFrame_display_(NSMakeRect(*rect), False)
                self.shield_rect = rect

        restack = not self.shown or wid != self.above
        self.shown = True
        if want_shield != self.shield_on:
            self.shield_on = want_shield
            if not want_shield:
                self.shield.orderOut_(None)
            restack = True
        # The cursor and the click ring: on screen while there is something to show.
        if self.pointer.want and not self.pointer.visible:
            self.pointer.visible = restack = True
        if self.pointer.hide_at is not None and now >= self.pointer.hide_at:
            self.pointer.panel.orderOut_(None)
            self.pointer.visible = False
            self.pointer.hide_at = None
        if self.ripples.visible and not self.ripples.active():
            self.ripples.panel.orderOut_(None)
            self.ripples.visible = False
        must_show = mode in ("help", "person") or now < self.nudge_until  # their buttons are the way back
        if BAR_MODE == "off":
            want_bar = must_show
        elif BAR_MODE == "hover":
            if visible_at(self.wins, wid, loc.x, H - loc.y) or self.bar.contains(loc.x, loc.y):
                self.hover_until = now + HOVER_LINGER
            want_bar = mode != "idle" and (must_show or now < self.hover_until)
        else:
            want_bar = mode != "idle"
        fade_in = False
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
        # While nothing covers the browser, sit in the floating level: the browser coming to the
        # front then cannot cover the overlay, not even for a frame. Covered, sit just above it.
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
        if restack:
            self.order(stack, wid)
        if fade_in:
            self.bar.fade(True)

    def button(self):
        now = time.time()
        if self.mode in WORKING:
            self.control.update(holder="person", since=now, reason="button")
        elif self.mode == "person":
            self.control.update(holder="agent", since=now, reason="button")
        elif self.mode == "help":
            self.control.update(holder="agent", since=now, reason="help done", help_done=now)
        self.save()

    def toggle_cursor(self):
        self.control["show_cursor"] = not self.control.get("show_cursor")
        self.save()


TIMER = None
FAST = False


def schedule(interval):
    """(Re)start the tick timer at `interval`: TICK normally, TICK_MOVING while the window moves."""
    global TIMER
    if TIMER is not None:
        TIMER.invalidate()
    TIMER = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        interval, TARGET, "tick:", None, True)
    TIMER.setTolerance_(interval / 4)


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

    def switched_(self, _note):
        """Another app came to the front, or the Space changed: settle at once."""
        try:
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
    schedule(TICK)
    app.run()
