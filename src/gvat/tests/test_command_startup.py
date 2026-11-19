import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image


@unittest.skipUnless(sys.platform == 'linux', 'The launcher targets Linux')
class CommandStartupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = Path(__file__).resolve().parents[3]
        self.launcher = self.repo / 'src/bin/gvat'
        self.data = self.root / 'sample'
        self.data.mkdir()
        Image.new('RGB', (40, 30), 'green').save(self.data / 'one.png')
        self.environment = {**os.environ, 'XDG_CACHE_HOME': str(self.root / 'cache'),
                            'XDG_CONFIG_HOME': str(self.root / 'config'), 'PYTHONUNBUFFERED': '1'}
        self.process = None

    def tearDown(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
            self.process.communicate(timeout=10)
        self.temporary.cleanup()

    def start(self, command, environment=None):
        self.process = subprocess.Popen(command, cwd=self.root, env=environment or self.environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pending = b''
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not select.select([self.process.stdout], [], [], .5)[0]:
                continue
            data = os.read(self.process.stdout.fileno(), 8192)
            if not data:
                self.fail(self.process.stderr.read().decode())
            pending += data
            for line in pending.splitlines():
                if line.startswith(b'Open http'):
                    return line[5:].decode()
        self.fail('No session URL was printed')

    def test_help_and_version_skip_config_media_and_ssh_imports(self):
        environment = {**self.environment, 'GVAT_CONFIG': str(self.root / 'missing.yml'), 'PYTHONPROFILEIMPORTTIME': '1'}
        for option in ('--help', '--version'):
            result = subprocess.run([str(self.launcher), option], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('gvat_local.cli', result.stderr)
            self.assertNotIn('gvat_local.media', result.stderr)
            self.assertNotIn('paramiko', result.stderr)
            self.assertIn('--data_path' if option == '--help' else 'gvat ', result.stdout)

    def test_launcher_symlink_preserves_relative_sample_paths(self):
        link = self.root / 'command with spaces'
        link.symlink_to(self.launcher)
        url = self.start([str(link), '--data_path=./sample', '--class-names=object', '--yes', '--mode=annotate', '--port=0', '--no-browser'])
        address = urlsplit(url)
        request = urllib.request.Request(f'{address.scheme}://{address.netloc}/api/workspace',
            headers={'Authorization': 'Bearer ' + address.fragment.removeprefix('token=')})
        with urllib.request.urlopen(request, timeout=3) as response:
            self.assertEqual(json.load(response)['data_path'], str(self.data))

    def test_launcher_works_through_a_symlinked_parent_directory(self):
        link = self.root / 'linked commands'
        link.symlink_to(self.launcher.parent, target_is_directory=True)
        result = subprocess.run([str(link / 'gvat'), '--version'], env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('gvat ', result.stdout)

    def test_slow_browser_launch_does_not_block_serving_the_page(self):
        marker, release, finished = (self.root / name for name in ('browser-started', 'release-browser', 'browser-finished'))
        helper = self.root / 'browser.py'
        helper.write_text('import pathlib,sys,time\n'
            'pathlib.Path(sys.argv[1]).touch()\n'
            'deadline=time.monotonic()+15\n'
            'while not pathlib.Path(sys.argv[2]).exists() and time.monotonic()<deadline: time.sleep(.02)\n'
            'pathlib.Path(sys.argv[3]).touch()\n')
        environment = {**self.environment, 'BROWSER': shlex.join([sys.executable, str(helper), str(marker), str(release), str(finished), '%s'])}
        try:
            url = self.start([str(self.launcher), '--data_path=./sample', '--class-names=object', '--yes', '--mode=annotate', '--port=0'], environment)
            deadline = time.monotonic() + 5
            while not marker.is_file() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(marker.is_file(), 'Browser helper did not start')
            address = urlsplit(url)
            with urllib.request.urlopen(f'{address.scheme}://{address.netloc}/', timeout=2) as response:
                self.assertEqual(response.status, 200)
            self.assertFalse(finished.exists())
        finally:
            release.touch()
            deadline = time.monotonic() + 3
            while marker.exists() and not finished.exists() and time.monotonic() < deadline:
                time.sleep(.02)


if __name__ == '__main__':
    unittest.main()
