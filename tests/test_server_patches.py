"""A test that patches a name ON THE SERVER MODULE reaches only code that looks it up there.

Fast tests keep the code away from the person's data folder by pointing a name at a temporary one:
`monkeypatch.setattr(server, "PROFILES_ROOT", tmp_path / "profiles")`, and the same for
PROFILE_DIR, ensure_dirs, the profile lock lookups. Code in another module that has its own
`from ..state import PROFILES_ROOT` does not see that: the test still passes, and the code has
acted on the real `~/.mcp-stealth`. A fixture patching server.STORAGE_STATE_DIR let tests write
into the real storage-states folder this way after the cookie tools moved out of server.py.

So a module outside server.py reads such a name as `_srv.<name>` (`from .. import server as _srv`),
at call time. This test lists every name the tests patch on server and fails on a module that
imports one for itself and uses it in a function.
"""
import ast
import pathlib
import re

from _protocol import REPO

PACKAGE = REPO / "src" / "mcp_stealth_chrome"
TESTS = REPO / "tests"

# Pairs from before this check existed. Each is safe today for a stated reason, and none may be
# added: a new one means moved code stopped seeing a patch.
#   ensure_dirs: only makes the data folders; the tests that patch it on server watch the launch path.
#   get_url: patched on server by one test of click_and_wait, which reaches it as _srv.get_url.
#   STORAGE_STATE_DIR: the tests of cookie_export and storage_state_save patch the module itself,
#     and conftest points all three (server, tools.storage, tools.stealth) at a temporary folder.
#   find_chrome_binary: patched on server by two launch tests; the OS-cursor solver starts no browser there.
KNOWN = {
    ("image_output.py", "ensure_dirs"), ("tools/capture.py", "ensure_dirs"), ("tools/devtools.py", "ensure_dirs"),
    ("tools/mouse_kit.py", "ensure_dirs"), ("tools/page.py", "ensure_dirs"), ("tools/stealth.py", "ensure_dirs"),
    ("tools/storage.py", "ensure_dirs"), ("tools/vision.py", "ensure_dirs"),
    ("tools/interaction.py", "get_url"), ("tools/navigation.py", "get_url"), ("tools/network_http.py", "get_url"),
    ("tools/page.py", "get_url"), ("tools/stealth.py", "get_url"), ("tools/storage.py", "get_url"),
    ("tools/turnstile_oscursor.py", "get_url"),
    ("tools/stealth.py", "STORAGE_STATE_DIR"), ("tools/storage.py", "STORAGE_STATE_DIR"),
    ("tools/turnstile_oscursor.py", "find_chrome_binary"),
}


def patched_on_server() -> dict:
    """name -> the test lines that patch it on the server module."""
    found: dict = {}
    for path in sorted(TESTS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        aliases = set(re.findall(r"import mcp_stealth_chrome\.server as (\w+)", text))
        aliases |= {"server", "srv"} & set(re.findall(r"\b(server|srv)\.", text))
        if not aliases:
            continue
        alias = "|".join(sorted(aliases))
        patterns = (rf'setattr\(\s*(?:{alias})\s*,\s*"(\w+)"', rf'patch\.object\(\s*(?:{alias})\s*,\s*"(\w+)"',
                    rf"^\s*(?:{alias})\.(\w+)\s*=[^=]", r'"mcp_stealth_chrome\.server\.(\w+)"')
        for number, line in enumerate(text.splitlines(), 1):
            for pattern in patterns:
                for name in re.findall(pattern, line):
                    found.setdefault(name, []).append(f"{path.name}:{number}")
    return found


def own_imports(source: str) -> dict:
    """name -> where from, for a module's own top-level imports (not from server) that a function uses."""
    tree = ast.parse(source)
    names = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and not (node.module or "").endswith("server"):
            for alias in node.names:
                if alias.name != "server":
                    names[alias.asname or alias.name] = "." * node.level + (node.module or "")
    used = {inner.id for outer in ast.walk(tree) if isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef))
            for inner in ast.walk(outer) if isinstance(inner, ast.Name)}
    return {name: origin for name, origin in names.items() if name in used}


def pairs() -> tuple[dict, dict]:
    patched = patched_on_server()
    found = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "server.py":
            continue
        for name, origin in own_imports(path.read_text(encoding="utf-8")).items():
            if name in patched:
                found[(path.relative_to(PACKAGE).as_posix(), name)] = origin
    return found, patched


def test_no_module_reads_its_own_copy_of_a_name_tests_patch_on_server():
    found, patched = pairs()
    assert len(patched) > 50 and "PROFILES_ROOT" in patched and "PROFILE_DIR" in patched  # the scan sees the patches
    new = sorted(set(found) - KNOWN)
    assert not new, "\n".join(
        f"{module} imports {name} from {found[(module, name)]} for itself, and tests patch server.{name} "
        f"({', '.join(patched[name][:3])}): read it as _srv.{name} so the patch still reaches this code"
        for module, name in new)


def test_the_list_of_old_pairs_only_shrinks():
    found, _patched = pairs()
    gone = sorted(KNOWN - set(found))
    assert not gone, f"no longer true, remove from KNOWN: {gone}"


def test_the_scan_catches_a_module_with_its_own_import():
    source = ("from ..state import PROFILES_ROOT, BrowserState\nfrom .. import server as _srv\n\n"
              "def clone():\n    return PROFILES_ROOT / 'x'\n\n"
              "def fine():\n    return _srv.PROFILE_DIR\n")
    assert own_imports(source) == {"PROFILES_ROOT": "..state"}
