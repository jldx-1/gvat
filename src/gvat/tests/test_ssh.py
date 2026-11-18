import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cli import parser, prepare
from gvat_local.cvat_xml import CvatDocument
from gvat_local.native import NativeSession
from gvat_local.ssh import SSHLocation, input_locations, location
from gvat_local.workspace import Conflict, WorkspaceError
from ssh_fixture import SSHFixture


class SSHTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server_root = self.root / 'server'
        self.data = self.server_root / 'sample'
        self.data.mkdir(parents=True)
        Image.new('RGB', (64, 48), 'green').save(self.data / 'one.png')
        self.media = (self.data / 'one.png').read_bytes()
        (self.server_root / 'labels.txt').write_text('dog\n')
        document = ET.fromstring(CvatDocument.create(['dog'], image_count=1))
        image = ET.SubElement(document, 'image', id='0', name='one.png', width='64', height='48')
        ET.SubElement(image, 'box', label='dog', xtl='8', ytl='6', xbr='40', ybr='30', occluded='0')
        (self.data / 'annotations.xml').write_bytes(ET.tostring(document, encoding='utf-8'))
        self.server = SSHFixture(self.server_root, self.root / 'home')
        self.environment = patch.dict(os.environ, {'HOME': str(self.root / 'home'), 'XDG_CACHE_HOME': str(self.root / 'cache')})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.server.close()
        self.assertEqual((self.data / 'one.png').read_bytes(), self.media)
        self.assertFalse(any(file.name.startswith('.') for file in self.data.iterdir()))
        self.temporary.cleanup()

    def arguments(self, *extra):
        return parser().parse_args(['--data_path=ssh://sample-host/sample', '--mode=annotate', *extra])

    def test_ssh_saves_xml_reviews_and_reopens_without_redownloading_media(self):
        # Resolve and keep one connection alive for the complete editing session.
        args = self.arguments()
        with input_locations(args) as remote, redirect_stdout(io.StringIO()):
            workspace = prepare(args)
            remote.bind(workspace)
            native = NativeSession(workspace)
            boot = native.snapshot()
            payload = {'view': boot['view'], 'revision': boot['revision'], 'annotations': copy.deepcopy(boot['annotations'])}
            payload['annotations']['shapes'][0]['points'][0] = 11
            native.save(payload)
            issue = native.issue('POST', None, {'frame': 0, 'position': [1, 2, 20, 30], 'message': 'Check the paws'})
            native.comment({'issue': issue['id'], 'message': 'Keep the tail'})
            native.chunks.close()
        self.assertEqual(ET.parse(self.data / 'annotations.xml').find('image/box').get('xtl'), '11')
        findings = json.loads((self.data / 'reviews.json').read_text())
        self.assertEqual(findings['issues'][0]['image'], 'one.png')
        self.assertNotIn(str(self.root), (self.data / 'reviews.json').read_text())
        reads = self.server.reads.get('/sample/one.png', 0)
        args = self.arguments('--mode=review')
        with input_locations(args) as remote, redirect_stdout(io.StringIO()):
            workspace = prepare(args)
            remote.bind(workspace)
            native = NativeSession(workspace)
            self.assertEqual(native.snapshot()['issues'][0]['comments'][1]['message'], 'Keep the tail')
            native.chunks.close()
        self.assertEqual(self.server.reads.get('/sample/one.png', 0), reads)

    def test_remote_changes_are_not_overwritten_and_headless_export_works(self):
        args = self.arguments()
        with input_locations(args) as remote, redirect_stdout(io.StringIO()):
            workspace = prepare(args)
            remote.bind(workspace)
            native = NativeSession(workspace)
            boot = native.snapshot()
            changed = (self.data / 'annotations.xml').read_bytes() + b'\n'
            (self.data / 'annotations.xml').write_bytes(changed)
            with self.assertRaises(Conflict):
                native.save({'view': boot['view'], 'revision': boot['revision'], 'annotations': boot['annotations']})
            self.assertEqual((self.data / 'annotations.xml').read_bytes(), changed)
            with self.assertRaises(Conflict):
                remote.write_file(workspace.xml_file, b'not XML', boot['revision'])
            native.chunks.close()
        output = self.root / 'remote-export.zip'
        result = subprocess.run([sys.executable, '-m', 'gvat_local', '--data_path=ssh://sample-host/sample',
            '--export-labels', str(output), '--format=CVAT for images 1.1'], env=os.environ, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(output.is_file())
        self.assertNotIn('Open http', result.stdout)

    def test_missing_xml_uses_remote_shared_labels_and_checks_media_changes(self):
        (self.data / 'annotations.xml').unlink()
        args = self.arguments('--yes')
        with input_locations(args) as remote, redirect_stdout(io.StringIO()):
            workspace = prepare(args)
            remote.bind(workspace)
            self.assertEqual(ET.parse(self.data / 'annotations.xml').findtext('meta/task/labels/label/name'), 'dog')
            self.assertFalse((self.data / 'labels.txt').exists())
            (self.data / 'one.png').write_bytes(self.media + b'changed')
            with self.assertRaises(Conflict):
                remote.check_sources(force=True)
            (self.data / 'one.png').write_bytes(self.media)

    def test_location_parsing_preserves_spaces_and_supports_ports_and_scp(self):
        self.assertEqual(location('ssh://tester@host:2222/data/a%20b'), SSHLocation('host', '/data/a b', 'tester', 2222))
        self.assertEqual(location('tester@host:~/sample'), SSHLocation('host', '~/sample', 'tester'))
        self.assertEqual(location('./sample'), Path('./sample'))
        with self.assertRaises(Exception):
            location('ssh://user:password@host/sample')


if __name__ == '__main__':
    unittest.main()
