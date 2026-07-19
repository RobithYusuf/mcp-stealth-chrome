"""Exhaust the remaining no-auth Reddit avenues: RSS feeds + public redlib mirrors."""
import asyncio
import re
from mcp_stealth_chrome.tools.research import _fetch_http, _looks_blocked


def titles_from_rss(xml):
    # Atom <entry><title>..</title>  (Reddit RSS is Atom)
    return [re.sub(r"<[^>]+>", "", t).strip()
            for t in re.findall(r"<entry>.*?<title[^>]*>(.*?)</title>", xml, re.S)][:8]


async def main():
    targets = [
        ("RSS top", "https://www.reddit.com/r/ClaudeAI/top/.rss?t=month&limit=8"),
        ("RSS sub", "https://www.reddit.com/r/ClaudeAI/.rss"),
        ("RSS old", "https://old.reddit.com/r/ClaudeAI/.rss"),
        ("RSS search", "https://www.reddit.com/r/ClaudeAI/search.rss?q=subscription&restrict_sr=1&sort=top"),
        ("redlib catsarch", "https://redlib.catsarch.com/r/ClaudeAI/top?t=month"),
        ("redlib perennialte", "https://redlib.perennialte.ch/r/ClaudeAI"),
        ("redlib freedit", "https://libreddit.privacydev.net/r/ClaudeAI"),
    ]
    ok_any = False
    for label, url in targets:
        status, text, _ = await _fetch_http(url, timeout=18)
        blocked = _looks_blocked(status, text)
        info = ""
        if status == 200 and text and ("<entry>" in text or "<feed" in text):
            ts = titles_from_rss(text)
            if ts:
                info = f" | {len(ts)} RSS titles"
                ok_any = True
        elif status == 200 and text and "redlib" in url:
            # redlib HTML: post titles are <a class="post_title">
            hs = [re.sub(r"<[^>]+>", "", h).strip()
                  for h in re.findall(r'class="post_title"[^>]*>(.*?)</a>', text, re.S)][:8]
            if hs:
                info = f" | {len(hs)} redlib titles"
                ok_any = True
                text_titles = hs
        print(f"  [{label:16}] status={status} blocked={blocked} bytes={len(text)}{info}")
        if status == 200 and ("<entry>" in text):
            for t in titles_from_rss(text)[:6]:
                print(f"        • {t[:80]}")
        elif "redlib" in url and status == 200 and 'post_title' in text:
            for h in [re.sub(r"<[^>]+>", "", x).strip()
                      for x in re.findall(r'class="post_title"[^>]*>(.*?)</a>', text, re.S)][:6]:
                print(f"        • {h[:80]}")

    print("\nRESULT:", "REDDIT REACHABLE via at least one no-auth path ✅" if ok_any
          else "still blocked on every no-auth path in this sandbox ⛔")


asyncio.run(main())
