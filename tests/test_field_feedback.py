"""Field reports from a checkout run, the parts that need no browser.

The browser parts are in test_field_feedback_e2e.py. Each test names the report it answers.
"""
import json

import pytest

from mcp_stealth_chrome import helpers, profile_gc

# A token vault's reply, shaped like the one in the report, with synthetic values.
VAULT = {
    "gatewayTokens": [{
        "id": "gt_7f3a9c2e41b84d0f", "token": "4111110000001111", "value": "tok_live_9aZx81Kq",
        "cardScheme": "VISA", "fundingSource": "CREDIT", "ownerType": "CONSUMER", "panType": "FPAN",
        "cardExpiryMonth": "12", "cardExpiryYear": 2028, "issuerName": "Test Bank AG",
        "issuerCountryCode": "DE", "lastSuccessfulPurchaseDate": "2026-11-13T09:41:07Z",
        "maskedPan": "411111******1111", "last4": "1111", "cardNumber": "4111111111111111",
        "securityCode": "123", "cvc": "123", "reference": "ORDER-2026-0042",
        "alias": "kJhGfDsAqWeRtYuIoP", "type": "aZ9xQ2", "name": "bG9uZ3NlY3JldA", "keyId": "k1",
        "expiresIn": 3600, "networkTokenRef": "ntr_abc", "accessToken": "eyJhbGciOi.payload.sig",
        "authorization": "Bearer SYNTHAUTH", "cryptogram": "AAABBBCCC",
        "nested": {"pan": "4000000000000002", "status": "ACTIVE"},
    }],
    "schemeTokens": [], "pazeExternalSchemeTokens": None,
}
SECRETS = ("gt_7f3a9c2e41b84d0f", "4111110000001111", "tok_live_9aZx81Kq", "411111******1111",
           "4111111111111111", "ORDER-2026-0042", "kJhGfDsAqWeRtYuIoP", "aZ9xQ2", "bG9uZ3NlY3JldA",
           "ntr_abc", "eyJhbGciOi", "SYNTHAUTH", "AAABBBCCC", "4000000000000002")


def _masked(doc):
    return json.loads(helpers.mask_body(json.dumps(doc)))


# What a review found the narrowed rule let through under a secret parent; the default must hide
# every one of them, as origin/main did.
REVIEW_PROBES = {"tokens": {
    "name": "ABCDEF", "label": "correct horse battery staple", "title": "Recovery Phrase Words",
    "status": "ACTIVE", "securityEnabled": True, "birthDate": "1990-01-01",
    "securityCountry": "USA", "securityCurrency": "USD", "count": 1234, "size": 123,
    "labels": ["ABCDEF", "correct horse battery staple"], "NaMe": "ABCDEF",
    "cardholderName": "Budi Santoso", "issuerName": "Test Bank AG",
}}


def test_4_the_default_hides_everything_under_a_secret_key():
    """The narrowed rule leaked short keys, passphrases, 4-digit codes and PII: reverted."""
    out = _masked(REVIEW_PROBES)["tokens"]
    for key, value in REVIEW_PROBES["tokens"].items():
        if isinstance(value, bool):
            assert out[key] is value
        elif isinstance(value, list):
            assert all(str(v).startswith("***") for v in out[key]), key
        else:
            assert str(out[key]).startswith("***"), (key, out[key])
    card = _masked(VAULT)["gatewayTokens"][0]
    assert all(v is None or str(v).startswith("***") for k, v in card.items() if k != "nested")
    text = json.dumps(_masked(VAULT))
    for secret in SECRETS + ("VISA", "Test Bank AG", "2026-11-13T09:41:07Z"):
        assert secret not in text, secret


def _body_mode(doc):
    return json.loads(helpers.mask_body(json.dumps(doc), own_keys_only=True))


def test_4_body_mode_shows_values_under_a_secret_parent_but_not_secret_named_ones():
    """reveal_values="body": the caller asked to read a token vault's card metadata."""
    card = _body_mode(VAULT)["gatewayTokens"][0]
    assert card["cardScheme"] == "VISA" and card["issuerName"] == "Test Bank AG"
    assert card["lastSuccessfulPurchaseDate"] == "2026-11-13T09:41:07Z"
    assert card["cardExpiryMonth"] == "12" and card["id"] == "gt_7f3a9c2e41b84d0f"
    for key in ("token", "accessToken", "authorization"):
        assert card[key].startswith("***"), key
    for secret in ("4111110000001111", "eyJhbGciOi", "SYNTHAUTH"):
        assert secret not in json.dumps(card), secret
    # A value whose own key is secret-named stays hidden at any depth, list items included.
    out = _body_mode({"a": {"password": "P1", "pin": 1234, "otp": ["111111"], "cvc": "123",
                            "nested": [{"apiKey": "K1"}], "ok": True}})["a"]
    assert out["password"].startswith("***") and out["pin"].startswith("***")
    assert out["otp"] == ["*** (6 chars)"] and out["cvc"].startswith("***")
    assert out["nested"][0]["apiKey"].startswith("***") and out["ok"] is True
    # A form body is masked by its own names in both modes.
    assert helpers.mask_body("user=budi&password=P1", own_keys_only=True) == "user=budi&password=***"


def test_4_body_mode_keeps_credential_headers_and_url_secrets_masked():
    entry = {"url": "https://a.test/cb?code=SYNTHCODE&page=2",
             "request_headers": {"Authorization": "Bearer SYNTHAUTH", "Cookie": "sid=SYNTHSID",
                                 "X-Api-Key": "SYNTHKEY", "Proxy-Authorization": "Basic SYNTHPROXY",
                                 "Accept": "application/json"},
             "response_headers": {"Set-Cookie": "sid=SYNTHSETCOOKIE"},
             "request_body": json.dumps({"tokens": {"type": "card", "token": "SYNTHTOKEN"}})}
    out = helpers.mask_request_entry(entry, body_mode=True)
    text = json.dumps(out)
    for secret in ("SYNTHCODE", "SYNTHAUTH", "SYNTHSID", "SYNTHKEY", "SYNTHPROXY",
                   "SYNTHSETCOOKIE", "SYNTHTOKEN"):
        assert secret not in text, secret
    assert "page=2" in out["url"] and '"type": "card"' in out["request_body"]
    assert out["request_headers"]["Accept"] == "application/json"


def test_4_reveal_mode_reads_only_true_false_and_body():
    assert helpers.reveal_mode(False) == helpers.reveal_mode(None) == "none"
    assert helpers.reveal_mode(True) == "all" and helpers.reveal_mode("body") == "body"
    with pytest.raises(ValueError):
        helpers.reveal_mode("headers")


def test_4_rules_outside_a_secret_key_are_unchanged():
    out = _masked({"user": "budi", "password": "P1", "nested": [{"token": "T1", "keep": 1}],
                   "session": {"id": 5, "note": "n"}, "pin": 1234, "auth": True,
                   "credentials": ["S2", {"x": "S3"}], "token": {"value": "S1"}})
    assert out["user"] == "budi" and out["nested"][0]["keep"] == 1
    assert out["session"] == {"id": 5, "note": "n"}
    text = json.dumps(out)
    assert not any(s in text for s in ("P1", "T1", "S1", "S2", "S3")) and "1234" not in text


def test_4_credential_headers_stay_masked():
    headers = helpers.mask_headers({"Authorization": "Bearer SYNTHAUTH", "Cookie": "sid=SYNTHSID",
                                    "X-Api-Key": "SYNTHKEY", "Accept": "application/json"})
    assert "SYNTH" not in json.dumps(headers) and headers["Accept"] == "application/json"


def test_7_a_long_url_is_cut_saying_how_much_and_how_to_get_it():
    url = "https://pay.test/frame?config=" + "A" * 3000 + "&signature=xyz"
    out = helpers.cut_url({"url": url, "id": 1}, 200)
    assert len(out["url"]) == 201 and out["url"].endswith("…")
    assert out["url_cut"].startswith(f"{len(url) - 200} of {len(url)} chars cut")
    assert "max_url_len=0" in out["url_cut"]
    assert helpers.cut_url({"url": url}, 0) == {"url": url}
    assert helpers.cut_url({"url": "https://a.test/"}, 200) == {"url": "https://a.test/"}


def test_7_network_get_never_cuts_its_json_silently():
    from mcp_stealth_chrome import server
    entries = [{"url": f"https://a.test/{i}", "body": "x" * 900} for i in range(200)]
    text = server._bounded_entries(entries)
    doc = json.loads(text)  # the old text slice did not parse
    assert len(text) <= server._NETWORK_GET_MAX_CHARS
    assert doc["omitted"] == 200 - len(doc["entries"]) > 0
    assert doc["entries"][-1]["url"] == "https://a.test/199"  # the newest are kept
    assert "smaller limit" in doc["note"]
    small = [{"url": "https://a.test/"}]
    assert json.loads(server._bounded_entries(small)) == small


def test_10_server_status_counts_a_long_profile_list():
    rows = [{"name": f"profile-pid{n}", "why": "holds data", "bytes": 2**20} for n in range(47)]
    saved = dict(profile_gc._LAST)
    try:
        with profile_gc._LOCK:
            profile_gc._LAST.clear()
            profile_gc._LAST.update({"state": "done", "removed": rows[:2], "kept_with_data": rows,
                                     "skipped_in_use": [], "skipped": [], "errors": []})
        status = profile_gc.status()
    finally:
        with profile_gc._LOCK:
            profile_gc._LAST.clear()
            profile_gc._LAST.update(saved)
    assert [r["name"] for r in status["kept_with_data"]] == [f"profile-pid{n}" for n in range(5)]
    more = status["kept_with_data_more"]
    assert more["count"] == 47 and more["bytes"] == 47 * 2**20 and "profile_report" in more["note"]
    assert status["removed"] == rows[:2] and "removed_more" not in status
    assert len(json.dumps(status)) < 2000


def test_8_full_page_note_takes_the_scroll_off_both_axes(monkeypatch):
    """Review 4: the full-page formula subtracted the scroll from y only."""
    import asyncio
    from mcp_stealth_chrome import image_output
    from mcp_stealth_chrome.tools import page

    async def ctx(_tab, timeout=None):
        return {"dpr": 2, "pageLeft": 300, "pageTop": 50}

    monkeypatch.setattr(image_output, "viewport_context", ctx)
    note = asyncio.run(page._coordinate_note(object(), 1.0, (2000, 4000), None, True))[0]
    assert "x=image_x × 0.5 − 300" in note and "y=image_y × 0.5 − 50" in note, note
    note = asyncio.run(page._coordinate_note(object(), 1.0, (2000, 4000), None, False))[0]
    assert "mouse_click_xy(x=image_x × 0.5, y=image_y × 0.5)" in note, note


def test_3r_batch_aliases_pass_every_valid_argument_on():
    """Review 3: batch "wait" dropped frame_url and "navigate" dropped expect_url."""
    from mcp_stealth_chrome import server
    server._register_workflow_tools()
    tool, args = server._batch_action_call({"type": "wait", "frame_url": "/3ds", "timeout": 10})
    assert (tool, args) == ("wait_for", {"selector": None, "text": None, "timeout": 10,
                                         "frame_url": "/3ds"})
    tool, args = server._batch_action_call({"type": "navigate", "url": "https://a.test/",
                                            "expect_url": "a.test", "expect_status": 200})
    assert args == {"url": "https://a.test/", "expect_url": "a.test", "expect_status": 200}
    assert server._batch_action_call({"type": "press"}) == ("press_key", {"key": "Enter"})
    assert server._batch_action_call({"type": "click", "ref": "e3", "stop_on_error": True}) == (
        "click", {"ref": "e3", "selector": None, "humanize": False})


def test_3r_an_unknown_inner_argument_is_refused_by_name_and_counted():
    import asyncio
    from mcp_stealth_chrome import server
    out = asyncio.run(server.batch_actions([
        {"type": "screenshot", "path": "x.jpg"},
        {"type": "wait", "frame_url": "/3ds", "bogus": 1},
    ]))
    assert out.startswith("2 actions, 2 failed"), out
    assert "screenshot does not take 'path'; nothing was done" in out
    assert "wait_for does not take 'bogus'" in out
    result = asyncio.run(server.workflow_run([{"tool": "screenshot", "args": {"path": "x.jpg"}}]))
    assert result.startswith("Error:") and "screenshot does not take 'path'" in result, result
    doc = json.loads(result[len("Error: "):])
    assert doc["failed_at"] == 0


def test_r2_1_the_body_warning_survives_a_cut_for_size():
    """Review 2.1: the size note replaced the reveal_values="body" warning."""
    from mcp_stealth_chrome import server
    entries = [{"url": f"https://a.test/{i}", "body": "x" * 900} for i in range(200)]
    text = server._bounded_entries(entries, server._BODY_MODE_EXTRA)
    doc = json.loads(text)
    assert doc["reveal_note"] == helpers.BODY_REVEAL_NOTE and doc["reveal_values"] == "body"
    assert doc["omitted"] > 0 and "smaller limit" in doc["note"]
    assert len(text) <= server._NETWORK_GET_MAX_CHARS
    # The whole envelope counts toward the limit, however large what rides beside entries is.
    big = server._bounded_entries(entries, {"reveal_values": "body", "reveal_note": "n" * 3000})
    assert len(big) <= server._NETWORK_GET_MAX_CHARS, len(big)


def test_r2_2_offsets_stay_when_the_scale_is_exactly_one(monkeypatch):
    """Review 2.2: DPR 2 with a 0.5 resize, or DPR 1 with none, dropped the offsets."""
    import asyncio
    from mcp_stealth_chrome import image_output
    from mcp_stealth_chrome.tools import page

    def with_ctx(dpr):
        async def ctx(_tab, timeout=None):
            return {"dpr": dpr, "pageLeft": 300, "pageTop": 50}
        monkeypatch.setattr(image_output, "viewport_context", ctx)

    with_ctx(2)
    note = asyncio.run(page._coordinate_note(object(), 0.5, (1280, 4000), None, True))[0]
    assert "x=image_x × 1 − 300" in note and "y=image_y × 1 − 50" in note, note
    with_ctx(1)
    note = asyncio.run(page._coordinate_note(object(), 1.0, (1280, 4000), None, True))
    assert note and "− 300" in note[0], note
    note = asyncio.run(page._coordinate_note(object(), 1.0, (200, 100), {"x": 40, "y": 60}, False))
    assert note and "x=40 + image_x × 1" in note[0], note
    assert asyncio.run(page._coordinate_note(object(), 1.0, (1280, 800), None, False)) == []


def test_r2_3_a_network_get_running_across_a_restart_returns_no_old_body(monkeypatch):
    """Review 2.3: a network_get waiting on getResponseBody returned an old body after a
    network_start had cleared the capture."""
    import asyncio
    from mcp_stealth_chrome import server

    class Tab:
        target = None

        def __init__(self):
            self.release = asyncio.Event()

        async def send(self, _cmd):
            await self.release.wait()
            return ("OLD-BODY-SYNTH", False)

    async def scenario():
        tab = Tab()
        monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
        saved = (server.BrowserState.network_index, server.BrowserState.capture_network)
        server.BrowserState.network_index = {"1.1": {"request_id": "1.1", "url": "https://a.test/x"}}
        server.BrowserState.capture_network = True
        try:
            call = asyncio.ensure_future(server.network_get(include_body=True))
            await asyncio.sleep(0.05)
            server._network_restarted()  # what network_start does before clearing
            server.BrowserState.network_index.clear()
            tab.release.set()
            return await call
        finally:
            server.BrowserState.network_index, server.BrowserState.capture_network = saved

    out = asyncio.run(scenario())
    assert "OLD-BODY-SYNTH" not in out and out.startswith("Error:"), out
    assert "restarted while" in out


def test_r2_3_network_start_marks_a_restart_and_a_plain_start_sends_what_main_sent(monkeypatch):
    import asyncio
    from mcp_stealth_chrome import server

    class _Target:
        target_id = "TARGET-FF"

    class _Tab:
        target = _Target()

        def __init__(self):
            self.sent = []

        async def send(self, command):
            request = next(command)
            self.sent.append((request["method"], request.get("params", {})))
            return None

        def add_handler(self, *_a):
            pass

    tab = _Tab()
    monkeypatch.setattr(server.BrowserState, "active_tab", classmethod(lambda cls: tab))
    saved = (server.BrowserState.network_logs, server.BrowserState.network_index,
             server.BrowserState.capture_network)
    try:
        before = server._NETWORK_GENERATION[0]
        out = asyncio.run(server.network_start())
        assert server._NETWORK_GENERATION[0] == before + 1
        assert tab.sent == [("Network.enable", {})], tab.sent  # exactly origin/main
        assert "not kept past a navigation" in out and "keep_bodies=True" in out
        tab.sent.clear()
        out = asyncio.run(server.network_start(keep_bodies=True))
        assert [m for m, _ in tab.sent] == ["Network.enable", "Network.configureDurableMessages"]
        assert "until it closes" in out
        tab.sent.clear()
        asyncio.run(server.network_start())
        assert tab.sent == [("Network.enable", {}), ("Network.configureDurableMessages", {})]
    finally:
        (server.BrowserState.network_logs, server.BrowserState.network_index,
         server.BrowserState.capture_network) = saved
        server._NETWORK_ARMED_TAB_IDS.discard("TARGET-FF")
        server._NETWORK_KEPT_BODIES.discard("TARGET-FF")
        server._NETWORK_CAPTURE_BODIES.pop("TARGET-FF", None)
