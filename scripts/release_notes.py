#!/usr/bin/env python3
"""Print one version's notes from CHANGELOG.md (for the GitHub release).

With --check, instead make sure a release tag is ready to publish: the
version matches app/__init__.py and CHANGELOG.md has a dated (not
"unreleased") section for it. See RELEASING.md.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(version: str) -> tuple[str, str] | None:
    """(heading line, body) of `## <version>` in CHANGELOG.md."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(version)}\b([^\n]*)\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return (m.group(1).strip(), m.group(2).strip()) if m else None


def app_version() -> str:
    m = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "app/__init__.py").read_text())
    return m.group(1) if m else ""


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if a != "--check"]
    if len(args) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", args[0]):
        print("usage: release_notes.py [--check] X.Y.Z", file=sys.stderr)
        return 2
    version = args[0]
    found = section(version)
    if check:
        problems = []
        if app_version() != version:
            problems.append(f"app/__init__.py says {app_version()!r}, the tag says {version!r}")
        if not found:
            problems.append(f"CHANGELOG.md has no '## {version}' section")
        elif "unreleased" in found[0].lower():
            problems.append(f"CHANGELOG.md still marks {version} as unreleased; put the date there")
        for p in problems:
            print(f"::error::{p}")
        return 1 if problems else 0
    if not found:
        print(f"CHANGELOG.md has no '## {version}' section", file=sys.stderr)
        return 1
    print(found[1])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
