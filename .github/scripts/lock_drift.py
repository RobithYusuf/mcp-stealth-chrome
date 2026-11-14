"""Print every installed distribution whose version differs from uv.lock.

Run with the interpreter of a lock-free venv: `python -I lock_drift.py path/to/uv.lock`. It only
reports; what the drift breaks is for the test steps after it to find. The list is what to read
first when a suite passes under uv.lock and fails here.
"""
import sys
import tomllib
from importlib import metadata


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def main() -> int:
    with open(sys.argv[1], "rb") as fh:
        locked = {_norm(p["name"]): p.get("version") for p in tomllib.load(fh).get("package", [])}
    rows = []
    for dist in metadata.distributions():
        name = _norm(dist.metadata["Name"] or "")
        if name in locked and locked[name] and locked[name] != dist.version:
            rows.append((name, locked[name], dist.version))
    for name, was, now in sorted(rows):
        print(f"{name:28} uv.lock {was:14} installed {now}")
    print(f"{len(rows)} distributions differ from uv.lock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
