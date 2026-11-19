import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gvat_local.cli import parser
from gvat_local.configuration import BUNDLED_CONFIG, load_config
from gvat_local.workspace import WorkspaceError


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'XDG_CONFIG_HOME': str(self.root)})
        self.environment.start()
        self.custom = self.root / 'custom.yml'

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_defaults_and_user_overrides_reach_cli_and_cli_flags_win(self):
        self.custom.write_text('media:\n  preferred_type: video\nplayer:\n  step: 3\n  pixelated: true\nexport:\n  labels_format: "YOLO 1.1"\nbuffer:\n  seconds: 20\n  max_memory: "2GiB"\n')
        settings = load_config(self.custom)
        args = parser(settings).parse_args([])
        self.assertEqual((args.step, args.pixelated, args.buffer_seconds, args.buffer_memory), (3, True, 20, 2 * 1024 ** 3))
        self.assertEqual(settings['export']['labels_format'], 'YOLO 1.1')
        self.assertEqual(args.media_type, 'video')
        args = parser(settings).parse_args(['--media-type=images', '--step=7', '--smooth-images', '--buffer-seconds=2', '--buffer-memory=512MiB'])
        self.assertEqual(args.media_type, 'images')
        self.assertEqual((args.step, args.pixelated, args.buffer_seconds, args.buffer_memory), (7, False, 2, 512 * 1024 ** 2))
        self.assertEqual(load_config()['player']['step'], 10)

    def test_invalid_defaults_fail_with_actionable_error(self):
        for text in ('player:\n  step: 0', 'player:\n  pixelated: sometimes', 'export:\n  labels_format: unknown',
                     'buffer:\n  seconds: .nan', 'buffer:\n  max_memory: -3GiB', 'media:\n  preferred_type: unknown', 'unknown: {}'):
            self.custom.write_text(text)
            with self.subTest(text=text), self.assertRaises(WorkspaceError):
                load_config(self.custom)

    def test_first_install_copies_config_and_reinstall_preserves_edits(self):
        tool = Path(__file__).resolve().parents[1] / 'tools/install_config.py'
        command = [sys.executable, str(tool), str(BUNDLED_CONFIG)]
        subprocess.run(command, check=True, capture_output=True)
        destination = self.root / 'gvat/config.yml'
        self.assertEqual(destination.read_bytes(), BUNDLED_CONFIG.read_bytes())
        destination.write_text('player:\n  step: 5\n')
        subprocess.run(command, check=True, capture_output=True)
        self.assertEqual(load_config()['player']['step'], 5)
        self.assertEqual(destination.read_text(), 'player:\n  step: 5\n')


if __name__ == '__main__':
    unittest.main()
