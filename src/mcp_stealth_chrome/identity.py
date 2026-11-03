"""Coherent browser identity and complete key events, as plain functions.

A browser identity is several fields that must agree: the User-Agent string,
navigator.platform, and the client hints in navigator.userAgentData. Overriding the
User-Agent over CDP without metadata leaves userAgentData.brands EMPTY, which no
real Chrome ever reports. So every override goes through ua_metadata_fields, which
derives the hints from the same string the caller chose.
"""
from __future__ import annotations

import platform as _platform
import re
import sys
from typing import Any, Optional

_CHROME_VERSION = re.compile(r"(?:Headless)?Chrome/(\d+)\.(\d+)\.(\d+)\.(\d+)")

_ESCAPED = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")
_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def chrome_version(user_agent: str) -> Optional[str]:
    """Full Chrome version in a UA string, or None when it is not a Chrome UA."""
    found = _CHROME_VERSION.search(user_agent or "")
    return ".".join(found.groups()) if found else None


def strip_headless(user_agent: str) -> str:
    """Headless Chrome announces itself as HeadlessChrome/; the rest is identical."""
    return (user_agent or "").replace("HeadlessChrome/", "Chrome/")


def brand_list(major: int, version: Optional[str] = None) -> list[dict[str, str]]:
    """Chrome's brand list for a major version, ordered and GREASEd as Chrome does.

    Chromium derives the GREASE brand, its version and the order from the major
    version alone, so the same version always yields the same list. Checked against
    the Chrome on this machine by a test; the format has changed across Chrome
    versions before, so a much older major in an override may differ. Pass the full
    version to build fullVersionList instead of brands.
    """
    grease = f"Not{_ESCAPED[major % 11]}A{_ESCAPED[(major + 1) % 11]}Brand"
    grease_version = _GREASE_VERSIONS[major % 3]
    if version:
        grease_version = f"{grease_version}.0.0.0"
    real = version or str(major)
    entries = (
        {"brand": grease, "version": grease_version},
        {"brand": "Chromium", "version": real},
        {"brand": "Google Chrome", "version": real},
    )
    order = _ORDERS[major % 6]
    return [entries[i] for i in order]


def os_family(user_agent: str = "", platform: str = "") -> Optional[str]:
    """windows, mac or linux. An explicit navigator.platform wins over the UA."""
    if platform.startswith("Win"):
        return "windows"
    if platform.startswith("Mac"):
        return "mac"
    if "Linux" in platform or platform.startswith("X11"):
        return "linux"
    if "Windows" in user_agent:
        return "windows"
    if "Macintosh" in user_agent or "Mac OS" in user_agent:
        return "mac"
    if "Linux" in user_agent or "X11" in user_agent:
        return "linux"
    return None


def native_family() -> str:
    if sys.platform == "darwin":
        return "mac"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


_NAVIGATOR_PLATFORM = {"windows": "Win32", "mac": "MacIntel", "linux": "Linux x86_64"}
_HINT_PLATFORM = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}


def navigator_platform(family: Optional[str]) -> Optional[str]:
    return _NAVIGATOR_PLATFORM.get(family or "")


def _machine_hints(family: str) -> tuple[str, str]:
    """(platformVersion, architecture). Real values on this OS, typical ones otherwise."""
    if family != native_family():
        return {"windows": "10.0.0", "mac": "15.0.0", "linux": "6.8.0"}[family], "x86"
    machine = _platform.machine().lower()
    arch = "arm" if machine.startswith(("arm", "aarch")) else "x86"
    if family == "mac":
        version = _platform.mac_ver()[0] or "15.0.0"
    elif family == "windows":
        version = _platform.version() or "10.0.0"
    else:
        version = _platform.release().split("-")[0] or "6.8.0"
    parts = (version.split(".") + ["0", "0"])[:3]
    return ".".join(parts), arch


def ua_metadata_fields(
    user_agent: str, platform: str = "", real_version: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Client hints that agree with a UA string. None when it is not a Chrome UA,
    since a Firefox or Safari UA has no userAgentData to agree with.

    Chrome's UA string is reduced to 156.0.0.0 while its hints carry the real build,
    so real_version supplies that build whenever the major version is the same.
    """
    full = chrome_version(user_agent)
    family = os_family(user_agent, platform)
    if not full or not family:
        return None
    major = int(full.split(".")[0])
    if real_version and real_version.split(".")[0] == str(major):
        full = real_version
    platform_version, architecture = _machine_hints(family)
    return {
        "platform": _HINT_PLATFORM[family],
        "platform_version": platform_version,
        "architecture": architecture,
        "model": "",
        "mobile": False,
        "bitness": "64",
        "full_version": full,
        "brands": brand_list(major),
        "full_version_list": brand_list(major, full),
    }


_ANDROID_UA = re.compile(r"Android ([\d.]+); ([^;)]+)")


def device_ua_metadata_fields(
    user_agent: str, real_version: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """ua_metadata_fields for an emulated phone. Android's hints name the device rather
    than the desktop this is running on. None for a Safari UA, which has no hints."""
    hints = ua_metadata_fields(user_agent, real_version=real_version)
    found = _ANDROID_UA.search(user_agent or "")
    if hints is None or found is None:
        return hints
    return {**hints, "platform": "Android", "platform_version": found.group(1),
            "model": found.group(2).strip(), "mobile": True,
            "architecture": "", "bitness": ""}


def device_navigator_platform(user_agent: str) -> Optional[str]:
    """navigator.platform on the device a mobile UA names."""
    for marker, platform in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Linux armv81")):
        if marker in (user_agent or ""):
            return platform
    return None


def navigator_languages(accept_language: str) -> list[str]:
    """The language tags in an Accept-Language value, without weights or duplicates.

    Chrome splits the override on commas to build navigator.languages, so passing it
    "id-ID,id;q=0.9" reports a language literally named "id;q=0.9".
    """
    tags: list[str] = []
    for part in (accept_language or "").split(","):
        tag = part.split(";", 1)[0].strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def default_accept_language(tags: list[str]) -> str:
    """The header Chrome itself generates for these languages: q falling by 0.1."""
    parts = []
    for index, tag in enumerate(tags):
        weight = max(10 - index, 1)
        parts.append(tag if index == 0 else f"{tag};q=0.{weight}")
    return ",".join(parts)


# ── Key events ─────────────────────────────────────────────────────────────

# name -> (key, code, windows virtual key code, text). The text is what makes a key
# act on the page: without "\r" Enter fires keydown but no keypress, so a form never
# gets its implicit submission.
_NAMED_KEYS: dict[str, tuple[str, str, int, Optional[str]]] = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "return": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, None),
    "escape": ("Escape", "Escape", 27, None),
    "esc": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "insert": ("Insert", "Insert", 45, None),
    "space": (" ", "Space", 32, " "),
    " ": (" ", "Space", 32, " "),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "up": ("ArrowUp", "ArrowUp", 38, None),
    "down": ("ArrowDown", "ArrowDown", 40, None),
    "left": ("ArrowLeft", "ArrowLeft", 37, None),
    "right": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    **{f"f{n}": (f"F{n}", f"F{n}", 111 + n, None) for n in range(1, 13)},
}
_MODIFIERS = {
    "alt": 1, "option": 1,
    "control": 2, "ctrl": 2,
    "meta": 4, "cmd": 4, "command": 4,
    "shift": 8,
}
_PUNCTUATION = {
    "-": ("Minus", 189), "=": ("Equal", 187), "[": ("BracketLeft", 219),
    "]": ("BracketRight", 221), "\\": ("Backslash", 220), ";": ("Semicolon", 186),
    "'": ("Quote", 222), ",": ("Comma", 188), ".": ("Period", 190),
    "/": ("Slash", 191), "`": ("Backquote", 192),
}


def key_event_params(key: str) -> dict[str, Any]:
    """Complete CDP key fields for "Enter", "a" or "Control+a". ValueError if unknown."""
    if not key:
        raise ValueError("key must not be empty")
    parts = key.split("+") if len(key) > 1 and not key.endswith("+") else [key]
    modifiers = 0
    for name in parts[:-1]:
        bit = _MODIFIERS.get(name.strip().lower())
        if bit is None:
            raise ValueError(f"unknown modifier {name!r} in {key!r}")
        modifiers |= bit
    name = parts[-1] if parts[-1] == " " else parts[-1].strip()
    named = _NAMED_KEYS.get(name.lower())
    if named:
        key_value, code, vk, text = named
    elif len(name) == 1:
        key_value, text = name, name
        if name.isascii() and name.isalpha():
            code, vk = f"Key{name.upper()}", ord(name.upper())
            if name.isupper():
                modifiers |= _MODIFIERS["shift"]
        elif name.isdigit():
            code, vk = f"Digit{name}", ord(name)
        else:
            code, vk = _PUNCTUATION.get(name, ("", 0))
    else:
        raise ValueError(
            f"unknown key {key!r}. Use a single character, a name such as Enter, Tab, "
            f"Escape, Backspace, ArrowDown, Home, PageDown, F5, or a combination such "
            f"as Control+a."
        )
    # A modified key is a shortcut. Sending text as well would also type the letter.
    if modifiers & (_MODIFIERS["control"] | _MODIFIERS["meta"] | _MODIFIERS["alt"]):
        text = None
    return {
        "key": key_value,
        "code": code,
        "windows_virtual_key_code": vk,
        "native_virtual_key_code": vk,
        "text": text,
        "modifiers": modifiers,
    }
