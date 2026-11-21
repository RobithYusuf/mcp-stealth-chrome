"""The anti-bot tripwire: whether a page is a site's verification page, and why, in words.

An agent reading many pages does not notice the moment a site answers with a verification page:
it goes on opening pages into the wall, which makes the block worse. The tripwire notices for it,
stops, and hands the page to the person. It never solves or bypasses anything.

This module is data and pure functions, free of browser imports, so every rule is testable alone:
the built-in rules (each one sourced), the person's own (the `antibot_rules` setting), the match
on a URL and on words the page shows, and the lines a tool's result says. The browser side (the
one probe of the page, the request for help, where it is called from) is in server.py.

Narrow on purpose: a false stop of a long run is a defect too. No rule fires on a word alone in a
URL ("captcha", "verify", "challenge" in an ordinary path or query); a built-in rule names a host,
and a page's words are read only on a host with a rule that asks for them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlsplit

MODES = ("pause", "warn", "off")
PAUSED = "antibot_paused"      # the status word a pause starts with, right after "Error: "
WARNING = "antibot_warning"    # the first line a warning puts before a tool's own result
PAUSE_PREFIX = f"Error: {PAUSED}:"

# How much of a page's address a result shows: a verification URL can carry 2 KB of tracking id,
# and the field run's failed workflow said it four times (8 KB).
PATH_SHOWN = 120
QUERY_SHOWN = 48
URL_MATCH_LENGTH = 8192  # cap input to a person's regex; Python's re has no timeout
# The workflow steps that look at the page they reach themselves. Before a call's first step the
# page is not looked at when the step is one of these: it leaves the page.
NAVIGATION_TOOLS = frozenset({"navigate", "reload", "go_back", "go_forward"})


@dataclass(frozen=True)
class Rule:
    """A verification page on one host and its subdomains. path: regexes searched in the path;
    query: (parameter, value prefix) pairs; url: regexes searched in path?query (the person's
    rules); text: words the page shows, any case, read only for this host."""
    host: str
    path: tuple = ()
    query: tuple = ()
    url: tuple = ()
    text: tuple = ()
    source: str = "built-in rule"


# Each entry sourced; keep the table tiny.
BUILT_IN = (
    # A research session on Shopee, about 230 pages in one day on one account (Nov 2026, the 0.27
    # field requests): the first product page of a new run was redirected to
    # /verify/captcha?anti_bot_tracking_id=<about 2 KB>&scene=crawler_item&next=/verify/traffic?...
    # and the page read "Verifikasi untuk melanjutkan" and "Geser untuk menyelesaikan puzzle".
    Rule("shopee.co.id",
         path=(r"^/verify/(?:captcha|traffic)(?:/|$)",),
         query=(("scene", "crawler_"),),
         text=("Verifikasi untuk melanjutkan", "Geser untuk menyelesaikan puzzle")),
)


@dataclass(frozen=True)
class Verdict:
    """A verification page found: what fired (in words), and where."""
    trigger: str
    url: str
    host: str
    path: str
    kind: str = "rule"   # "rule" or "cloudflare"

    @property
    def place(self) -> str:
        return f"{self.host} {cut(self.path or '/', 60)}"


def cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def person_rules(value: Any) -> tuple:
    """The `antibot_rules` setting as Rules. settings.parse_rules has checked it already; anything
    else reads as no rules."""
    if not isinstance(value, dict):
        return ()
    out = []
    for host, rule in value.items():
        if not isinstance(rule, dict):
            continue
        url = tuple(p for p in rule.get("url") or () if isinstance(p, str) and p)
        text = tuple(w for w in rule.get("text") or () if isinstance(w, str) and w.strip())
        if url or text:
            out.append(Rule(str(host).lower(), url=url, text=text,
                            source="rule from your antibot_rules"))
    return tuple(out)


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _on(host: str, rule_host: str) -> bool:
    return host == rule_host or host.endswith("." + rule_host)


def rules_for(host: str, rules: Iterable[Rule]) -> list:
    return [r for r in rules if host and _on(host, r.host)]


def text_words(rules: Iterable[Rule]) -> dict:
    """{host: [words in lower case]} for the hosts whose rules read the page's words: the probe
    reads the page's text only on one of these."""
    out: dict = {}
    for rule in rules:
        if rule.text:
            out.setdefault(rule.host, []).extend(w.lower() for w in rule.text)
    return out


def wants_text(url: str, rules: Iterable[Rule]) -> bool:
    return any(r.text for r in rules_for(_host(url), rules))


def _verdict(found: list, url: str, kind: str = "rule") -> Verdict:
    parts = urlsplit(url)
    first, rest = found[0], found[1:]
    trigger = first + (f" (also: {', '.join(rest)})" if rest else "")
    return Verdict(trigger=trigger, url=url, host=(parts.hostname or "").lower(), path=parts.path,
                   kind=kind)


def url_verdict(url: Optional[str], rules: Iterable[Rule]) -> Optional[Verdict]:
    """A rule's URL part fired on this address, or None. Only http(s) pages: about:blank, a data
    URL or chrome:// are no site's verification page."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    found: list = []
    lead = ""
    for rule in rules_for(host, rules):
        hits: list = []
        path_for_match = parts.path[:URL_MATCH_LENGTH]
        target_for_match = (parts.path + (f"?{parts.query}" if parts.query else ""))[:URL_MATCH_LENGTH]
        for pattern in rule.path:
            if re.search(pattern, path_for_match):
                hits.append(f"path {cut(parts.path, 60)}")
                break
        if rule.query:
            params = parse_qsl(parts.query, keep_blank_values=True)
            for name, prefix in rule.query:
                value = next((v for k, v in params if k == name and v.startswith(prefix)), None)
                if value is not None:
                    hits.append(f"query {name}={cut(value, 40)}")
        if rule.url:
            for pattern in rule.url:
                if re.search(pattern, target_for_match):
                    hits.append(f"url pattern {pattern!r}")
        if hits:
            if not found:
                lead = f"{rule.host} {rule.source}, "
                hits[0] = lead + hits[0]
            found.extend(hits)
    return _verdict(found, url) if found else None


def text_verdict(url: str, words: Iterable[str], rules: Iterable[Rule]) -> Optional[Verdict]:
    """The page shows a word a rule for its host names (the probe found `words`), or None."""
    seen = {w.lower() for w in words or ()}
    if not seen:
        return None
    for rule in rules_for(_host(url), rules):
        hits = [w for w in rule.text if w.lower() in seen]
        if hits:
            found = [f"{rule.host} {rule.source}, page text {hits[0]!r}"]
            found += [f"page text {w!r}" for w in hits[1:]]
            return _verdict(found, url)
    return None


def cloudflare_verdict(url: str, mark: str, header: Optional[str]) -> Verdict:
    """A Cloudflare challenge page, PRESENT (its widget not solved), named by what showed it."""
    marks = [mark] + (["response header cf-mitigated: challenge"] if header == "challenge" else [])
    return _verdict([f"a Cloudflare challenge page ({', '.join(marks)})"], url, kind="cloudflare")


# ── What a result says ────────────────────────────────────────────────────────────────────────

def place(url: Optional[str]) -> str:
    """Host and path of a page, its query cut with a mark saying how much and where the whole URL
    is, as a workflow's previews cut a long result. The fragment is left out."""
    if not url:
        return "(unknown)"
    try:
        parts = urlsplit(url)
    except ValueError:
        return cut(url, PATH_SHOWN)
    if parts.scheme not in ("http", "https"):
        return cut(url, PATH_SHOWN)
    text = (parts.hostname or "") + (f":{parts.port}" if parts.port else "")
    path = parts.path or "/"
    text += path if len(path) <= PATH_SHOWN else f"{path[:PATH_SHOWN]}... [path cut: +{len(path) - PATH_SHOWN} chars]"
    query = parts.query
    if query:
        if len(query) <= QUERY_SHOWN:
            text += f"?{query}"
        else:
            head = query[:QUERY_SHOWN]
            at = head.rfind("&")
            if at > 0:  # whole parameters: a cut name or value reads as the real one
                head = head[:at]
            elif "=" in head:  # one long first value (a tracking id): its name and a little of it
                name, _, value = head.partition("=")
                head = f"{name}={value[:8]}"
            text += (f"?{head}... [query cut: +{len(query) - len(head)} chars; "
                     "get_url gives it whole]")
    if parts.fragment:
        text += " [#fragment left out]"
    return text


def next_line(where: str, again: str, cloudflare: bool) -> str:
    """What to do, by where the request is: "bar" (the overlay's bar shows it), "window" (headed,
    no overlay), "pending" (a request made with request_human_help is pending) or "headless"."""
    wait = {
        "bar": ("the person was asked in the overlay's bar (Done); the window was not brought "
                "forward. Call wait_for_human; then "),
        "window": ("no overlay shows the person a request: request_human_help brings the window "
                   "forward and asks them, then wait_for_human; then "),
        "pending": ("a request made with request_human_help is pending on this tab and was left "
                    "as it is. Call wait_for_human; then "),
        "agent_pending": ("an explicit request made with request_human_help is still pending in "
                          "the overlay's bar. Call wait_for_human; then "),
    }
    # Headless: on a visible browser the tripwire asks by itself at its next look at this page.
    line = wait.get(where, "this browser is headless, so nobody sees a request: relaunch it "
                           "visible (browser_close, browser_launch(headless=False) on this "
                           "profile), then ") + again
    if where not in wait:
        line += ", which asks the person"
    line += ". Open no more pages of this site until then."
    if cloudflare:
        line = ("the person can pass it (wait_for_human), or, where the user is entitled to it, "
                "solve_turnstile_oscursor() (it takes over the mouse: tell the user first). "
                + line[0].upper() + line[1:])
    return line


def pause_lines(verdict: Verdict, asked_for: Optional[str], next_text: str,
                auto_verify: str = "") -> str:
    """The lines after a pause's first one: trigger, page, asked for, what auto_verify did (a
    Cloudflare page: its own words on the tools that fit, kept), next."""
    lines = [f"trigger: {verdict.trigger}", f"page: {place(verdict.url)}"]
    if asked_for:
        lines.append(f"asked for: {place(asked_for)}")
    if auto_verify:
        lines.append(f"auto_verify: {auto_verify}")
    lines.append(f"next: {next_text}")
    return "\n".join(lines)


def warning_line(verdict: Verdict, repeat: bool) -> str:
    if repeat:
        return f"{WARNING}: still on the verification page reported before ({verdict.place})."
    return (f"{WARNING}: verification page ({verdict.trigger}) at {place(verdict.url)}; "
            "antibot_tripwire=warn, nothing paused.")


def action_line(verdict: Verdict, where: str) -> str:
    """The first line of a click or key that led to a verification page (pause mode): the action
    did happen, so no error, and what was done about the page."""
    asked = {
        "bar": "the person was asked in the overlay's bar (Done): call wait_for_human before the "
               "next step",
        "window": "no overlay shows the person a request: request_human_help brings the window "
                  "forward, then wait_for_human",
        "pending": "a request made with request_human_help is pending: wait_for_human",
    }.get(where, "this browser is headless, so nobody sees a request")
    return (f"{WARNING}: this led to a verification page ({verdict.trigger}) at "
            f"{place(verdict.url)}; {asked}. Open no more pages of this site until then.")


def with_first_line(result: str, line: str) -> str:
    """`line` first, before the tool's own result. An error keeps "Error: " first, the line right
    after it, so that counting failures by that prefix still counts it."""
    if result.startswith("Error: "):
        return f"Error: {line}\n{result[len('Error: '):]}"
    return f"{line}\n{result}"


def details(text: str) -> dict:
    """A pause's lines back as {trigger, page, asked_for, auto_verify, next}, for a workflow's
    report."""
    out = {}
    for line in text.splitlines()[1:]:
        name, sep, value = line.partition(": ")
        if sep and name in ("trigger", "page", "asked for", "auto_verify", "next"):
            out[name.replace(" ", "_")] = value
    return out
