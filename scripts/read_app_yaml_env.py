#!/usr/bin/env python3
"""Read ``env:`` entry ``value`` from a Databricks App ``app.yaml`` (for deploy_app.sh)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

_NAME_RE = re.compile(r"^\s*-\s*name:\s*(\S+)\s*$")
_VALUE_RE = re.compile(
    r'^\s*value:\s*(?:"([^"]*)"|\'([^\']*)\'|(\S+))\s*$'
)


def iter_app_yaml_env(app_yaml: Path) -> list[tuple[str, str]]:
    """Parse ``env:`` blocks without regex backtracking (large app.yaml files hang otherwise)."""
    text = app_yaml.read_text(encoding="utf-8")
    entries: list[tuple[str, str]] = []
    in_env = False
    current_name: str | None = None

    for line in text.splitlines():
        if re.match(r"^env:\s*$", line):
            in_env = True
            current_name = None
            continue
        if in_env and re.match(r"^[a-zA-Z_][\w-]*:\s*$", line) and not line.startswith(" "):
            break

        if not in_env:
            continue

        name_m = _NAME_RE.match(line)
        if name_m:
            current_name = name_m.group(1)
            continue

        if current_name is None:
            continue

        value_m = _VALUE_RE.match(line)
        if value_m:
            val = (value_m.group(1) or value_m.group(2) or value_m.group(3) or "").strip()
            entries.append((current_name, val))
            current_name = None

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
    if sys.argv[1] == "--export-all":
        path = Path(sys.argv[2] if len(sys.argv) > 2 else "app.yaml")
        if not path.is_file():
            return 0
        import shlex

        for env_name, val in iter_app_yaml_env(path):
            print(f"export {env_name}={shlex.quote(val)}")
        return 0
    env_name = sys.argv[1]
    path = Path(sys.argv[2] if len(sys.argv) > 2 else "app.yaml")
    if not path.is_file():
        print("", end="")
        return 1
    print(read_app_yaml_value(path, env_name), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
