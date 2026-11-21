# The browserless HTTP path

Part of the agent guide: [AGENTS.md](../../AGENTS.md) says when to read this. Its rules bind like the rest of the guide. Add what you learn about this topic here, not to AGENTS.md.

- `curl_cffi` presents a real Chrome JA3/JA4, matching the browser this server drives. `http_request`,
  `scrape_markdown` and `smart_fetch` never launch a browser; `smart_fetch` escalates only when the
  page looks blocked or SPA-empty.
- `web_search` retries an empty HTTP search through the browser SERP only when a browser already runs (nothing but
  `browser_launch`/`spawn_browser` starts one). An empty result carries `browser_escalation`: tried and empty, not tried
  because no browser runs, or `use_browser='off'`. Without it, two empty searches right after a `/mcp` reconnect (no
  browser yet) read as "the browser SERP failed too".
- `export_curl` emits both a `curl` string and an `http_request(impersonate='chrome')` call, and
  recommends the second. Replaying a captured request with plain curl sends a curl TLS fingerprint —
  the request that worked in the browser then fails on the command line for reasons no header diff
  explains.
- Volatile headers (`Content-Length`, `Host`, `:authority`) are dropped rather than replayed stale;
  a mismatched Content-Length is itself a bot signal.
- `http_request` follows redirects itself (`allow_redirects=False` per hop, 30 at most, then curl's error 47 as
  before). curl carried every caller header to whatever host a redirect named: `X-Api-Key` reached a third party,
  and only Cookie and Authorization were dropped, by curl's own rule. Caller headers now go only to the first
  origin (scheme, host, effective port); `headers_not_forwarded` names what stayed behind and `redirects` lists
  the hops. Browser cookies go into the session's own jar before the first hop, host-only for the first host, each
  with its own path, Secure flag and expiry (`_seed_host_only`): sent to the same host on another port, never to
  another host, a `/admin` cookie never to `/public`, a Secure one never over plain http to a real host name (curl,
  like Chrome, treats `localhost`, `127.0.0.1` and `::1` as secure, so a test of that rule needs another name:
  `CURLOPT_RESOLVE` in `test_sweep_fixes.py`), and replaced by any cookie of that name a response sets. The snapshot
  was a name to value dict until 0.19.1, which seeded every cookie as path `/` without Secure, so a Secure cookie
  went out over http and a path-scoped one to every path. `localhost` and `::1` go through the jar too: given as a
  dict, curl_cffi named them `localhost.local` and curl never sent them, and the `Cookie` header that replaced it
  was dropped on a redirect to another port. `cookies_sent` is per request: the final one at the top, each
  redirect's on its hop (`_cookies_for`, curl's rules, checked against what the server received). Hosts are compared
  in canonical form (`_canonical_host`: lower case, no trailing dot, IDNA A-label by UTS 46): Chrome stores an IDN
  cookie under `xn--bcher-kva.example` and a URL written `bücher.example` found none; a host IDNA cannot encode
  matches nothing, and the jar is seeded under the A-label, which is the host curl sends. Never as the
  request's `cookies=`: curl_cffi up to 0.15 copied curl's whole
  store back into the jar after each response, so they reached later hops; from 0.16 it copies only what responses
  changed (`COOKIECHANGES`), and every redirect after the first went without them, while uv.lock (0.15) kept every
  suite green. Measured on 0.7.0, 0.7.4, 0.10.0, 0.13.0, 0.15.0, 0.16.3 and 0.17.0. Error 47 is the module's own
  `_TooManyRedirects`: curl_cffi 0.7 has no `TooManyRedirects`, and the ImportError read as "curl-cffi not installed",
  `not_executed`, after 31 requests. 303 (not HEAD) and 301/302 after
  a POST continue as a GET without the body; only http and https Locations are followed. A failure on a later hop
  reports the chain and `failed_at` (masked), and is never `not_executed`: the first server got the request.
