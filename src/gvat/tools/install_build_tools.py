"""Prepare user-local compilers only when CVAT's exporter needs a source build."""

import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import urllib.request
from pathlib import Path

ENGINE_URL = 'https://github.com/cvat-ai/datumaro/archive/335fc3e55fe699b9401091f6af11f10c3b0e08cb.zip'


def exporter_already_installed():
    try:
        metadata = importlib.metadata.distribution('datumaro')
        return json.loads(metadata.read_text('direct_url.json') or '{}').get('url') == ENGINE_URL
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return False


def download(url):
    if shutil.which('curl'):
        return subprocess.check_output(['curl', '--fail', '--location', '--silent', '--show-error',
                                        '--connect-timeout', '15', '--max-time', '180', url])
    with urllib.request.urlopen(url, timeout=180) as response:
        return response.read()


def prepare(cache, environment_file):
    cache.mkdir(parents=True, exist_ok=True)
    exports = {}
    if not exporter_already_installed():
        if not (Path(sysconfig.get_path('include')) / 'Python.h').is_file():
            raise SystemExit('This Python is missing development headers (Python.h). Set GVAT_PYTHON to a Python 3.10+ installation with headers and rerun ./install.sh.')
        compiler = next((shutil.which(name) for name in ('c++', 'g++', 'clang++') if shutil.which(name)), None)
        if not compiler or not shutil.which('cc'):
            # Zig supplies a standalone C/C++ toolchain without system headers.
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', 'ziglang==0.14.1'], check=True)
            for command, mode in (('cc', 'cc'), ('c++', 'c++')):
                wrapper = cache / command
                wrapper.write_text('#!/usr/bin/env bash\nexec ' + shlex.quote(sys.executable) + ' -m ziglang ' + mode + ' "$@"\n')
                wrapper.chmod(0o755)
            exports.update(CC=str(cache / 'cc'), CXX=str(cache / 'c++'))

        cargo, rustc = shutil.which('cargo'), shutil.which('rustc')
        rust_ok = False
        if cargo and rustc:
            result = subprocess.run([rustc, '--version'], capture_output=True, text=True)
            version = re.search(r'rustc (\d+)\.(\d+)', result.stdout)
            rust_ok = result.returncode == 0 and version is not None and tuple(map(int, version.groups())) >= (1, 85)
        if not rust_ok:
            architecture = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}.get(platform.machine())
            if architecture is None:
                raise SystemExit('Install Rust 1.85+ for your user account, then rerun ./install.sh.')
            triple = architecture + '-unknown-linux-gnu'
            binary = cache / 'rustup-init'
            url = f'https://static.rust-lang.org/rustup/dist/{triple}/rustup-init'
            content, checksum = download(url), download(url + '.sha256').decode().split()[0]
            if hashlib.sha256(content).hexdigest() != checksum:
                raise SystemExit('Rust installer checksum mismatch')
            binary.write_bytes(content)
            binary.chmod(0o755)
            exports.update(RUSTUP_HOME=str(cache / 'rustup'), CARGO_HOME=str(cache / 'cargo'))
            subprocess.run([str(binary), '-y', '--no-modify-path', '--profile', 'minimal', '--default-toolchain', 'stable'],
                           env={**os.environ, **exports}, check=True)
        if 'CC' in exports:
            architecture = {'x86_64': 'X86_64', 'aarch64': 'AARCH64'}.get(platform.machine())
            if architecture:
                exports[f'CARGO_TARGET_{architecture}_UNKNOWN_LINUX_GNU_LINKER'] = exports['CC']
    with environment_file.open('w') as output:
        for name, value in exports.items():
            output.write(f'export {name}={shlex.quote(value)}\n')
        if exports:
            paths = [str(cache)]
            if 'CARGO_HOME' in exports:
                paths.append(str(Path(exports['CARGO_HOME']) / 'bin'))
            output.write('export PATH=' + ':'.join(shlex.quote(path) for path in paths) + ':"$PATH"\n')


if __name__ == '__main__':
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
