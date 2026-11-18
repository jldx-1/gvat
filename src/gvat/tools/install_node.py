"""Download and verify a local Node release when no suitable Node is installed."""

import hashlib
import io
import json
import os
import platform
import posixpath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
cache = Path(sys.argv[1])
arch = {"x86_64": "x64", "aarch64": "arm64"}.get(platform.machine())
if not arch:
    raise SystemExit("Automatic Node installation supports Linux x86_64 and aarch64. Install Node 18+ manually.")
def download(url):
    if not shutil.which("curl"):
        raise SystemExit("curl is needed to bootstrap Node. Make curl or Node 18+ available and rerun ./install.sh.")
    try:
        return subprocess.check_output(["curl", "--fail", "--location", "--silent", "--show-error", "--connect-timeout", "15", "--max-time", "120", url])
    except subprocess.CalledProcessError:
        raise SystemExit(f"Could not download {url}. Check network access and rerun ./install.sh.")
print("Checking official Node 22 LTS releases…", flush=True)
releases = json.loads(download("https://nodejs.org/dist/index.json"))
version = next(item["version"] for item in releases if item["version"].startswith("v22.") and item.get("lts"))
filename = f"node-{version}-linux-{arch}.tar.xz"
destination = cache / f"node-{version}-linux-{arch}"
if not (destination / ".gvat-ready").is_file():
    base = f"https://nodejs.org/dist/{version}/"
    print(f"Downloading {filename} and verifying its SHA-256 checksum…", flush=True)
    expected = next(line.split()[0] for line in download(base + "SHASUMS256.txt").decode().splitlines() if line.split()[-1] == filename)
    archive_file = cache / filename
    archive = archive_file.read_bytes() if archive_file.is_file() else download(base + filename)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise SystemExit("Node archive checksum mismatch")
    archive_file.write_bytes(archive)
    staging = Path(tempfile.mkdtemp(prefix=".node-staging-", dir=cache))
    def inside(name):
        value = PurePosixPath(posixpath.normpath(name))
        return not value.is_absolute() and value.parts and value.parts[0] == destination.name and ".." not in value.parts
    try:
        print(f"Extracting Node {version}…", flush=True)
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:xz") as bundle:
            for member in bundle.getmembers():
                if not inside(member.name) or not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise SystemExit("Unexpected path or file type in Node archive")
                if member.issym() or member.islnk():
                    target = posixpath.join(posixpath.dirname(member.name), member.linkname) if member.issym() else member.linkname
                    if not inside(target):
                        raise SystemExit("Unexpected link in Node archive")
            bundle.extractall(staging)
        extracted = staging / destination.name
        if not (extracted / "bin/node").is_file() or not (extracted / "lib/node_modules/npm/lib/cli.js").is_file():
            raise SystemExit("Node archive did not contain a complete installation")
        (extracted / ".gvat-ready").write_text(expected)
        if destination.exists():
            # Preserve an interrupted older extraction instead of trusting it.
            destination.rename(cache / f".incomplete-{destination.name}-{uuid.uuid4().hex}")
        os.replace(extracted, destination)
    finally:
        shutil.rmtree(staging)
(cache / "active-path").write_text(str(destination / "bin"))
