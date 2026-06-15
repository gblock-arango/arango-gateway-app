#!/usr/bin/env python3
"""Read ``env:`` entry ``value`` from a Databricks App ``app.yaml`` (for deploy_app.sh)."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def iter_app_yaml_env(app_yaml: Path) -> list[tuple[str, str]]:
    text = app_yaml.read_text(encoding="utf-8")
    pattern = re.compile(
        r"-\s*name:\s*(\S+)\s*\n"
        r"(?:\s+[^\n]+\n)*?"
        r'\s+value:\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))',
        re.MULTILINE,
    )
    entries: list[tuple[str, str]] = []
    for m in pattern.finditer(text):
        name = m.group(1)
        val = (m.group(2) or m.group(3) or m.group(4) or "").strip()
        entries.append((name, val))
    return entries


def read_app_yaml_value(app_yaml: Path, name: str) -> str:
    for key, val in iter_app_yaml_env(app_yaml):
        if key == name:
            return val
    return ""


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: read_app_yaml_env.py NAME|--export-all [app.yaml]", file=sys.stderr)
        return 2
    path = Path(sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] != "--export-all" else "app.yaml")
    if len(sys.argv) > 2 and sys.argv[1] == "--export-all":
        path = Path(sys.argv[2])
    elif sys.argv[1] == "--export-all":
        path = Path("app.yaml")
    if not path.is_file():
        if sys.argv[1] == "--export-all":
            return 0
        print("", end="")
        return 1
    if sys.argv[1] == "--export-all":
        import shlex

        for name, val in iter_app_yaml_env(path):
            print(f"export {name}={shlex.quote(val)}")
        return 0
    name = sys.argv[1]
    print(read_app_yaml_value(path, name), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
