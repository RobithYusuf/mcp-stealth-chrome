"""Which instance a launch starts, and which one a close leaves current, said in the result.

close_instance of the session's current instance leaves that id current, not running. It is not
switched to main on its own: a tool call right after the close would then act on main's browser,
which may be the one the person is looking at. So the following browser_launch starts that same
instance again, on its own profile (profiles/<id>, or brave-profiles/<id> for browser="brave").
v0.19.0 did that while answering "Browser launched ...", naming neither the instance nor why; an
agent expecting main got another profile's cookie jar. Both results now name it.
"""
from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable

from .state import BrowserState

MAIN = "main"


def _on_first_line(result: str, note: str) -> str:
    # A launch result is one summary line and then a JSON body; the note goes on the summary
    # line so the body still parses.
    head, sep, rest = result.partition("\n")
    return f"{head}{note}{sep}{rest}"


def launched_as(result: str, instance_id: str, profile: Any = None) -> str:
    """A browser_launch result, with the instance and its profile named when it is not main."""
    if instance_id == MAIN or not isinstance(result, str) or result.startswith("Error"):
        return result
    where = f"profile {profile}" if profile else "its own profile"
    return _on_first_line(result, (
        f" [launched as instance {instance_id!r} on {where}: it is this session's current "
        f"instance (set by switch_instance, and left current by close_instance of it); "
        f"switch_instance('main') first to launch main]"))


def closed_current(result: str, instance_id: str) -> str:
    """A close_instance result for the current instance: it stays current, not running."""
    if instance_id == MAIN or not isinstance(result, str) or result.startswith("Error"):
        return result
    if BrowserState.current_instance_id != instance_id or BrowserState.browser is not None:
        return result
    return _on_first_line(result, (
        f" [{instance_id!r} stays this session's current instance, not running: the next "
        f"browser_launch starts {instance_id!r} again on its own profile, and other tools say "
        f"no browser is running; switch_instance('main') to go back to main]"))


def names_the_instance(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """For browser_launch: append launched_as to its result. functools.wraps keeps the signature
    and docstring FastMCP reads."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        instance_id = BrowserState.current_instance_id
        result = await fn(*args, **kwargs)
        profile = (BrowserState.current_profile_dir
                   if BrowserState.current_instance_id == instance_id else None)
        return launched_as(result, instance_id, profile)
    return wrapper


def says_what_stays_current(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """For close_instance: append closed_current when it closed the current instance."""
    @functools.wraps(fn)
    async def wrapper(instance_id: str, *args: Any, **kwargs: Any) -> Any:
        was_current = instance_id == BrowserState.current_instance_id
        result = await fn(instance_id, *args, **kwargs)
        return closed_current(result, instance_id) if was_current else result
    return wrapper
