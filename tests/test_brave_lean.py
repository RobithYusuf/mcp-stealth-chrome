"""brave_lean: the parts of Brave an agent has no use for, off per process and per profile.
Fast: no browser starts. The real-Brave proof is test_brave_lean_e2e.py.

Every profile here is a folder under tmp_path; the person's own Brave folder is a stand-in under
tmp_path too (person_brave_folders patched), never the real one.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from mcp_stealth_chrome import brave, brave_lean, state

POSIX_MODES = pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps no POSIX mode bits")


def _prefs(root: Path, sub: str = "Default") -> dict:
    return json.loads((root / sub / "Preferences").read_text())


def _local_state(root: Path) -> dict:
    return json.loads((root / "Local State").read_text())


def _get(data, dotted):
    for part in dotted.split("."):
        data = data[part]
    return data


def _snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.read_bytes() if p.is_file() and not p.is_symlink() else None)
            for p in sorted(folder.rglob("*"))}


@pytest.fixture(autouse=True)
def nobody_holds_it(monkeypatch, tmp_path):
    """The process list is read for real elsewhere; here no process names these folders, and the
    person's Brave is a folder under tmp_path that no test writes to."""
    monkeypatch.setattr(state, "processes_naming", lambda root: [])
    monkeypatch.setattr(brave_lean, "person_brave_folders", lambda: [tmp_path / "person-brave"])


# ── Writing ─────────────────────────────────────────────────────────────────

def test_a_new_profile_gets_every_setting_and_stays_a_brave_profile(tmp_path):
    root = tmp_path / "agent"
    out = brave_lean.apply(root, [], True)
    assert out.not_written == [] and out.features == brave_lean.FEATURES
    prefs, local = _prefs(root), _local_state(root)
    for key, value in brave_lean.PREFERENCES.items():
        assert _get(prefs, key) == value, key
    for key, value in brave_lean.LOCAL_STATE.items():
        assert _get(local, key) == value, key
    assert sorted(out.written["Default/Preferences"]) == sorted(brave_lean.PREFERENCES)
    assert sorted(out.written["Local State"]) == sorted(brave_lean.LOCAL_STATE)
    assert brave.profile_browser(root) == "brave", "the binding still reads it as Brave's"
    assert brave_lean.read_back(root, []) == {**brave_lean.LOCAL_STATE, **brave_lean.PREFERENCES}
    leftovers = [p.name for p in root.rglob(".*") if p.name != brave_lean.MARKER]
    assert leftovers == [], "no temporary file left"
    assert "brave_lean on (asked for), turned off: Leo AI" in out.note() and "NOT applied" not in out.note()


def test_existing_settings_are_kept_and_the_launched_sub_profile_is_the_one_written(tmp_path):
    root = tmp_path / "agent"
    (root / "Profile 2").mkdir(parents=True)
    (root / "Profile 2" / "Preferences").write_text(json.dumps(
        {"brave": {"tabs": {"vertical_tabs_enabled": True}, "brave_vpn": {"show_button": True}},
         "profile": {"name": "kept"}}))
    (root / "Local State").write_text(json.dumps({"brave": {"p3a": {"enabled": True}}, "keep": 1}))
    out = brave_lean.apply(root, ["--profile-directory=Profile 2"], True)
    assert out.not_written == []
    prefs = _prefs(root, "Profile 2")
    assert prefs["profile"] == {"name": "kept"} and prefs["brave"]["tabs"]["vertical_tabs_enabled"] is True
    assert prefs["brave"]["brave_vpn"]["show_button"] is False
    assert _local_state(root)["keep"] == 1 and _local_state(root)["brave"]["p3a"]["enabled"] is False
    assert not (root / "Default").exists(), "only the launched sub-profile"


def test_off_puts_back_what_was_there_and_leaves_what_changed_since(tmp_path):
    root = tmp_path / "agent"
    (root / "Default").mkdir(parents=True)
    original_prefs = {"brave": {"brave_vpn": {"show_button": True}}, "profile": {"name": "x"}}
    (root / "Default" / "Preferences").write_text(json.dumps(original_prefs))
    (root / "Local State").write_text(json.dumps({"brave": {"p3a": {"enabled": True}}}))
    brave_lean.apply(root, [], True)
    # Since then a person turned the sidebar back on.
    prefs = _prefs(root)
    prefs["brave"]["sidebar"]["sidebar_show_option"] = 0
    (root / "Default" / "Preferences").write_text(json.dumps(prefs))

    out = brave_lean.apply(root, [], False)
    assert out.not_written == [] and out.features == ()
    assert out.left == ["brave.sidebar.sidebar_show_option"]
    assert len(out.restored) == len(brave_lean.PREFERENCES) + len(brave_lean.LOCAL_STATE) - 1
    prefs = _prefs(root)
    assert prefs["brave"]["brave_vpn"]["show_button"] is True, "the earlier value is back"
    assert prefs["brave"]["sidebar"]["sidebar_show_option"] == 0, "the person's change stays"
    assert "ai_chat" not in prefs["brave"] or "show_toolbar_button" not in prefs["brave"]["ai_chat"], \
        "a key lean added is removed again"
    assert prefs["profile"] == {"name": "x"}
    assert _local_state(root)["brave"]["p3a"]["enabled"] is True
    assert "tor" not in _local_state(root) or "tor_disabled" not in _local_state(root)["tor"]
    assert not (root / brave_lean.MARKER).exists()
    assert "left 1 changed since" in out.note()
    # Off again with nothing recorded touches nothing.
    before = _snapshot(root)
    assert brave_lean.apply(root, [], False).restored == []
    assert _snapshot(root) == before


def test_on_twice_names_a_value_changed_since(tmp_path):
    root = tmp_path / "agent"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Preferences").write_text(json.dumps({"brave": {"sidebar": {"sidebar_show_option": 0}}}))
    brave_lean.apply(root, [], True)
    prefs = _prefs(root)
    prefs["brave"]["sidebar"]["sidebar_show_option"] = 1  # Brave or a person changed it
    (root / "Default" / "Preferences").write_text(json.dumps(prefs))
    again = brave_lean.apply(root, [], True)
    assert again.changed_since == ["brave.sidebar.sidebar_show_option"]
    assert "changed brave.sidebar.sidebar_show_option since the last lean launch" in again.note()
    assert _prefs(root)["brave"]["sidebar"]["sidebar_show_option"] == 3
    brave_lean.apply(root, [], False)
    assert _prefs(root)["brave"]["sidebar"]["sidebar_show_option"] == 1, "the person's later choice"


@POSIX_MODES
def test_the_marker_is_private(tmp_path):
    root = tmp_path / "agent"
    brave_lean.apply(root, [], True)
    assert (root / brave_lean.MARKER).stat().st_mode & 0o777 == 0o600
    assert (root / "Default" / "Preferences").stat().st_mode & 0o777 == 0o600


# ── Guards: unknown means do not write ──────────────────────────────────────

def _assert_nothing_written(out, root, before, why):
    assert _snapshot(root) == before, "nothing was written"
    assert out.written == {}
    assert out.not_written and why in out.not_written[0][1], out.not_written
    assert out.features == brave_lean.FEATURES, "the per-process features still go off"
    assert "NOT applied: profile settings" in out.note()


def _seeded(tmp_path) -> Path:
    root = tmp_path / "agent"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Preferences").write_text(json.dumps({"brave": {}}))
    (root / "Local State").write_text(json.dumps({"brave": {}}))
    return root


def test_a_held_profile_is_not_written(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    monkeypatch.setattr(state, "chrome_lock_state", lambda p: ("held", 4242))
    _assert_nothing_written(brave_lean.apply(root, [], True), root, before, "in use")


def test_a_lock_that_cannot_be_read_is_not_free(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    monkeypatch.setattr(state, "chrome_lock_state", lambda p: ("unknown", None))
    _assert_nothing_written(brave_lean.apply(root, [], True), root, before, "in use")


def test_an_unreadable_process_list_is_not_empty(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    monkeypatch.setattr(state, "processes_naming", lambda r: None)
    _assert_nothing_written(brave_lean.apply(root, [], True), root, before, "cannot be told")


def test_a_process_naming_the_profile_holds_it(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    monkeypatch.setattr(state, "processes_naming", lambda r: [(777, "brave --user-data-dir=x")])
    _assert_nothing_written(brave_lean.apply(root, [], True), root, before, "PID 777")


def test_the_persons_own_brave_folder_is_never_written(tmp_path):
    """A stand-in under tmp_path plays the person's Brave folder (see the autouse fixture)."""
    root = _seeded(tmp_path)
    person = tmp_path / "person-brave"
    root.rename(person)
    before = _snapshot(person)
    out = brave_lean.apply(person, [], True)
    _assert_nothing_written(out, person, before, "person's own Brave folder")
    # Through a link to it as well.
    link = tmp_path / "link-to-person"
    link.symlink_to(person)
    _assert_nothing_written(brave_lean.apply(link, [], True), person, before, "person's own Brave folder")
    # Off never writes there either.
    assert brave_lean.apply(person, [], False).restored == [] and _snapshot(person) == before


def test_a_broken_preferences_file_is_not_replaced(tmp_path):
    root = _seeded(tmp_path)
    (root / "Default" / "Preferences").write_text("{broken")
    before = _snapshot(root)
    _assert_nothing_written(brave_lean.apply(root, [], True), root, before, "JSONDecodeError")


@pytest.mark.parametrize("which", ["Default/Preferences", "Local State", brave_lean.MARKER, "Default"])
def test_a_link_is_never_written_through(tmp_path, which):
    root = _seeded(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "target"
    link = root / which
    if which == "Default":
        (outside / "target").mkdir()
        (outside / "target" / "Preferences").write_text("{}")
        for child in link.iterdir():
            child.unlink()
        link.rmdir()
    else:
        target.write_text("{}")
        if link.exists():
            link.unlink()
    link.symlink_to(target)
    before, outside_before = _snapshot(root), _snapshot(outside)
    out = brave_lean.apply(root, [], True)
    _assert_nothing_written(out, root, before, "link")
    assert _snapshot(outside) == outside_before, "nothing written through the link"


@pytest.mark.parametrize("sub", ["..", "../x", "/abs", "a/b"])
def test_a_profile_directory_outside_the_profile_is_refused(tmp_path, sub):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    out = brave_lean.apply(root, [f"--profile-directory={sub}"], True)
    _assert_nothing_written(out, root, before, "profile")


def test_a_key_brave_protects_with_a_mac_is_not_written(tmp_path):
    root = _seeded(tmp_path)
    (root / "Default" / "Secure Preferences").write_text(json.dumps({"protection": {"macs": {
        "brave": {"wayback_machine_enabled": "ABC", "wayback_machine_enabled_encrypted_hash": "x"}}}}))
    out = brave_lean.apply(root, [], True)
    assert ("brave.wayback_machine_enabled", "Brave protects it with a MAC and would reset it") in out.not_written
    assert "wayback_machine_enabled" not in _prefs(root)["brave"]
    assert "brave.wayback_machine_enabled" not in out.written["Default/Preferences"]
    assert _prefs(root)["brave"]["sidebar"]["sidebar_show_option"] == 3, "the others are written"


def test_a_marker_of_another_version_is_not_trusted(tmp_path):
    root = _seeded(tmp_path)
    (root / brave_lean.MARKER).write_text(json.dumps({"version": 9, "files": {}}))
    before = _snapshot(root)
    _assert_nothing_written(brave_lean.apply(root, [], True), root, before, "version")


def test_apply_never_raises(tmp_path, monkeypatch):
    root = _seeded(tmp_path)

    def boom(*_a):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(brave, "_refuse_unless_free", boom)
    out = brave_lean.apply(root, [], True)
    assert out.not_written == [("profile settings", "RuntimeError: unexpected")]
    assert brave_lean.apply(None, [], True).not_written[0][1] == "the launch has no profile folder"


# ── The server's side ───────────────────────────────────────────────────────

class _Config:
    """The parts of nodriver's Config the launch uses: browser_args, add_argument, and calling
    it lists every argument, nodriver's own --disable-features first."""

    def __init__(self, root):
        self.user_data_dir = str(root)
        self.browser_args = []

    def add_argument(self, arg):
        self.browser_args.append(arg)

    def __call__(self):
        return ["--disable-features=IsolateOrigins,site-per-process", *self.browser_args]


def test_the_launch_passes_one_disable_features_keeping_nodrivers_list(tmp_path):
    from mcp_stealth_chrome import server

    config = _Config(tmp_path / "agent")
    config.add_argument("--disable-features=Translate")  # testing_mode's, say
    note = server._apply_brave_lean(config, "brave", None, server_chosen=True)
    last = [a for a in config() if a.startswith("--disable-features=")][-1]
    assert last == ("--disable-features=IsolateOrigins,site-per-process,Translate,"
                    "AIChat,BraveVPN,Speedreader,Playlist")
    assert "brave_lean on" in note
    assert getattr(config, brave_lean.ATTR).enabled is True


@pytest.mark.parametrize("given,expect_note", [(None, ""), (True, "ignored"), (False, "ignored")])
def test_chrome_gets_nothing_and_hears_about_it_only_when_it_asked(tmp_path, given, expect_note):
    from mcp_stealth_chrome import server

    config = _Config(tmp_path / "chrome")
    note = server._apply_brave_lean(config, "chrome", given)
    assert config.browser_args == [] and not (tmp_path / "chrome").exists()
    assert (expect_note in note) if expect_note else note == ""


def test_off_adds_no_flag(tmp_path):
    from mcp_stealth_chrome import server

    config = _Config(tmp_path / "agent")
    note = server._apply_brave_lean(config, "brave", False)
    assert config.browser_args == [] and "brave_lean=False" in note


def test_a_reattach_names_brave_lean_as_not_applied():
    from mcp_stealth_chrome import server

    assert server._reattach_ignored({"brave_lean": False}) == ["brave_lean=False"]
    assert server._reattach_ignored({"brave_lean": None}) == []


def test_server_status_describes_lean():
    from mcp_stealth_chrome import server

    lean = server._brave_status()["lean"]
    assert lean["features_disabled"] == list(brave_lean.FEATURES)
    assert "brave.p3a.enabled" in lean["local_state"]
    assert any("Rewards" in item for item in lean["not_possible_without_policy"])


def test_the_parameter_reaches_both_tools():
    import inspect

    from mcp_stealth_chrome import server

    for tool in (server.browser_launch, server.spawn_browser, server._launch_browser_instance):
        fn = getattr(tool, "fn", tool)
        assert inspect.signature(fn).parameters["brave_lean"].default is None


# ── Review fixes ────────────────────────────────────────────────────────────

# (1) Lean by default only on a profile this server made empty.

def test_default_lean_claims_an_empty_server_profile_and_marks_it(tmp_path):
    root = tmp_path / "brave-profile"
    out = brave_lean.apply(root, [], None, server_chosen=True, registry=tmp_path / "registry")
    assert out.enabled and out.written, out.not_written
    assert (root / brave_lean.PROVENANCE).is_file(), "the profile is marked as ours"
    again = brave_lean.apply(root, [], None, server_chosen=True, registry=tmp_path / "registry")  # now it holds data
    assert again.enabled and again.written


def test_default_lean_leaves_a_clone_alone(tmp_path):
    """A clone in brave-profiles/<id> sits where the server would put a profile, but it was
    not made empty by this server: it holds the person's settings."""
    root = _seeded(tmp_path)
    before = _snapshot(root)
    out = brave_lean.apply(root, [], None, server_chosen=True, registry=tmp_path / "registry")
    assert not out.enabled and out.features == () and out.written == {}
    assert _snapshot(root) == before
    assert "brave_lean=True" in out.note() and "not made empty by this server" in out.note()


@pytest.mark.parametrize("seed", [False, True])
def test_default_lean_never_touches_a_folder_the_caller_named(tmp_path, seed):
    root = _seeded(tmp_path) if seed else tmp_path / "named"
    if not seed:
        root.mkdir()
    before = _snapshot(root)
    out = brave_lean.apply(root, [], None, server_chosen=False)
    assert not out.enabled and out.written == {} and _snapshot(root) == before
    assert "you named" in out.note() and "brave_lean=True" in out.note()


def test_asked_for_lean_applies_to_a_named_folder_without_claiming_it(tmp_path):
    root = _seeded(tmp_path)
    out = brave_lean.apply(root, [], True, server_chosen=False)
    assert out.enabled and out.written
    assert not (root / brave_lean.PROVENANCE).exists(), "asked for is not made by us"


def test_default_does_not_take_back_what_an_asked_for_lean_wrote(tmp_path):
    root = _seeded(tmp_path)
    brave_lean.apply(root, [], True, server_chosen=False)
    written = _snapshot(root)
    out = brave_lean.apply(root, [], None, server_chosen=False)
    assert _snapshot(root) == written and out.restored == []
    assert "brave_lean=False puts them back" in out.note()


def test_the_server_passes_whose_profile_it_is(tmp_path):
    from mcp_stealth_chrome import server

    named = _seeded(tmp_path)
    config = _Config(named)
    note = server._apply_brave_lean(config, "brave", None, server_chosen=False)
    assert config.browser_args == [] and "you named" in note
    fresh = _Config(tmp_path / "fresh")
    server._apply_brave_lean(fresh, "brave", None, server_chosen=True)
    assert any("AIChat" in a for a in fresh.browser_args)


# (2) Taking back across sub-profiles.

def test_off_puts_back_every_sub_profile_lean_wrote_whatever_is_launched_now(tmp_path):
    root = tmp_path / "agent"
    for sub, option in (("Default", 0), ("Profile 2", 1)):
        (root / sub).mkdir(parents=True)
        (root / sub / "Preferences").write_text(json.dumps({"brave": {"sidebar": {"sidebar_show_option": option}}}))
    brave_lean.apply(root, [], True)
    brave_lean.apply(root, ["--profile-directory=Profile 2"], True)
    out = brave_lean.apply(root, ["--profile-directory=Profile 3"], False)
    assert out.not_written == []
    assert _prefs(root)["brave"]["sidebar"]["sidebar_show_option"] == 0
    assert _prefs(root, "Profile 2")["brave"]["sidebar"]["sidebar_show_option"] == 1
    assert not (root / brave_lean.MARKER).exists()


def test_a_sub_profile_that_cannot_be_read_keeps_its_record(tmp_path):
    root = tmp_path / "agent"
    brave_lean.apply(root, [], True)
    brave_lean.apply(root, ["--profile-directory=Profile 2"], True)
    (root / "Profile 2" / "Preferences").write_text("{broken")
    out = brave_lean.apply(root, [], False)
    assert any("Profile 2/Preferences" in what for what, _why in out.not_written), out.not_written
    record = json.loads((root / brave_lean.MARKER).read_text())["files"]
    assert list(record) == ["Profile 2/Preferences"], "only what was put back leaves the record"
    assert "ai_chat" not in _prefs(root)["brave"] or "show_toolbar_button" not in _prefs(root)["brave"]["ai_chat"]


def test_a_change_made_after_lean_is_what_off_puts_back(tmp_path):
    """The person set the sidebar to 1 after lean; lean set it to 3 again; off gives back 1,
    their latest choice, not the 0 from before the first lean."""
    root = tmp_path / "agent"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Preferences").write_text(json.dumps({"brave": {"sidebar": {"sidebar_show_option": 0}}}))
    brave_lean.apply(root, [], True)
    prefs = _prefs(root)
    prefs["brave"]["sidebar"]["sidebar_show_option"] = 1
    (root / "Default" / "Preferences").write_text(json.dumps(prefs))
    brave_lean.apply(root, [], True)
    brave_lean.apply(root, [], False)
    assert _prefs(root)["brave"]["sidebar"]["sidebar_show_option"] == 1


# (3) A parent that is not a settings object.

def test_a_parent_holding_a_scalar_is_kept_and_its_keys_are_not_written(tmp_path):
    root = _seeded(tmp_path)
    (root / "Default" / "Preferences").write_text(json.dumps({"brave": {"ai_chat": "person-value"}}))
    (root / "Local State").write_text(json.dumps({"tor": 7}))
    out = brave_lean.apply(root, [], True)
    assert _prefs(root)["brave"]["ai_chat"] == "person-value"
    assert _local_state(root)["tor"] == 7
    skipped = {what for what, _why in out.not_written}
    assert {"brave.ai_chat.show_toolbar_button", "tor.tor_disabled"} <= skipped
    assert _prefs(root)["brave"]["sidebar"]["sidebar_show_option"] == 3, "the others are written"
    record = json.loads((root / brave_lean.MARKER).read_text())["files"]
    assert "brave.ai_chat.show_toolbar_button" not in record["Default/Preferences"]
    assert "tor.tor_disabled" not in record["Local State"]
    assert "NOT applied: brave.ai_chat.show_toolbar_button" in out.note()


def test_off_does_not_build_parents_to_remove_a_key(tmp_path):
    root = tmp_path / "agent"
    brave_lean.apply(root, [], True)
    prefs = _prefs(root)
    prefs["brave"]["ai_chat"] = "person-value"  # turned into a scalar after lean
    (root / "Default" / "Preferences").write_text(json.dumps(prefs))
    out = brave_lean.apply(root, [], False)
    assert _prefs(root)["brave"]["ai_chat"] == "person-value"
    assert "brave.ai_chat.show_toolbar_button" in out.left


@pytest.mark.parametrize("record", [[], {"Default/Preferences": []}, {"Default/Preferences": {"k": 1}}])
def test_a_record_of_an_unexpected_shape_is_not_trusted(tmp_path, record):
    root = _seeded(tmp_path)
    (root / brave_lean.MARKER).write_text(json.dumps({"version": 1, "files": record}))
    before = _snapshot(root)
    out = brave_lean.apply(root, [], True)
    assert _snapshot(root) == before and out.written == {}
    assert out.not_written and "record" in out.not_written[0][1]


# (4) A profile folder that is itself a link.

@pytest.mark.parametrize("asked", [True, None, False])
def test_a_linked_profile_folder_is_never_written(tmp_path, asked):
    real = _seeded(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    if asked is False:
        brave_lean.apply(real, [], True)
    before = _snapshot(real)
    out = brave_lean.apply(alias, [], asked, server_chosen=True, registry=tmp_path / "registry")
    assert _snapshot(real) == before, "nothing written through the link"
    assert out.written == {} and out.restored == []
    assert any("link" in why for _what, why in out.not_written), out.not_written


# (5) A reattach applies no lean, and says so.

@pytest.mark.parametrize("given", [None, True, False])
def test_a_brave_reattach_says_lean_was_not_applied(given):
    from mcp_stealth_chrome import server

    note = server._lean_on_reattach("brave", given)
    assert "brave_lean" in note and "not applied" in note and "keeps" in note
    assert server._lean_on_reattach("chrome", None) == ""


# ── Second review ───────────────────────────────────────────────────────────

# (1) Where a profile came from is proved by a record outside it, not by a file inside it.

def _claim(tmp_path, name="brave-profile"):
    root = tmp_path / name
    out = brave_lean.apply(root, [], None, server_chosen=True, registry=tmp_path / "registry")
    assert out.enabled and out.written, out.not_written
    return root


def _default(root, tmp_path):
    return brave_lean.apply(root, [], None, server_chosen=True, registry=tmp_path / "registry")


def test_the_claim_is_recorded_outside_the_profile_and_binds_its_root(tmp_path):
    root = _claim(tmp_path)
    records = list((tmp_path / "registry").iterdir())
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    st = os.lstat(root)
    assert record["root"] == os.path.realpath(root)
    assert (record["dev"], record["ino"]) == (st.st_dev, st.st_ino)
    if sys.platform != "win32":
        assert records[0].stat().st_mode & 0o777 == 0o600
        assert (tmp_path / "registry").stat().st_mode & 0o777 == 0o700
    assert _default(root, tmp_path).enabled, "the same folder is still ours"


def test_a_copy_of_a_claimed_profile_is_not_ours(tmp_path):
    import shutil
    root = _claim(tmp_path)
    copy = tmp_path / "copy"
    shutil.copytree(root, copy, symlinks=True)
    before = _snapshot(copy)
    out = _default(copy, tmp_path)
    assert not out.enabled and out.features == () and out.written == {}
    assert _snapshot(copy) == before
    assert "this server's record of it does not match" in out.note(), out.note()


def test_a_moved_profile_is_not_ours(tmp_path):
    root = _claim(tmp_path)
    moved = tmp_path / "moved"
    root.rename(moved)
    out = _default(moved, tmp_path)
    assert not out.enabled and "does not match" in out.note()


def test_a_mark_written_by_hand_is_not_proof(tmp_path):
    root = _seeded(tmp_path)
    st = os.lstat(root)
    (root / brave_lean.PROVENANCE).write_text(json.dumps(
        {"version": 1, "root": os.path.realpath(root), "dev": st.st_dev, "ino": st.st_ino}))
    before = _snapshot(root)
    out = _default(root, tmp_path)
    assert not out.enabled and out.written == {} and _snapshot(root) == before
    assert "does not match" in out.note()


def test_a_record_without_its_mark_is_not_enough(tmp_path):
    root = _claim(tmp_path)
    (root / brave_lean.PROVENANCE).unlink()
    assert not _default(root, tmp_path).enabled


def test_without_a_record_folder_the_default_proves_nothing(tmp_path):
    root = tmp_path / "fresh"
    out = brave_lean.apply(root, [], None, server_chosen=True, registry=None)
    assert not out.enabled and out.features == () and not root.exists()


def test_a_broken_mark_applies_nothing_not_even_the_flag(tmp_path):
    from mcp_stealth_chrome import server

    root = _claim(tmp_path)
    (root / brave_lean.PROVENANCE).write_text("{broken")
    before = _snapshot(root)
    config = _Config(root)
    note = server._apply_brave_lean(config, "brave", None, server_chosen=True,
                                    registry=tmp_path / "registry")
    assert config.browser_args == [], "no --disable-features on an unproved profile"
    assert _snapshot(root) == before and "brave_lean off" in note and "NOT applied" in note


def test_a_broken_lean_record_under_the_default_applies_nothing(tmp_path):
    from mcp_stealth_chrome import server

    root = _claim(tmp_path)
    (root / brave_lean.MARKER).write_text("{broken")
    config = _Config(root)
    note = server._apply_brave_lean(config, "brave", None, server_chosen=True,
                                    registry=tmp_path / "registry")
    assert config.browser_args == [] and "NOT applied" in note


# (2) Every write checks again that no browser took the profile and the folder is the same.

def _lock(root: Path) -> None:
    """A stand-in for a browser that just started: SingletonLock naming a live PID (ours)."""
    os.symlink(f"stand-in-host-{os.getpid()}", root / "SingletonLock")


def test_a_browser_starting_after_the_check_stops_every_write(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    real = brave._refuse_unless_free

    def passes_then_a_browser_starts(r):
        real(r)
        _lock(Path(r))
    monkeypatch.setattr(brave, "_refuse_unless_free", passes_then_a_browser_starts)
    out = brave_lean.apply(root, [], True)
    after = {k: v for k, v in _snapshot(root).items() if k != "SingletonLock"}
    assert after == before, "nothing written once the lock appeared"
    assert out.written == {} and any("in use" in why for _w, why in out.not_written), out.not_written


def test_a_lock_appearing_between_files_stops_and_says_what_was_written(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    real = brave_lean._write_json
    writes = []

    def write_then_lock(path, data):
        real(path, data)
        writes.append(Path(path).name)
        if Path(path).name == "Local State":
            _lock(root)
    monkeypatch.setattr(brave_lean, "_write_json", write_then_lock)
    out = brave_lean.apply(root, [], True)
    assert "Preferences" not in writes
    assert list(out.written) == ["Local State"]
    problem = " ".join(why for _w, why in out.not_written)
    assert "Default/Preferences" in problem and "in use" in problem, out.not_written


def test_a_lock_that_cannot_be_read_mid_way_stops(tmp_path, monkeypatch):
    root = _seeded(tmp_path)
    calls = []
    real = state.chrome_lock_state

    def unknown_after_first(r):
        calls.append(1)
        return real(r) if len(calls) == 1 else ("unknown", None)
    monkeypatch.setattr(state, "chrome_lock_state", unknown_after_first)
    out = brave_lean.apply(root, [], True)
    assert out.written == {} and not (root / brave_lean.MARKER).exists()


def test_a_folder_replaced_after_the_claim_record_is_not_written(tmp_path, monkeypatch):
    root = tmp_path / "brave-profile"
    real = brave_lean._write_json
    swapped = tmp_path / "swapped-away"

    def write_then_replace(path, data):
        real(path, data)
        if Path(path).parent == tmp_path / "registry":
            root.rename(swapped)
            root.mkdir()
    monkeypatch.setattr(brave_lean, "_write_json", write_then_replace)
    out = _default(root, tmp_path)
    assert list(root.iterdir()) == [] and list(swapped.iterdir()) == []
    assert out.written == {} and out.features == ()


def test_taking_back_stops_before_removing_the_record_when_a_browser_starts(tmp_path, monkeypatch):
    root = tmp_path / "agent"
    brave_lean.apply(root, [], True)
    real = brave_lean._write_json

    def write_then_lock(path, data):
        real(path, data)
        if Path(path).name == "Preferences":
            _lock(root)
    monkeypatch.setattr(brave_lean, "_write_json", write_then_lock)
    out = brave_lean.apply(root, [], False)
    assert (root / brave_lean.MARKER).exists(), "the record stays for what was not put back"
    assert any("in use" in why for _w, why in out.not_written), out.not_written


def test_a_copy_put_in_the_place_of_a_claimed_profile_is_not_ours(tmp_path):
    """Same path, same mark, another folder: only the inode tells."""
    import shutil
    root = _claim(tmp_path)
    shutil.copytree(root, tmp_path / "held", symlinks=True)
    shutil.rmtree(root)
    (tmp_path / "held").rename(root)
    out = _default(root, tmp_path)
    assert not out.enabled and "does not match" in out.note()


def test_records_of_profiles_that_are_gone_are_dropped_and_nothing_else(tmp_path):
    import shutil
    gone = _claim(tmp_path, "temp-profile")
    shutil.rmtree(gone)
    kept = _claim(tmp_path, "brave-profile")
    stray = tmp_path / "registry" / "notes.txt"
    stray.write_text("not ours")
    _claim(tmp_path, "another")
    names = {p.name for p in (tmp_path / "registry").iterdir()}
    assert brave_lean._registry_path(tmp_path / "registry", gone).name not in names
    assert brave_lean._registry_path(tmp_path / "registry", kept).name in names
    assert "notes.txt" in names and _default(kept, tmp_path).enabled


# (2, part two) Every lean write happens inside the profile's launch reservation (launch_lock).

def _held_elsewhere(root: Path):
    """Another process of this project holding the launch reservation of `root`, as a launch
    of it (a click on the launcher, another server) would."""
    import subprocess
    from mcp_stealth_chrome import launch_lock
    path = launch_lock.lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    code = ("import sys, time, os\n"
            "p = sys.argv[1]\n"
            "fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "try:\n import fcntl; fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "except ImportError:\n import msvcrt; msvcrt.locking(fd, msvcrt.LK_LOCK, 1)\n"
            "print('held', flush=True)\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(path)], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_launch_reservation_held_elsewhere_stops_every_write(tmp_path):
    root = _seeded(tmp_path)
    before = _snapshot(root)
    holder = _held_elsewhere(root)
    try:
        out = brave_lean.apply(root, [], True)
    finally:
        holder.kill()
        holder.wait()
    assert _snapshot(root) == before and out.written == {} and out.features == ()
    assert "brave_lean not applied: another launch holds the reservation" in out.note(), out.note()


def test_the_default_on_a_reserved_profile_applies_nothing(tmp_path):
    root = tmp_path / "brave-profile"
    holder = _held_elsewhere(root)
    try:
        out = _default(root, tmp_path)
    finally:
        holder.kill()
        holder.wait()
    assert not out.enabled and out.features == () and out.written == {}
    assert not root.exists() or list(root.iterdir()) == []


def test_lean_writes_inside_the_reservation_the_launch_already_holds(tmp_path):
    from mcp_stealth_chrome import launch_lock
    root = _seeded(tmp_path)
    with launch_lock.hold([root], 0):
        out = brave_lean.apply(root, [], True)
    assert out.written and not out.not_written


def test_every_check_and_write_happens_while_the_reservation_is_held(tmp_path, monkeypatch):
    import contextlib
    from mcp_stealth_chrome import launch_lock
    root = _seeded(tmp_path)
    events = []
    real_hold, real_free, real_write = launch_lock.hold, brave._refuse_unless_free, brave_lean._write_json

    @contextlib.contextmanager
    def logged_hold(paths, wait):
        with real_hold(paths, wait):
            events.append("reserved")
            yield
            events.append("released")
    monkeypatch.setattr(launch_lock, "hold", logged_hold)
    monkeypatch.setattr(brave, "_refuse_unless_free", lambda r: (events.append("free check"), real_free(r)))
    monkeypatch.setattr(brave_lean, "_write_json", lambda p, d: (events.append("write"), real_write(p, d)))
    brave_lean.apply(root, [], True)
    assert events[0] == "reserved" and events[-1] == "released", events
    assert events.count("write") == 3 and "free check" in events[1:-1]


# ── Third review ────────────────────────────────────────────────────────────

# (1) A record that exists but cannot be trusted turns the default off and is never rewritten.

def _entry(tmp_path, root):
    path = brave_lean._registry_path(tmp_path / "registry", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _assert_default_off_untouched(out, root, entry, entry_bytes, why):
    assert not out.enabled and out.features == () and out.written == {}
    assert not root.exists() or list(root.iterdir()) == [], "nothing written in the profile"
    assert entry.read_bytes() == entry_bytes, "the record is never rewritten by a launch"
    assert why in out.note(), out.note()


@pytest.mark.parametrize("content", [
    b"{broken",
    json.dumps({"version": 99, "root": "R", "dev": 0, "ino": 0}).encode(),
    json.dumps({"version": 1, "root": "R", "dev": "0", "ino": 0}).encode(),
    json.dumps({"version": 1}).encode(),
    json.dumps([1, 2]).encode(),
])
def test_a_damaged_or_unknown_record_of_an_empty_root_keeps_the_default_off(tmp_path, content):
    root = tmp_path / "brave-profile"
    root.mkdir()
    st = os.lstat(root)
    content = content.replace(b'"R"', json.dumps(os.path.realpath(root)).encode()) \
                     .replace(b'"ino": 0', f'"ino": {st.st_ino}'.encode())
    entry = _entry(tmp_path, root)
    entry.write_bytes(content)
    out = _default(root, tmp_path)
    _assert_default_off_untouched(out, root, entry, content, "record")


def test_a_valid_record_of_another_folder_at_this_path_is_not_replaced(tmp_path):
    root = tmp_path / "brave-profile"
    root.mkdir()
    entry = _entry(tmp_path, root)
    content = json.dumps({"version": 1, "root": os.path.realpath(root), "dev": 1, "ino": 1}).encode()
    entry.write_bytes(content)
    _assert_default_off_untouched(_default(root, tmp_path), root, entry, content, "record")


def test_a_record_of_unknown_version_for_a_claimed_profile_is_not_trusted(tmp_path):
    root = _claim(tmp_path)
    entry = brave_lean._registry_path(tmp_path / "registry", root)
    record = json.loads(entry.read_text())
    record["version"] = 99
    entry.write_text(json.dumps(record))
    before, entry_bytes = _snapshot(root), entry.read_bytes()
    out = _default(root, tmp_path)
    assert not out.enabled and out.features == () and _snapshot(root) == before
    assert entry.read_bytes() == entry_bytes and "record" in out.note()


def test_a_record_appearing_before_the_claim_is_not_overwritten(tmp_path, monkeypatch):
    root = tmp_path / "brave-profile"
    entry = brave_lean._registry_path(tmp_path / "registry", root)
    real = brave._refuse_unless_free

    def passes_then_a_record_appears(r):
        real(r)
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("{written by someone else")
    monkeypatch.setattr(brave, "_refuse_unless_free", passes_then_a_record_appears)
    out = _default(root, tmp_path)
    assert entry.read_text() == "{written by someone else"
    assert not out.enabled and out.features == () and out.written == {}
    assert not (root / brave_lean.PROVENANCE).exists()


# (2) Without a reservation actually held, lean writes nothing and adds no flag.

@pytest.fixture
def no_reservation(monkeypatch, tmp_path):
    """A keep-alive folder that cannot hold reservation files, as launch_lock documents: the
    launch goes on unreserved, lean must not."""
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("x")
    monkeypatch.setenv("MCP_STEALTH_KEEP_ALIVE_DIR", str(blocker / "keep-alive"))


@pytest.mark.parametrize("asked", [True, None])
def test_no_reservation_means_no_lean(tmp_path, no_reservation, asked):
    root = _seeded(tmp_path) if asked else tmp_path / "brave-profile"
    before = _snapshot(root) if root.exists() else None
    out = brave_lean.apply(root, [], asked, server_chosen=True, registry=tmp_path / "registry")
    assert not out.enabled and out.features == () and out.written == {}
    assert (_snapshot(root) if root.exists() else None) in (before, {})
    assert not (tmp_path / "registry").exists() or not list((tmp_path / "registry").iterdir())
    assert "not applied" in out.note() and "reservation" in out.note(), out.note()


def test_no_reservation_adds_no_flag_at_the_launch(tmp_path, no_reservation):
    from mcp_stealth_chrome import server

    config = _Config(_seeded(tmp_path))
    note = server._apply_brave_lean(config, "brave", True)
    assert config.browser_args == [] and "not applied" in note


def test_another_launch_holding_it_adds_no_flag_either(tmp_path):
    from mcp_stealth_chrome import server

    root = _seeded(tmp_path)
    holder = _held_elsewhere(root)
    try:
        config = _Config(root)
        note = server._apply_brave_lean(config, "brave", True)
    finally:
        holder.kill()
        holder.wait()
    assert config.browser_args == [] and "another launch" in note
