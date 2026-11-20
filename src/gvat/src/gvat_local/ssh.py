"""SSH/SFTP locations with local media caching and checked metadata writes."""

from __future__ import annotations

import argparse
import errno
import getpass
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .workspace import Conflict, WorkspaceError, atomic_write, digest, read_optional


@dataclass(frozen=True)
class SSHLocation:
    host: str
    path: str
    user: str | None = None
    port: int | None = None

    @property
    def endpoint(self):
        return self.host, self.user, self.port


def location(value):
    if isinstance(value, (Path, SSHLocation)):
        return value
    try:
        if value.startswith('ssh://'):
            url = urlsplit(value)
            if not url.hostname or not url.path or url.password is not None or url.query or url.fragment:
                raise ValueError('Use ssh://[user@]host[:port]/path, without passwords or query parameters')
            path = unquote(url.path)
            return _validate(SSHLocation(url.hostname, path[1:] if path.startswith('/~/') else path,
                                         unquote(url.username) if url.username else None, url.port))
        match = re.fullmatch(r'(?:(?P<user>[^@/:]+)@)?(?P<host>[^/:\\]+):(?P<path>.+)', value)
        if match and (match['path'].startswith(('/', '~'))):
            return _validate(SSHLocation(match['host'], match['path'], match['user']))
        return Path(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _validate(value):
    if any(any(ord(character) < 32 for character in part) for part in (value.host, value.path, value.user or '')):
        raise ValueError('SSH locations cannot contain control characters')
    if value.host.startswith('-') or value.user and value.user.startswith('-'):
        raise ValueError('Invalid SSH host/user')
    return value


def connect(location):
    import paramiko

    config = paramiko.SSHConfig()
    profile = Path.home() / '.ssh/config'
    if profile.is_file():
        with profile.open() as source:
            config.parse(source)
    settings = config.lookup(location.host)
    host = settings.get('hostname', location.host)
    user = location.user or settings.get('user', getpass.getuser())
    port = location.port or int(settings.get('port', 22))
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known_hosts = settings.get('userknownhostsfile', [])
    for name in shlex.split(known_hosts) if isinstance(known_hosts, str) else known_hosts:
        path = Path(name).expanduser()
        if path.is_file():
            client.load_system_host_keys(str(path))
    proxy = settings.get('proxycommand')
    if not proxy and settings.get('proxyjump') not in (None, 'none'):
        jumps = settings['proxyjump'].split(',')
        arguments = ['ssh'] + (['-J', ','.join(jumps[:-1])] if len(jumps) > 1 else [])
        proxy = shlex.join(arguments + ['-W', f'[{host}]:{port}', jumps[-1]])
    options = dict(hostname=host, username=user, port=port, timeout=20, auth_timeout=30, banner_timeout=30,
                   key_filename=[str(Path(name).expanduser()) for name in settings.get('identityfile', [])] or None)
    socket = paramiko.ProxyCommand(proxy) if proxy and proxy != 'none' else None
    try:
        try:
            client.connect(**options, sock=socket)
        except (paramiko.AuthenticationException, paramiko.PasswordRequiredException):
            if not sys.stdin.isatty():
                raise WorkspaceError('SSH authentication failed. Load your key into ssh-agent or configure IdentityFile in ~/.ssh/config.')
            client.close()
            if socket:
                socket.close()
                socket = paramiko.ProxyCommand(proxy)
            client.connect(**options, sock=socket, password=getpass.getpass('SSH password or key passphrase: '))
        sftp = client.open_sftp()
        sftp.get_channel().settimeout(60)
        return client, sftp, socket
    except BaseException as error:
        client.close()
        if socket:
            socket.close()
        if isinstance(error, paramiko.SSHException):
            raise WorkspaceError(f'SSH connection failed: {error}. Verify the host first with ssh and check ~/.ssh/config.') from error
        raise


def signature(attributes):
    return attributes.st_size, attributes.st_mtime


class SSHMirror:
    def __init__(self, source, sftp, cache):
        self.source, self.sftp, self.cache = source, sftp, cache
        home = sftp.normalize('.')
        path = posixpath.join(home, source.path[2:]) if source.path.startswith('~/') else source.path
        self.remote_root = sftp.normalize(path)
        if not stat.S_ISDIR(sftp.stat(self.remote_root).st_mode):
            raise WorkspaceError('SSH data path must be a directory')
        self.sample = cache / 'sample'
        self.sample.mkdir(parents=True, exist_ok=True)
        self.paths = {}
        self.lock = threading.RLock()
        self.sources = {}
        self.last_source_check = 0
        self.manifest_file = cache / 'media-cache.json'
        try:
            self.manifest = json.loads(self.manifest_file.read_text())
        except (FileNotFoundError, ValueError):
            self.manifest = {}

    def _read(self, path):
        try:
            attributes = self.sftp.lstat(path)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return None
            raise
        if not stat.S_ISREG(attributes.st_mode):
            raise WorkspaceError('SSH metadata must be a regular file, not a symlink or directory')
        with self.sftp.open(path, 'rb') as source:
            raw = source.read()
        if signature(attributes) != signature(self.sftp.stat(path)):
            raise Conflict('SSH file changed while reading. Try again.')
        return raw

    def refresh_file(self, local):
        with self.lock:
            remote = self.paths.setdefault(local, posixpath.join(self.remote_root, local.name))
            raw = self._read(remote)
            current = read_optional(local)
            if raw == current:
                return
            if raw is None:
                local.unlink(missing_ok=True)
            else:
                atomic_write(local, raw, digest(current))

    def _files(self, relative=''):
        """Walk regular remote files without following symlinks or hidden dirs."""
        for entry in self.sftp.listdir_attr(posixpath.join(self.remote_root, relative)):
            name = entry.filename
            if name.startswith('.'):
                continue
            if name != posixpath.basename(name) or '\\' in name:
                raise WorkspaceError('Invalid SSH sample filename')
            key = posixpath.join(relative, name)
            if stat.S_ISDIR(entry.st_mode):
                yield from self._files(key)
            elif stat.S_ISREG(entry.st_mode):
                yield key, entry

    def download(self):
        with self.lock:
            present = set()
            self.inventory = {}
            for name, entry in self._files():
                present.add(name)
                self.inventory[name] = signature(entry)
                remote, local = posixpath.join(self.remote_root, name), self.sample / name
                local.parent.mkdir(parents=True, exist_ok=True)
                self.paths[local] = remote
                if name.lower().endswith('.xml') or name == 'reviews.json':
                    self.refresh_file(local)
                elif not local.is_file() or self.manifest.get(name) != list(signature(entry)):
                    descriptor, staging = tempfile.mkstemp(prefix='.download-', dir=self.cache)
                    try:
                        with os.fdopen(descriptor, 'wb') as output, self.sftp.open(remote, 'rb') as source:
                            shutil.copyfileobj(source, output, 1024 * 1024)
                        if signature(entry) != signature(self.sftp.stat(remote)):
                            raise Conflict('SSH source changed during transfer. Restart GVAT.')
                        os.replace(staging, local)
                        os.utime(local, (entry.st_mtime, entry.st_mtime))
                    finally:
                        Path(staging).unlink(missing_ok=True)
                    self.manifest[name] = list(signature(entry))
            # This directory contains only files owned by this cache, never user data.
            for local in self.sample.rglob('*'):
                if local.is_file() and local.relative_to(self.sample).as_posix() not in present:
                    local.unlink()
            atomic_write(self.manifest_file, json.dumps(self.manifest).encode(), digest(read_optional(self.manifest_file)))

    def metadata_path(self, value, default_name):
        if value is None:
            return None
        if isinstance(value, SSHLocation):
            if value.endpoint != self.source.endpoint:
                raise WorkspaceError('The annotation XML must use the same SSH host as --data_path')
            remote = value.path
        elif value.is_absolute():
            raise WorkspaceError('For SSH samples, use an SSH XML URL or an XML filename relative to the remote sample')
        else:
            remote = posixpath.join(self.remote_root, value.as_posix())
        if remote.startswith('~/'):
            remote = posixpath.join(self.sftp.normalize('.'), remote[2:])
        local = self.cache / default_name
        self.paths[local] = remote
        self.refresh_file(local)
        return local

    def bind(self, workspace):
        self.paths.setdefault(workspace.xml_file, posixpath.join(self.remote_root, workspace.xml_file.name))
        self.sources = {source.name: self.inventory[source.name] for source in workspace.sources}
        self.check_sources(force=True)
        workspace.remote = self
        # A newly created XML is published before the browser opens.
        remote = self.paths[workspace.xml_file]
        if self._read(remote) is None:
            self.write_file(workspace.xml_file, workspace.xml_file.read_bytes(), 'missing')

    def check_sources(self, force=False):
        with self.lock:
            if not force and time.monotonic() - self.last_source_check < 1:
                return
            actual = {}
            # One listing per source directory, rather than a round trip per image.
            for directory in {posixpath.dirname(name) for name in self.sources}:
                actual.update((posixpath.join(directory, entry.filename), signature(entry))
                              for entry in self.sftp.listdir_attr(posixpath.join(self.remote_root, directory))
                              if stat.S_ISREG(entry.st_mode))
            for name, expected in self.sources.items():
                if actual.get(name) != expected:
                    raise Conflict('SSH source media changed. Restart GVAT before continuing.')
            self.last_source_check = time.monotonic()

    def write_file(self, local, raw, expected):
        with self.lock:
            self.check_sources(force=True)
            remote = self.paths.setdefault(local, posixpath.join(self.remote_root, local.name))
            if digest(self._read(remote)) != expected:
                raise Conflict('SSH file changed on the server. Reload before saving; your browser edits are retained.')
            staging = posixpath.join(posixpath.dirname(remote), f'.{posixpath.basename(remote)}.gvat-{uuid.uuid4().hex}')
            try:
                with self.sftp.open(staging, 'wx') as output:
                    output.write(raw)
                    output.flush()
                if expected != 'missing':
                    self.sftp.chmod(staging, stat.S_IMODE(self.sftp.stat(remote).st_mode))
                if digest(self._read(remote)) != expected:
                    raise Conflict('SSH file changed during save. Reload before saving again.')
                # OpenSSH atomic replacement; never delete the original as a fallback.
                self.sftp.posix_rename(staging, remote)
            finally:
                try:
                    self.sftp.remove(staging)
                except OSError:
                    pass
            current = read_optional(local)
            if current != raw:
                atomic_write(local, raw, digest(current))


@contextmanager
def input_locations(args):
    """Resolve CLI inputs for the whole session and release every SSH connection."""
    import fcntl

    with ExitStack() as stack:
        mirror = None
        if isinstance(args.data_path, SSHLocation):
            source = args.data_path
            key = hashlib.sha256(repr(source).encode()).hexdigest()
            cache = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'gvat/ssh' / key
            cache.mkdir(parents=True, exist_ok=True)
            lock = stack.enter_context((cache / 'session.lock').open('a'))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkspaceError('Another GVAT instance is already using this SSH sample') from error
            client, sftp, socket = connect(source)
            stack.callback(client.close)
            if socket:
                stack.callback(socket.close)
            stack.callback(sftp.close)
            mirror = SSHMirror(source, sftp, cache)
            print('Checking SSH sample and caching source media…', flush=True)
            mirror.download()
            args.data_path = mirror.sample
            args.annotations = mirror.metadata_path(args.annotations, 'selected.xml')
            if args.labels is None:
                parent_labels = SSHLocation(source.host, posixpath.join(mirror.remote_root, '../labels.txt'), source.user, source.port)
                if mirror._read(parent_labels.path) is not None:
                    args.labels = parent_labels
                    args._discovered_labels = True
        if isinstance(args.labels, SSHLocation):
            label_source = args.labels
            if label_source.path.lower().endswith('.xml'):
                raise WorkspaceError('Select remote XML with --annotations; --labels accepts a shared text class list')
            if mirror and label_source.endpoint == mirror.source.endpoint:
                sftp = mirror.sftp
            else:
                client, sftp, socket = connect(label_source)
                stack.callback(client.close)
                if socket:
                    stack.callback(socket.close)
                stack.callback(sftp.close)
            temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='gvat-labels-'))
            destination = Path(temporary) / ('labels.xml' if label_source.path.lower().endswith('.xml') else 'labels.txt')
            path = label_source.path
            if path.startswith('~/'):
                path = posixpath.join(sftp.normalize('.'), path[2:])
            with sftp.open(path, 'rb') as source:
                destination.write_bytes(source.read())
            args.labels = destination
        if isinstance(args.annotations, SSHLocation):
            raise WorkspaceError('SSH annotation XML requires an SSH --data_path')
        yield mirror
