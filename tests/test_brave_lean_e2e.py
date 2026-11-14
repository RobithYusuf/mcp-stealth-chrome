"""brave_lean on real Brave, through the full server.

Needs MCP_E2E=1 and Brave installed (skipped otherwise, with the reason). Every server gets
MCP_STEALTH_DATA_DIR in a temporary folder, so every profile lives there.

What the running browser says is read from brave://settings, which a CDP evaluate can reach:
loadTimeData carries the feature switches (isLeoAssistantAllowed, isBraveVPNEnabled,
isSpeedreaderAllowed), the settings page's own handlers answer for Local State (getP3AEnabled,
getStatsUsagePingEnabled, brave_tor.isTorEnabled), and chrome.settingsPrivate answers for the
profile's preferences it lists. Toolbar buttons and the sidebar have no reading outside the
browser's own UI, so for those the preference is what is checked.

The network test leaves two fresh profiles idle side by side, one lean and one not, each writing
a net-log, and compares the hosts they contacted. It needs the network (Brave's own servers) and
takes MCP_LEAN_IDLE seconds (90 by default).

Shields, farbling, identity, cookie persistence, restore_tabs, vertical tabs and keep_alive
reattach are proved with lean on by test_brave_e2e.py, whose launches take the default.
"""
import json
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

import pytest

from _protocol import LOOPBACK_ONLY, Client
from mcp_stealth_chrome import brave, brave_lean, state

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MCP_E2E"), reason="needs a real browser"),
    pytest.mark.skipif(brave.find_brave() is None, reason="Brave is not installed"),
]
IDLE = float(os.environ.get("MCP_LEAN_IDLE", "90"))
# Hosts lean stops: P3A (star-randsrv.bsg, collector.bsg) and the daily usage ping.
STOPPED = {"usage-ping.brave.com", "star-randsrv.bsg.brave.com", "collector.bsg.brave.com"}

PROBE = """(async () => {
  const out = {url: location.href};
  const cr = await import('chrome://resources/js/cr.js');
  for (const m of ['getP3AEnabled', 'getStatsUsagePingEnabled', 'brave_tor.isTorEnabled'])
    out[m] = await Promise.race([cr.sendWithPromise(m), new Promise(r => setTimeout(() => r('timeout'), 5000))]);
  const ltd = (await import('chrome://resources/js/load_time_data.js')).loadTimeData;
  for (const k of ['isLeoAssistantAllowed', 'isBraveVPNEnabled', 'isSpeedreaderAllowed',
                   'isPlaylistFeatureEnabled', 'isBraveRewardsSupported', 'isBraveWalletAllowed'])
    out[k] = ltd.valueExists(k) ? ltd.getBoolean(k) : null;  // Linux builds have no VPN at all
  out.prefs = {};
  for (const k of %s) {
    const p = await new Promise(r => chrome.settingsPrivate.getPref(k, r));
    out.prefs[k] = p ? p.value : null;
  }
  return JSON.stringify(out);
})()"""
SETTINGS_PREFS = ["brave.wallet.default_wallet2", "brave.wallet.default_solana_wallet",
                  "brave.sidebar.sidebar_show_option", "brave.ai_chat.show_toolbar_button",
                  "brave.brave_ads.sponsored.enabled", "brave.brave_vpn.show_button",
                  "brave.rewards.show_brave_rewards_button_in_location_bar",
                  "brave.wallet.show_wallet_icon_on_toolbar", "brave.wayback_machine_enabled",
                  "brave.web_discovery_enabled", "brave.new_tab_page.show_stats",
                  "brave.new_tab_page.show_rewards", "brave.new_tab_page.show_brave_news",
                  "brave.new_tab_page.show_brave_vpn", "brave.new_tab_page.show_together",
                  "brave.today.should_show_toolbar_button"]


@pytest.fixture
def data():
    root = pathlib.Path(tempfile.mkdtemp(prefix="mcp-brave-lean-e2e-"))
    try:
        yield root
    finally:
        for pid, _command in state.processes_naming(root) or []:  # only this test's own folder
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.5)
        shutil.rmtree(root, ignore_errors=True)


def _client(data: pathlib.Path) -> Client:
    return Client(env={"MCP_STEALTH_DATA_DIR": str(data / "mcp")})


def _launch(client, **kw):
    kw.setdefault("headless", True)
    reply = client.call("browser_launch", _timeout=120, url="about:blank", browser="brave", **kw)
    assert not reply.failed, reply.text
    return reply


def _probe(client) -> dict:
    assert not client.call("navigate", url="brave://settings", _timeout=30).failed
    time.sleep(1.5)
    return json.loads(client.evaluate(PROBE % json.dumps(SETTINGS_PREFS), _timeout=40))


def _leo_page(client) -> str:
    client.call("navigate", url="brave://leo-ai", _timeout=30)
    time.sleep(1)
    return client.evaluate("location.href")


def test_lean_is_the_default_and_the_running_brave_says_so(data):
    profile = data / "mcp" / "brave-profile"
    client = _client(data)
    try:
        reply = _launch(client, extra_args=[LOOPBACK_ONLY])
        assert ("[brave_lean on (default for a profile this server made empty), turned off: Leo AI, "
                "VPN, Speedreader, Playlist (features)") in reply.text, reply.text
        assert (profile / brave_lean.PROVENANCE).is_file(), "the new profile is marked as ours"
        proof = brave_lean._registry_path(pathlib.Path(os.environ[brave_lean.REGISTRY_ENV]), profile)
        assert json.loads(proof.read_text())["root"] == os.path.realpath(profile), "proved outside it"
        assert "NOT applied" not in reply.text, reply.text
        seen = _probe(client)
        print("\nlean:", json.dumps(seen))
        assert seen["getP3AEnabled"] is False
        assert seen["getStatsUsagePingEnabled"] is False
        assert seen["brave_tor.isTorEnabled"] is False
        assert seen["isLeoAssistantAllowed"] is False
        assert seen["isBraveVPNEnabled"] in (False, None)  # None: a build without VPN (Linux)
        assert seen["isSpeedreaderAllowed"] is False
        assert seen["isPlaylistFeatureEnabled"] is False
        listed = {k: v for k, v in seen["prefs"].items() if v is not None}
        print("settingsPrivate listed:", sorted(listed), "not listed:", sorted(set(seen["prefs"]) - set(listed)))
        assert len(listed) >= 10, seen["prefs"]
        for key, value in listed.items():
            assert value == brave_lean.PREFERENCES[key], (key, value)
        assert _leo_page(client).startswith("chrome-error://"), "brave://leo-ai is gone"
        status = json.loads(client.call("server_status").text)["brave"]
        assert status["lean_running"]["enabled"] is True
        assert status["lean_running"]["features_disabled"] == list(brave_lean.FEATURES)
        assert status["lean"]["features_disabled"] == list(brave_lean.FEATURES)
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        # What the profile holds after the close: nothing Brave reset.
        assert brave_lean.read_back(profile, []) == {**brave_lean.LOCAL_STATE, **brave_lean.PREFERENCES}

        again = _launch(client, brave_lean=False, extra_args=[LOOPBACK_ONLY])
        assert "[brave_lean=False: Brave as it ships; put back" in again.text, again.text
        plain = _probe(client)
        print("not lean:", json.dumps(plain))
        assert plain["getP3AEnabled"] is True and plain["getStatsUsagePingEnabled"] is True
        assert plain["brave_tor.isTorEnabled"] is True
        assert plain["isLeoAssistantAllowed"] is True
        # Linux builds of Brave have no VPN, and brave://settings carries no switch for it there.
        assert plain["isBraveVPNEnabled"] is (None if sys.platform.startswith("linux") else True)
        assert plain["isSpeedreaderAllowed"] is True
        assert plain["prefs"]["brave.wallet.default_wallet2"] != 1
        assert plain["prefs"]["brave.ai_chat.show_toolbar_button"] is True
        assert _leo_page(client) == "chrome://leo-ai/"
        assert json.loads(client.call("server_status").text)["brave"]["lean_running"]["enabled"] is False
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert not (profile / brave_lean.MARKER).exists()
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


def test_spawned_brave_is_lean_and_chrome_is_untouched(data):
    client = _client(data)
    try:
        spawned = client.call("spawn_browser", _timeout=120, instance_id="lean2", url="about:blank",
                              headless=True, browser="brave", extra_args=[LOOPBACK_ONLY])
        assert not spawned.failed and "brave_lean on" in spawned.text, spawned.text
        assert client.call("switch_instance", instance_id="lean2").text
        assert _probe(client)["isLeoAssistantAllowed"] is False
        assert not client.call("close_instance", instance_id="lean2", _timeout=40).failed
        lean_root = data / "mcp" / "brave-profiles" / "lean2"
        assert brave_lean.read_back(lean_root, [])["tor.tor_disabled"] is True

        chrome_profile = data / "chrome-profile"
        chrome_profile.mkdir()
        chrome = client.call("browser_launch", _timeout=120, url="about:blank", headless=True,
                             browser="chrome", brave_lean=True, user_data_dir=str(chrome_profile),
                             extra_args=[LOOPBACK_ONLY])
        assert not chrome.failed and "[brave_lean ignored: it applies to Brave only]" in chrome.text
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert not (chrome_profile / brave_lean.MARKER).exists()
        local = json.loads((chrome_profile / "Local State").read_text())
        assert "tor" not in local and "p3a" not in (local.get("brave") or {}), "nothing of lean in Chrome"
    finally:
        client.call("close_all_instances", _timeout=40)
        client.close()


def test_a_folder_the_caller_names_keeps_its_settings_unless_lean_is_asked_for(data):
    """A clone or any user_data_dir holds someone's settings: the default leaves them, and the
    result says why. brave_lean=True applies lean there."""
    named = data / "named-profile"
    named.mkdir()
    client = _client(data)
    try:
        reply = _launch(client, user_data_dir=str(named), extra_args=[LOOPBACK_ONLY])
        assert "[brave_lean off: " in reply.text and "a folder you named" in reply.text, reply.text
        assert _probe(client)["isLeoAssistantAllowed"] is True, "no lean flag either"
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert not (named / brave_lean.MARKER).exists() and not (named / brave_lean.PROVENANCE).exists()
        assert "tor" not in json.loads((named / "Local State").read_text())

        asked = _launch(client, user_data_dir=str(named), brave_lean=True, extra_args=[LOOPBACK_ONLY])
        assert "brave_lean on (asked for)" in asked.text, asked.text
        assert _probe(client)["isLeoAssistantAllowed"] is False
        assert client.call("browser_close", _timeout=40).text.startswith("Browser closed")
        assert not (named / brave_lean.PROVENANCE).exists(), "asked for does not make it ours"
    finally:
        client.call("browser_close", _timeout=40)
        client.close()


def _hosts(netlog: pathlib.Path) -> set:
    text = netlog.read_text()
    if not text.rstrip().endswith("}"):
        text = text.rstrip().rstrip(",") + "]}"  # a log Brave did not finish
    found = set()
    for event in json.loads(text).get("events", []):
        params = event.get("params") or {}
        url = params.get("url") or ""
        if url.startswith(("http://", "https://", "wss://", "ws://")):
            found.add(urlparse(url).hostname)
        host = params.get("host")
        if isinstance(host, str) and "://" not in host:
            found.add(host.rsplit(":", 1)[0])
    return {h for h in found if h and h not in ("127.0.0.1", "localhost")}


def test_lean_stops_p3a_and_the_usage_ping_and_keeps_component_updates(data):
    """Two fresh profiles left idle side by side on about:blank, each with a net-log."""
    logs, replies = {}, {}

    def run(name: str, lean: bool):
        client = Client(env={"MCP_STEALTH_DATA_DIR": str(data / name)})
        try:
            logs[name] = data / f"{name}.netlog.json"
            replies[name] = _launch(client, persistent=False, brave_lean=lean,
                                    extra_args=[f"--log-net-log={logs[name]}", "--net-log-capture-mode=Default"])
            time.sleep(IDLE)
            client.call("browser_close", _timeout=40)
        finally:
            client.close()

    threads = [threading.Thread(target=run, args=(n, lean)) for n, lean in (("lean", True), ("plain", False))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(IDLE + 240)
    lean, plain = _hosts(logs["lean"]), _hosts(logs["plain"])
    print(f"\nidle {IDLE:.0f} s, not lean: {sorted(plain)}\nidle {IDLE:.0f} s, lean: {sorted(lean)}")
    assert "usage-ping.brave.com" in plain, "the measurement saw nothing to stop"
    assert not lean & STOPPED, sorted(lean & STOPPED)
    assert "componentupdater.brave.com" in lean, "component updates (Shields lists) still run"
