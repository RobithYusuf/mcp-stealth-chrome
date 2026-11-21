---
name: stealth-chrome
description: Use the mcp-stealth-chrome MCP server (tools named mcp__stealth-chrome__* or mcp__<your server name>__*) when a task needs a browser that gets past bot checks - Cloudflare or other anti-bot pages, CAPTCHAs (reCAPTCHA, Turnstile, hCaptcha), sites that block automation, checkouts with 3-D Secure frames, or the agent's own logged-in browser profile - or when the user says stealth, stealth-chrome or /stealth-chrome. Read it before the first stealth-chrome tool call. For ordinary browsing another browser skill may fit better.
---

# stealth-chrome

A real Chrome (or Brave) driven over CDP, with a browserless HTTP client that has a real Chrome TLS fingerprint. Every tool result says what actually happened. Read the result before the next step: it is how you avoid acting on a page that did not change.

## Start

1. `server_status()` once per session: the version, `settings` (each with its source), `agent_overlay`, and whether a browser runs.
2. `browser_launch(...)`:
   - Default (`persistent` unset) opens the agent's own profile, where its logins live. Add `profile_conflict="fail"` whenever those logins matter: another Chrome holding the profile then refuses the launch instead of opening an empty fallback.
   - `persistent=False` for a clean, throwaway profile (tests, demos, one-off reads). `privacy=True` keeps nothing at all.
   - Keep `headless=False` on protected sites (they detect headless). The window opens behind the app in use and does not take the keyboard.
   - `browser="brave"` for Brave with its Shields. `keep_alive=True` leaves it running when the session ends.
   - A result that starts with `WARNING: EMPTY FALLBACK PROFILE` means your logins are not there. Stop and tell the user; never log in on the fallback.
3. `browser_close()` when done (`close_all_instances()` for every instance).

## Read before you act

- Text first: `get_text()`, `get_text(selector=...)`, `describe_page(wait_stable=True)`. Use `screenshot()` only when layout or an image matters.
- `browser_snapshot(...)` lists interactive elements with refs (`e0`, `e1`, ...). Every snapshot replaces all refs. Narrow it with `name_contains`, `role`, `container_selector`, `max_elements`.
- Snapshots reach into iframes (cross-origin too, marked `frame=fN`) and closed shadow roots. Hosted card fields and 3-D Secure challenges are filled by ref.
- `frame_evaluate(frame_url_pattern, expression)` runs JS in a cross-origin frame. `list_frames()` lists them. `wait_for(frame_url=...)` waits for a frame such as a 3-D Secure challenge.
- "Nothing matched ... not rendered yet" means a single-page app has not drawn yet: `wait_for(text=...)` or `wait_for(selector=...)`, then snapshot again.

## Act

- Prefer `click(ref=...)`; `click_text(text)` when there is no ref. `mouse_click_xy(x, y)` for canvas and positions inside widgets (viewport CSS pixels).
- `fill(ref=..., value=...)` types real keys and reads the field back. "the page formatted it as ..." is fine (a card mask added spaces).
- `select_option`, `check`, `type_text`, `press_key`, `scroll` (real wheel events), `fill_form`.
- Add `humanize=True` on bot-sensitive pages.

## Research across many pages

Read a whole page per call, not one element per call. On a marketplace price check, agents that clicked and read one step at a time needed 34 to 82 calls where a scripted browser needed 11 to 19, and read a fraction of the listings.

- `extract_structured(container_selector, fields)` reads every card on a search or list page in one call: title, price, link (absolute) per card. Look at one card with `browser_snapshot` or `get_text` first to pick the selectors.
- `scrape_page(return_mode="markdown")` reads the open page's main content; `get_links()` lists its links.
- `smart_fetch(url)` reads a URL over HTTP with a Chrome fingerprint, without moving the tab, and uses the browser only when the site blocks it. `scrape_markdown(url)` is the HTTP-only form; `scrape_markdown(url, use_browser=True)` renders a JavaScript-heavy page in the tab.
- `workflow_run(steps)` runs many tool steps in one call, with `foreach` loops and earlier results (`save_as`, `${name}`): navigate to each search URL and `extract_structured` it, in one call. `batch_actions(actions)` runs a flat list.
- In a `foreach`, use `save_as: "pages[]"` to collect every parsed page result, then read `${pages[0].field}`. Vars above 200,000 characters are reported as large; successful collections above 1,000,000 characters are saved to a private export file, while failed runs retain vars whole for resume.
- Pace the pages in a workflow with a random pause step, `{"tool": "sleep", "args": {"min": 6, "max": 12}}`, and keep to one tab. It returns a summary by default; `output="full"` lists the steps up to 200 and counts the rest in `completed_omitted`.
- Sort a search by price where the site allows it, read the cards in one call, and open only the few listings you need to verify (variant, seller, stock).
- `web_search(query)` searches without a browser first; when that comes back empty it retries through the browser only if one is running. An empty result's `browser_escalation` says whether that happened. With no browser running, `browser_launch` first. `engine="brave"` needs a running browser.

## Login walls and warm profiles

- A site can lock its search behind a login while its product or article pages stay open. Find those pages with `web_search(...)` and read them directly. Do not log in to get around a wall unless the user asked you to.
- The default profile keeps its cookies between sessions, and a site that walled a fresh profile may open for it later (seen with a marketplace search). `persistent=False` starts cold every time.

## What results mean

| Result says | Do this |
|---|---|
| `clicked but NO page change detected within 450ms` | The click landed, but no page change was observed in the verification window. Read the page before clicking again; a later reaction is still possible. |
| `NOT clicked ... obscured by <x>` | Something covers the target. Close the cookie banner or modal named, then retry. |
| `NOT clicked ... pointer-events:none` | You hit a label inside a button. Click the button itself (its ref, or its position). |
| `the first text match was unreachable (...), so a reachable element with the same text was clicked` | Another element than the first match was clicked. Check it was the one you meant. |
| `the person took over this browser` | The person pressed Take over. Stop, say so in chat, and `wait_for_human()` or ask them. Do not carry on in another browser or over HTTP. |
| `[agent_overlay: the person holds this browser ...]` | A tool that works without the browser ran while they hold it. Go on only with what they asked for. |
| `[agent_overlay: the person pressed Done on the request for help ...]` | They finished what you asked. Read the page and go on; ask in chat if unsure. |
| `[agent_overlay: while the agent was idle the person clicked ...]` | They used the page while you were away. Read it again before acting. |
| `[agent_overlay: the person typed in the browser ...]` | Their keys reached the page too. Re-read the field before going on. |
| `auto-verify: ... challenge gone` | A Cloudflare widget was handled during the load. |
| `antibot_paused: ...` (or `status: antibot_paused` in a workflow) | The site answered with a verification page, and the run stopped there. Open no more pages of that site. Tell the user, `wait_for_human()` while the bar shows the request (a headless browser cannot show one: say so), then run `resume_with` or navigate again. `trigger:` says what fired, `get_url` gives the whole URL. Solve it only where the user is entitled to (see Bot checks). |
| `antibot_warning: ...` | The same, with the `antibot_tripwire` setting on warn, or after a click: nothing was stopped. Do not go on opening pages of that site. |

## Working with the person

With the overlay on (macOS, visible browser) the person sees a light, a bar and a lock while you work, and can press **Take over**. Tools are refused while they hold the browser.

- `request_human_help(message)` shows your request in the bar with a **Done** button; then `wait_for_human()`. Use it for anything a person should do: logging in with 2FA, a CAPTCHA you should not solve, confirming a payment.
- The person changes the overlay from its **...** menu or `mcp-stealth-chrome settings`. You do not need to.

## Bot checks and CAPTCHAs

Solve a challenge only where the user is entitled to: their own site or test keys, a demo page, or a site they asked you to use with a check they want you to pass. When in doubt, hand it to the person with `request_human_help`.

- **Cloudflare / Turnstile:** `browser_launch` and `navigate` handle the widget themselves (`auto_verify`). For a full-page "Just a moment..." challenge, `solve_turnstile_oscursor()` clicks with the real OS cursor; it takes over the mouse for up to 50 s, so tell the user first.
- **reCAPTCHA v2 (checkbox and image grid):** `solve_recaptcha_ai()`. It needs a vision-capable model that the user brings (BYOK): `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_MODEL` (any OpenAI-compatible provider) or `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` in the server's environment, or `provider`, `base_url`, `api_key`, `model` passed in the call. On "No vision provider configured", ask the user which provider to use. Never invent or guess a key, and never repeat one back. A checked box is not proof: confirm the page accepted it (its own success message or result).
- **reCAPTCHA v3:** nothing to click or solve. It scores the session from 0 to 1 by profile, IP and behaviour. A warm persistent profile scores better than a new throwaway one.
- **hCaptcha:** there is no built-in solver, and hCaptcha's terms forbid bots and AI that bypass its challenges. Use `request_human_help`. `solve_captcha(kind="hcaptcha", ...)` exists only for a solving service the user has a key for (`CAPSOLVER_KEY`).
- `detect_anti_bot()` names the protection on a page and the tools that fit it.

## Rules

- Do not log in, post, like, follow, comment, buy or submit real personal data on the user's accounts unless the user asked for that exact action.
- Never print cookie values, tokens or API keys. `cookie_list()` masks values unless `reveal_values=True`; use that only when the value itself is needed.
- One page at a time: read the result, then decide. Do not chain clicks on coordinates measured before the page changed.
- When a tool refuses, the reason is in its text. Fix the cause it names instead of repeating the call.
