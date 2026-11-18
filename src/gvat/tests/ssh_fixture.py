"""An isolated real SSH/SFTP server; keys and files exist only in a temp folder."""

import errno
import os
import posixpath
import socket
import threading
from pathlib import Path

import paramiko


class Files(paramiko.SFTPServerInterface):
    def __init__(self, server, root, reads):
        super().__init__(server)
        self.root, self.reads = root, reads

    def path(self, value):
        result = (self.root / value.lstrip('/')).resolve()
        if not result.is_relative_to(self.root):
            raise OSError(errno.EACCES, 'Outside test server')
        return result

    def canonicalize(self, path):
        return posixpath.normpath('/' + path)

    def list_folder(self, path):
        try:
            entries = []
            for file in self.path(path).iterdir():
                entry = paramiko.SFTPAttributes.from_stat(file.lstat())
                entry.filename = file.name
                entries.append(entry)
            return entries
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.path(path).stat())
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    lstat = stat

    def open(self, path, flags, attr):
        try:
            descriptor = os.open(self.path(path), flags, attr.st_mode or 0o600)
            mode = 'r+b' if flags & os.O_RDWR else 'wb' if flags & os.O_WRONLY else 'rb'
            file = os.fdopen(descriptor, mode)
            handle = paramiko.SFTPHandle(flags)
            if not flags & os.O_WRONLY:
                handle.readfile = file
                self.reads[path] = self.reads.get(path, 0) + 1
            if flags & (os.O_WRONLY | os.O_RDWR):
                handle.writefile = file
            return handle
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def remove(self, path):
        try:
            self.path(path).unlink()
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def posix_rename(self, oldpath, newpath):
        try:
            os.replace(self.path(oldpath), self.path(newpath))
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)

    def chattr(self, path, attr):
        try:
            paramiko.SFTPServer.set_file_attr(self.path(path), attr)
            return paramiko.SFTP_OK
        except OSError as error:
            return paramiko.SFTPServer.convert_errno(error.errno)


class Authentication(paramiko.ServerInterface):
    def __init__(self, key):
        self.key = key

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL if username == 'tester' and key == self.key else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'publickey'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class SSHFixture:
    def __init__(self, root, home):
        self.root, self.home = root, home
        self.reads, self.transports, self.threads = {}, [], []
        self.stop = threading.Event()
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        self.host_key, self.user_key = paramiko.RSAKey.generate(2048), paramiko.RSAKey.generate(2048)
        ssh = home / '.ssh'
        ssh.mkdir(parents=True)
        self.user_key.write_private_key_file(str(ssh / 'id_rsa'))
        (ssh / 'known_hosts').write_text(f'[127.0.0.1]:{self.port} {self.host_key.get_name()} {self.host_key.get_base64()}\n')
        (ssh / 'config').write_text(f'Host sample-host\n  HostName 127.0.0.1\n  Port {self.port}\n  User tester\n  IdentityFile {ssh / "id_rsa"}\n')
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(target=self.serve, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def serve(self, connection):
        transport = paramiko.Transport(connection)
        self.transports.append(transport)
        transport.add_server_key(self.host_key)
        transport.set_subsystem_handler('sftp', paramiko.SFTPServer, Files, root=self.root, reads=self.reads)
        try:
            transport.start_server(server=Authentication(self.user_key))
            while transport.is_active() and not self.stop.wait(.1):
                pass
        except (EOFError, paramiko.SSHException):
            pass
        finally:
            transport.close()

    def close(self):
        self.stop.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=2)
        for thread in self.threads:
            thread.join(timeout=2)
