"""browser="brave": finding Brave, its own profiles, the profile binding, vertical tabs and the
identity it keeps. Fast: no browser starts. The real-Brave proof is test_brave_e2e.py."""
import asyncio
import json
import os
from pathlib import Path

import pytest

from mcp_stealth_chrome import brave, identity, state


# ── Identity ────────────────────────────────────────────────────────────────

def test_brave_brands_match_what_brave_157_reports():
    """Measured on Brave 157.1.99.55: Not-A.Brand 99, Brave 157, Chromium 157, in that order,
    and every full version reduced to 157.0.0.0 while the engine is 157.0.8103.39."""
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/157.0.0.0 Safari/537.36")
    hints = identity.ua_metadata_fields(ua, "MacIntel", "157.0.8103.39", product="Brave")
    assert [(b["brand"], b["version"]) for b in hints["brands"]] == [
        ("Not-A.Brand", "99"), ("Brave", "157"), ("Chromium", "157")]
    assert [(b["brand"], b["version"]) for b in hints["full_version_list"]] == [
        ("Not-A.Brand", "99.0.0.0"), ("Brave", "157.0.0.0"), ("Chromium", "157.0.0.0")]
    assert hints["full_version"] == "157.0.0.0"
    assert "Google Chrome" not in json.dumps(hints)


def test_chrome_hints_are_unchanged():
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/157.0.0.0 Safari/537.36")
    hints = identity.ua_metadata_fields(ua, "MacIntel", "157.0.8103.51")
    assert hints["full_version"] == "157.0.8103.51"
    assert ("Google Chrome", "157.0.8103.51") in [(b["brand"], b["version"]) for b in hints["full_version_list"]]


class _Config:
    def __init__(self, path):
        self.browser_executable_path = path


class _Browser:
    def __init__(self, path):
        self.config = _Config(path)


class _Tab:
    def __init__(self, path):
        self.browser = _Browser(path)


@pytest.mark.parametrize("path,product", [
    ("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser", "brave"),
    ("/usr/bin/brave-browser", "brave"),
    (r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe", "brave"),
    ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
    (None, "chrome"),
])
def test_product_of_a_tab_or_browser(path, product):
    if path and "\\" in path and os.sep == "/":
        path = path.replace("\\", "/")
    assert brave.product_of(_Tab(path)) == product
    assert brave.product_of(_Browser(path)) == product


def test_the_identity_override_names_brave_on_a_brave_tab():
    """Headless always overrides the UA (HeadlessChrome) and so the hints: on Brave they must
    keep naming Brave, or navigator.brave and userAgentData contradict each other."""
    from mcp_stealth_chrome import server

    sent = []

    class Tab(_Tab):
        async def send(self, command):
            sent.append(command)
            if len(sent) == 1:
                return ("1.3", "Chrome/157.0.8103.39", "rev",
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) HeadlessChrome/157.0.0.0 Safari/537.36", "js")
            return None

    def run(tab):
        sent.clear()
        captured = {}
        from nodriver.cdp import network
        real = network.set_user_agent_override

        def spy(**kwargs):
            captured.update(kwargs)
            return real(**kwargs)
        network.set_user_agent_override = spy
        try:
            asyncio.run(server._apply_fingerprint_to_tab(tab, {}))
        finally:
            network.set_user_agent_override = real
        return captured

    on_brave = run(Tab("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"))
    brands = [b.brand for b in on_brave["user_agent_metadata"].brands]
    assert "Brave" in brands and "Google Chrome" not in brands
    assert on_brave["user_agent_metadata"].full_version == "157.0.0.0"
    assert "HeadlessChrome" not in on_brave["user_agent"]
    on_chrome = run(Tab("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    assert "Google Chrome" in [b.brand for b in on_chrome["user_agent_metadata"].brands]


# ── The request ─────────────────────────────────────────────────────────────

def test_check_request(monkeypatch, tmp_path):
    exe = tmp_path / "brave"
    exe.write_text("")
    monkeypatch.setenv(brave.ENV_PATH, str(exe))
    assert brave.check_request(None, None) == ("chrome", "", "")
    assert brave.check_request("brave", "right") == ("brave", str(exe), "")
    assert "must be 'chrome' or 'brave'" in brave.check_request("firefox", None)[2]
    assert "vertical_tabs must be" in brave.check_request("brave", "top")[2]
    refused = brave.check_request("chrome", "right")[2]
    assert "only puts vertical tabs on the left" in refused and "Nothing was started" in refused
    assert brave.check_request("chrome", "left")[2] == ""


def test_brave_missing_is_named_with_how_to_install(monkeypatch, tmp_path):
    monkeypatch.setenv(brave.ENV_PATH, str(tmp_path / "missing"))
    _product, exe, problem = brave.check_request("brave", None)
    assert exe == "" and "Brave is not installed here" in problem and "brave.com" in problem


def test_browser_launch_refuses_before_anything_runs(monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "ensure_dirs", lambda: pytest.fail("got past the check"))
    text = asyncio.run(server.browser_launch(browser="chrome", vertical_tabs="right"))
    assert text.startswith("Error:") and "on the left" in text
    ok, text = asyncio.run(server._launch_browser_instance(
        instance_id="w", url="about:blank", headless=True, proxy=None, user_agent=None,
        window_width=800, window_height=600, persistent=False, lang="en-US", extra_args=None,
        storage_state_path=None, idle_timeout=0, browser="chrome", vertical_tabs="right"))
    assert not ok and "on the left" in text


# ── Profiles and the binding ────────────────────────────────────────────────

def _profile(folder: Path, local_state: dict | None = None, prefs: dict | None = None) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if local_state is not None:
        (folder / "Local State").write_text(json.dumps(local_state))
    if prefs is not None:
        (folder / "Default").mkdir(exist_ok=True)
        (folder / "Default" / "Preferences").write_text(json.dumps(prefs))
    return folder


def test_which_browser_made_a_profile(tmp_path):
    assert brave.profile_browser(None) is None
    assert brave.profile_browser(tmp_path / "missing") is None
    assert brave.profile_browser(_profile(tmp_path / "empty")) is None
    assert brave.profile_browser(_profile(tmp_path / "b", {"brave": {}, "profile": {}})) == "brave"
    assert brave.profile_browser(_profile(tmp_path / "s", {"brave_shields": {}})) == "brave"
    assert brave.profile_browser(_profile(tmp_path / "c", {"profile": {}})) == "chrome"
    assert brave.profile_browser(_profile(tmp_path / "bp", None, {"brave": {}})) == "brave"
    assert brave.profile_browser(_profile(tmp_path / "cp", None, {"profile": {}})) == "chrome"
    bad = _profile(tmp_path / "bad")
    (bad / "Local State").write_text("{not json")
    assert brave.profile_browser(bad) == "unknown"


def test_binding_refuses_the_other_browser_and_names_why(tmp_path):
    made_by_brave = _profile(tmp_path / "b", {"brave": {}})
    made_by_chrome = _profile(tmp_path / "c", {"profile": {}})
    assert brave.binding_refusal(made_by_brave, "brave") == ""
    assert brave.binding_refusal(made_by_chrome, "chrome") == ""
    assert brave.binding_refusal(tmp_path / "new", "brave") == ""
    to_chrome = brave.binding_refusal(made_by_brave, "chrome")
    assert "made by Brave" in to_chrome and "browser='brave'" in to_chrome
    to_brave = brave.binding_refusal(made_by_chrome, "brave")
    assert "made by Google Chrome" in to_brave and "browser='chrome'" in to_brave
    assert "Nothing was started and the profile was not changed" in to_brave
    bad = _profile(tmp_path / "bad")
    (bad / "Local State").write_text("{not json")
    # Unknown is refused to both (review F2): Chrome used to open such a folder.
    assert "cannot be told" in brave.binding_refusal(bad, "brave")
    assert "cannot be told" in brave.binding_refusal(bad, "chrome")


def test_the_launch_check_applies_the_binding(tmp_path):
    """_profile_version_refusal runs at every launch before anything writes to the profile."""
    from mcp_stealth_chrome import server

    made_by_brave = _profile(tmp_path / "b", {"brave": {}})
    made_by_chrome = _profile(tmp_path / "c", {"profile": {}})
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    brave_exe = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    assert "made by Brave" in server._profile_version_refusal(made_by_brave, chrome)
    assert "made by Google Chrome" in server._profile_version_refusal(made_by_chrome, brave_exe)
    assert "made by" not in server._profile_version_refusal(made_by_brave, brave_exe)


def test_brave_profiles_live_apart_from_chromes(tmp_path, monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")
    monkeypatch.setattr(state, "_PROFILE_AFFINITY", tmp_path / "pinned-chrome")
    path, info = server._resolve_launch_profile("main", persistent=True, profile_conflict="fallback",
                                                browser="brave")
    assert path == tmp_path / "brave-profile" and "error" not in info, info
    path, _ = server._resolve_launch_profile("acct", persistent=True, profile_conflict="fallback",
                                             browser="brave")
    assert path == tmp_path / "brave-profiles" / "acct"
    assert server._instance_profile_path("acct", persistent=True, privacy=False,
                                         profile_dir_override=None, browser="brave") \
        == tmp_path / "brave-profiles" / "acct"
    assert server._instance_profile_path("main", persistent=True, privacy=False,
                                         profile_dir_override=None) == tmp_path / "profile"


def test_a_held_brave_profile_falls_back_to_a_brave_folder(tmp_path, monkeypatch):
    from mcp_stealth_chrome import server

    monkeypatch.setattr(server, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")
    (tmp_path / "brave-profile").mkdir()
    monkeypatch.setattr(server, "chrome_lock_holder_pid", lambda p: 4242)
    path, info = server._resolve_launch_profile("main", persistent=True, profile_conflict="fallback",
                                                browser="brave")
    assert path == tmp_path / f"brave-profile-pid{os.getpid()}", info


def test_the_dock_icon_never_replaces_brave(monkeypatch):
    from mcp_stealth_chrome import dock_icon, server

    monkeypatch.setattr(dock_icon, "prepare", lambda *a, **k: pytest.fail("Dock icon asked for Brave"))
    monkeypatch.setattr(dock_icon, "chosen", lambda **k: "/Applications/Google Chrome Beta.app/x")
    exe = "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"
    choice = server._launch_choice(None, False, [], exe)
    assert choice.executable == exe and not choice.beta
    config = _Config(None)
    assert asyncio.run(server._use_dock_icon_browser(config, False, choice)) == ""
    assert config.browser_executable_path == exe


# ── Vertical tabs ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("product,mode,expect", [
    ("brave", "left", {"brave": {"tabs": {"vertical_tabs_enabled": True, "vertical_tabs_on_right": False}}}),
    ("brave", "right", {"brave": {"tabs": {"vertical_tabs_enabled": True, "vertical_tabs_on_right": True}}}),
    ("brave", "off", {"brave": {"tabs": {"vertical_tabs_enabled": False, "vertical_tabs_on_right": False}}}),
    ("chrome", "left", {"vertical_tabs": {"enabled": True}}),
    ("chrome", "off", {"vertical_tabs": {"enabled": False}}),
])
def test_vertical_tabs_are_merged_into_the_profile(tmp_path, product, mode, expect):
    sub = tmp_path / "Profile 3"
    sub.mkdir()
    (sub / "Preferences").write_text(json.dumps({"profile": {"name": "kept"},
                                                 "brave": {"tabs": {"hover_mode": 1}}}))
    note = brave.write_vertical_tabs(tmp_path, ["--profile-directory=Profile 3"], product, mode)
    assert note.startswith(" [vertical tabs:")
    data = json.loads((sub / "Preferences").read_text())
    assert data["profile"] == {"name": "kept"}
    assert data["brave"]["tabs"]["hover_mode"] == 1
    for key, value in expect.items():
        for inner, v in value.items():
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    assert data[key][inner][k2] == v2
            else:
                assert data[key][inner] == v
    assert brave.vertical_tabs_read_back(tmp_path, ["--profile-directory=Profile 3"], product) == mode
    assert not [p for p in sub.iterdir() if p.name.startswith(".Preferences-")]


def test_vertical_tabs_none_writes_nothing_and_a_broken_file_is_not_replaced(tmp_path):
    assert brave.write_vertical_tabs(tmp_path, [], "brave", None) == ""
    assert not (tmp_path / "Default").exists()
    (tmp_path / "Default").mkdir()
    (tmp_path / "Default" / "Preferences").write_text("{broken")
    with pytest.raises(ValueError):
        brave.write_vertical_tabs(tmp_path, [], "brave", "right")
    assert (tmp_path / "Default" / "Preferences").read_text() == "{broken"
    brave.write_vertical_tabs(tmp_path / "fresh", [], "brave", "right")
    assert brave.vertical_tabs_read_back(tmp_path / "fresh", [], "brave") == "right"


# ── Cloning a Brave profile ─────────────────────────────────────────────────

def test_a_brave_clone_lands_in_brave_profiles_and_says_to_launch_brave(tmp_path, monkeypatch):
    from mcp_stealth_chrome import server

    root = _profile(tmp_path / "BraveSoftware" / "Brave-Browser", {"brave": {}, "profile": {}},
                    {"brave": {}})
    data = tmp_path / "data"
    monkeypatch.setattr(server, "PROFILE_DIR", data / "profile")
    monkeypatch.setattr(server, "PROFILES_ROOT", data / "profiles")
    monkeypatch.setattr(server, "ensure_dirs", lambda: None)
    monkeypatch.setattr(server, "chrome_user_data_root", lambda b=None: root)
    monkeypatch.setattr(server, "chrome_user_data_roots", lambda: [("brave", root)])
    text = asyncio.run(server.clone_chrome_profile(target_instance_id="mine", browser="brave"))
    assert not text.startswith("Error"), text
    result = json.loads(text[text.index("{"):])
    assert result["target"] == str(data / "brave-profiles" / "mine")
    assert "browser='brave'" in result["next_step"]
    clone = data / "brave-profiles" / "mine"
    assert brave.profile_browser(clone) == "brave"
    assert not (data / "profiles" / "mine").exists()
