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


def test_4_harmless_fields_under_a_secret_key_stay_readable():
    """Report 4: every field under gatewayTokens read "*** (N chars)", the date included."""
    card = _masked(VAULT)["gatewayTokens"][0]
    assert card["cardScheme"] == "VISA" and card["fundingSource"] == "CREDIT"
    assert card["ownerType"] == "CONSUMER" and card["panType"] == "FPAN"
    assert card["cardExpiryMonth"] == "12" and card["cardExpiryYear"] == 2028
    assert card["issuerName"] == "Test Bank AG" and card["issuerCountryCode"] == "DE"
    assert card["lastSuccessfulPurchaseDate"] == "2026-11-13T09:41:07Z"
    assert card["nested"]["status"] == "ACTIVE"


def test_4_every_credential_under_a_secret_key_stays_hidden():
    out = _masked(VAULT)
    text = json.dumps(out)
    for secret in SECRETS:
        assert secret not in text, secret
    card = out["gatewayTokens"][0]
    for key in ("securityCode", "cvc", "last4", "expiresIn", "keyId", "id", "token", "value"):
        assert str(card[key]).startswith("***"), key
    # Shapes that look harmless stay hidden under a name that does not describe.
    assert _masked({"tokens": [{"value": "VISA"}, "plainword"]})["tokens"] == [
        {"value": "*** (4 chars)"}, "*** (9 chars)"]
    assert _masked({"token": {"data": 1234}})["token"]["data"].startswith("***")
    assert _masked({"token": {"securityCode": 123}})["token"]["securityCode"].startswith("***")
    assert _masked({"token": {"pinCode": "1234"}})["token"]["pinCode"].startswith("***")
    assert _masked({"token": {"type": "Bearer abc123"}})["token"]["type"].startswith("***")
    assert _masked({"token": {"name": "aBcDeFgHiJ"}})["token"]["name"].startswith("***")
    assert _masked({"token": {"month": "123456"}})["token"]["month"].startswith("***")
    # A secret name further down hides everything below it again, descriptive names included.
    inner = _masked({"tokens": [{"password": {"type": "Basic", "createdAt": "2026-01-01"}}]})
    assert inner["tokens"][0]["password"] == {"type": "*** (5 chars)", "createdAt": "*** (10 chars)"}


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


@pytest.mark.parametrize("name, value, shown", [
    ("cardExpiryMonth", "07", True), ("expiryYear", "2031", True), ("currency", "IDR", True),
    ("createdAt", "2026-01-02", True), ("brand", "MASTERCARD", True), ("issuerName", "Bank X", True),
    ("bankName", "Bank 9", False), ("countryCode", "123", False), ("label", "x" * 50, False),
    ("status", "ok!", False), ("type", None, True), ("verified", True, True),
])
def test_4_shape_rules(name, value, shown):
    out = _masked({"tokens": [{name: value}]})["tokens"][0][name]
    assert (out == value) is shown, out
