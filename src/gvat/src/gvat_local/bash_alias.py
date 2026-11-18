"""Install and remove only this checkout's managed Bash alias block."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path


def markers(root: Path) -> tuple[str, str]:
    identity = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:16]
    return f"# >>> GVAT {identity} >>>", f"# <<< GVAT {identity} <<<"


def strip_alias(text: str, root: Path) -> str:
    start, end = markers(root)
    return re.sub(r"\n" + re.escape(start) + r"\r?\n.*?\r?\n" + re.escape(end) + r"\r?\n?", "", text, flags=re.DOTALL)


def update_profile(profile: Path, root: Path, install: bool) -> bool:
    profile = profile.expanduser().resolve()
    original = profile.read_bytes() if profile.is_file() else b""
    text = strip_alias(original.decode("utf-8"), root)
    if install:
        start, end = markers(root)
        command = shlex.quote(str(root.resolve() / "src" / "bin" / "gvat"))
        text += f"\n{start}\nalias gvat={shlex.quote(command)}\n{end}\n"
    updated = text.encode("utf-8")
    if updated == original:
        return False
    profile.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".gvat-bashrc-", dir=profile.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, stat.S_IMODE(profile.stat().st_mode) if profile.exists() else 0o644)
        if (profile.read_bytes() if profile.is_file() else b"") != original:
            raise RuntimeError(f"{profile} changed during installation; run the script again")
        os.replace(temporary, profile)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def read_record(root: Path) -> dict:
    file = root / ".venv-gvat" / "gvat-install.json"
    if file.is_file():
        document = json.loads(file.read_text())
        if isinstance(document, dict) and document.get("root") == str(root.resolve()):
            return document
    return {"version": 1, "root": str(root.resolve()), "links": [], "profiles": []}


def install(root: Path, link: str = "", profile: str = "") -> None:
    root = root.resolve()
    record = read_record(root)
    if link:
        record["links"] = sorted(set(record.get("links", []) + [str(Path(link).absolute())]))
    if profile:
        path = Path(profile).expanduser().absolute()
        update_profile(path, root, True)
        record["profiles"] = sorted(set(record.get("profiles", []) + [str(path)]))
        print(f"Installed Bash alias in {path}")
        print(f"Open a new Bash session or run: source {shlex.quote(str(path))}")
    destination = root / ".venv-gvat" / "gvat-install.json"
    destination.write_text(json.dumps(record, indent=2) + "\n")


def uninstall(root: Path, fallback: str = "") -> None:
    root = root.resolve()
    profiles = set(read_record(root).get("profiles", []))
    if fallback:
        profiles.add(fallback)
    removed = False
    for name in profiles:
        profile = Path(name).expanduser()
        if profile.is_file() and update_profile(profile, root, False):
            print(f"Removed Bash alias from {profile}")
            removed = True
    if removed:
        print("Open a new Bash session or run: unalias gvat")


if __name__ == "__main__":
    operation, directory, *arguments = sys.argv[1:]
    if operation == "install":
        install(Path(directory), *arguments)
    elif operation == "uninstall":
        uninstall(Path(directory), *arguments)
    else:
        raise SystemExit("Expected install or uninstall")
