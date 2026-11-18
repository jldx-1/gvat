import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cvat_xml import CvatDocument, CvatXmlWorkspace
from gvat_local.export_cli import export
from gvat_local.workspace import WorkspaceError


class HeadlessExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / 'sample'
        self.data.mkdir()
        for index in range(3):
            Image.new('RGB', (64, 48), 'green').save(self.data / f'{index}.png')
        root = ET.fromstring(CvatDocument.create(['dog'], image_count=3))
        for index in range(3):
            ET.SubElement(root, 'image', id=str(index), name=f'{index}.png', width='64', height='48')
        track = ET.SubElement(root, 'track', id='7', label='dog')
        for frame, left in ((0, 8), (2, 24)):
            ET.SubElement(track, 'box', frame=str(frame), xtl=str(left), ytl='6', xbr=str(left + 20), ybr='30', outside='0', occluded='0', keyframe='1')
        self.xml = self.data / 'annotations.xml'
        self.xml.write_bytes(ET.tostring(root, encoding='utf-8'))
        self.before = {file.name: file.read_bytes() for file in self.data.iterdir()}
        self.environment = patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')})
        self.environment.start()

    def tearDown(self):
        self.assertEqual({file.name: file.read_bytes() for file in self.data.iterdir()}, self.before)
        self.environment.stop()
        self.temporary.cleanup()

    def test_headless_coco_expands_native_cvat_interpolation_and_preserves_source(self):
        destination = self.root / 'output.zip'
        with redirect_stdout(io.StringIO()):
            export(CvatXmlWorkspace(self.data, self.xml, native_ui=True), destination, 'COCO 1.0')
        with zipfile.ZipFile(destination) as archive:
            name = next(name for name in archive.namelist() if name.endswith('.json'))
            annotations = json.loads(archive.read(name))
        self.assertEqual([item['bbox'] for item in annotations['annotations']], [[8, 6, 20, 24], [16, 6, 20, 24], [24, 6, 20, 24]])
        self.assertEqual([item['attributes']['track_id'] for item in annotations['annotations']], [7, 7, 7])

    def test_cli_can_export_without_launching_server_or_browser_and_lists_formats(self):
        environment = {**os.environ, 'XDG_CACHE_HOME': str(self.root / 'cache')}
        output = self.root / 'native.zip'
        result = subprocess.run([sys.executable, '-m', 'gvat_local', '--data_path', str(self.data),
            '--export-labels', str(output), '--format', 'CVAT for images 1.1'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Open http', result.stdout)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read('annotations.xml'), self.xml.read_bytes())
        result = subprocess.run([sys.executable, '-m', 'gvat_local', '--list-formats'], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 29)

    def test_exports_refuse_existing_destinations_and_protect_source_files(self):
        workspace = CvatXmlWorkspace(self.data, self.xml, native_ui=True)
        for destination, overwrite in ((self.xml, True), (self.data / '0.png', True), (self.xml, False)):
            with self.assertRaises(WorkspaceError):
                export(workspace, destination, 'COCO 1.0', overwrite=overwrite)

    def test_cli_uses_configured_labels_format_when_format_is_omitted(self):
        config = self.root / 'config.yml'
        config.write_text('export:\n  labels_format: "YOLO 1.1"\n')
        output = self.root / 'from-config.zip'
        result = subprocess.run([sys.executable, '-m', 'gvat_local', '--config', str(config), '--data_path', str(self.data),
            '--export-labels', str(output)], env=os.environ, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(output) as archive:
            self.assertTrue(any(name.endswith('/0.txt') for name in archive.namelist()))


if __name__ == '__main__':
    unittest.main()
