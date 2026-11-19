"""Reuse validated source inspection only while its filesystem identity is unchanged."""

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .workspace import Conflict


_local = threading.local()


@contextmanager
def source_cache_session():
    if getattr(_local, 'connections', None) is not None:
        yield
        return
    _local.connections = {}
    try:
        yield
    finally:
        for connection in _local.connections.values():
            if connection is not None:
                try:
                    connection.commit()
                except sqlite3.Error:
                    pass
                connection.close()
        _local.connections = None


def _connection():
    path = cache_root() / 'source-metadata/index.sqlite3'
    connections = _local.connections
    if path not in connections:
        connection = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(path, timeout=.25)
            connection.execute('PRAGMA journal_mode=WAL')
            connection.execute('CREATE TABLE IF NOT EXISTS sources (key TEXT PRIMARY KEY, record TEXT NOT NULL)')
        except (OSError, sqlite3.Error):
            if connection:
                connection.close()
            connection = None
        connections[path] = connection
    return connections[path]


def cache_root():
    return Path(os.path.abspath(Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')).expanduser())) / 'gvat'


def source_stamp(path):
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


class SourceCache:
    def __init__(self, source):
        self.source = source
        self.stamp = source_stamp(source)
        # Filesystem identity is in the stamp. Resolving every parent symlink
        # here would add repeated stat calls for every image in the folder.
        self.key = hashlib.sha256(os.fsencode(os.path.abspath(source))).hexdigest()

    @property
    def fingerprint(self):
        return tuple(self.stamp[2:])

    def read(self):
        try:
            with source_cache_session():
                connection = _connection()
                if connection is None:
                    return None
                row = connection.execute('SELECT record FROM sources WHERE key = ?', (self.key,)).fetchone()
                if row is None:
                    return None
                record = json.loads(row[0])
            if record.get('version') not in (1, 2) or record.get('stamp') != self.stamp:
                return None
            record.setdefault('validated', record['version'] == 1)
            if type(record['validated']) is not bool:
                return None
            kind, count = record['kind'], record['frames']
            if kind not in ('image', 'video', 'none', 'other') or type(count) is not int or count < 0:
                return None
            if record.get('direct_format') not in (None, 'jpg', 'png'):
                return None
            revision = record.get('revision')
            if revision is not None and (not isinstance(revision, str) or len(revision) != 64 or any(character not in '0123456789abcdef' for character in revision)):
                return None
            if kind == 'image' and (count < 1 or len(record['dimensions']) != count or any(
                len(size) != 2 or any(type(value) is not int or value <= 0 for value in size) for size in record['dimensions'])):
                return None
            return record
        except (sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            return None

    def write(self, record):
        if source_stamp(self.source) != self.stamp:
            raise Conflict(f'Source media changed while opening: {self.source.name}. Run GVAT again.')
        data = json.dumps({**record, 'version': 2, 'stamp': self.stamp})
        try:
            with source_cache_session():
                connection = _connection()
                if connection is not None:
                    connection.execute('INSERT OR REPLACE INTO sources (key, record) VALUES (?, ?)', (self.key, data))
        except sqlite3.Error:
            # Cache contention must not block source inspection or rendering.
            connections = getattr(_local, 'connections', None)
            if connections is not None:
                path = cache_root() / 'source-metadata/index.sqlite3'
                connection = connections.get(path)
                if connection is not None:
                    connection.close()
                    connections[path] = None
