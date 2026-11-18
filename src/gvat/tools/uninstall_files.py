"""Remove generated installation files after checking their directory boundaries."""

import json
import os
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
launcher = root / "src" / "bin" / "gvat"
links = {str(Path(sys.argv[2]).expanduser().absolute() / "gvat")}
record = root / ".venv-gvat" / "gvat-install.json"
if record.is_file():
    try:
        installation = json.loads(record.read_text())
        if installation.get("version") == 1 and installation.get("root") == str(root):
            links.update(value for value in installation.get("links", []) if isinstance(value, str))
    except (ValueError, OSError, TypeError, AttributeError):
        print("Installation record is unreadable; checking the configured command directory.")
for name in links:
    link = Path(name)
    # Never remove an unrelated executable or a link to another checkout.
    if link.name == "gvat" and link.is_symlink() and link.resolve() == launcher.resolve():
        link.unlink()
        print(f"Removed command: {link}")

def remove_artifact(target, boundary):
    if not target.parent.resolve().is_relative_to(boundary):
        raise SystemExit(f"Refusing to remove a path outside the installation: {target}")
    if target.is_symlink():
        target.unlink()  # Remove only the link, never its destination.
    elif target.exists():
        resolved = target.resolve(strict=True)
        if resolved == boundary or not resolved.is_relative_to(boundary):
            raise SystemExit(f"Refusing to remove a path outside the installation: {target}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            raise SystemExit(f"Expected a generated directory: {target}")
    else:
        return
    print(f"Removed generated files: {target}")

for relative in (
    ".venv-gvat", "src/gvat/web/node_modules", "src/gvat/web/dist",
    "src/gvat/src/gvat_local.egg-info", "src/gvat/src/gvat_local/__pycache__",
    "src/gvat/tests/__pycache__", "src/gvat/tools/__pycache__",
    "src/gvat/web/test-results", "src/gvat/web/playwright-report",
    "src/gvat/build", "src/gvat/dist", ".pytest_cache", ".ruff_cache", ".mypy_cache",
):
    remove_artifact(root / relative, root)
if sys.argv[3] == "1":
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser().resolve()
    for generated in ("node", "frames", "video-index", "build-tools", "ssh"):
        remove_artifact(cache_root / "gvat" / generated, cache_root)
