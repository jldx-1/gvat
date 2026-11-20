"""Read historical XML and ordinary extracted CVAT image exports in place."""

import copy
import io
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cli import parser, prepare
from gvat_local.cvat_xml import CvatDocument, serialize_xml
from gvat_local.native import NativeSession
from gvat_local.workspace import Conflict, WorkspaceError


class XmlImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.data = self.root / 'sample'
        (self.data / 'images/camera').mkdir(parents=True)
        Image.new('RGB', (64, 48), 'green').save(self.data / 'images/camera/one.png')
        self.xml = self.data / 'annotations.xml'
        self.tree = ET.fromstring(CvatDocument.create(['dog'], image_count=1))
        image = ET.SubElement(self.tree, 'image', id='0', name='camera/one.png', width='64', height='48')
        ET.SubElement(image, 'box', label='dog', xtl='8', ytl='6', xbr='40', ybr='30', occluded='0')
        environment = patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache'), 'XDG_CONFIG_HOME': str(self.root / 'config')})
        environment.start()
        self.addCleanup(environment.stop)

    def prepare(self, *flags):
        with redirect_stdout(io.StringIO()):
            return prepare(parser().parse_args(['--data_path', str(self.data), '--mode=annotate', *flags]))

    def native(self, workspace):
        session = NativeSession(workspace)
        self.addCleanup(session.chunks.close)
        self.addCleanup(session.exports.close)
        return session

    def test_both_declared_versions_round_trip_through_native_annotation_and_review(self):
        for version in ('1.0', '1.1'):
            with self.subTest(version=version):
                self.tree.find('version').text = version
                if version == '1.0':
                    self.tree.find('image').attrib.pop('width', None)
                    self.tree.find('image').attrib.pop('height', None)
                else:
                    self.tree.find('image').set('width', '64')
                    self.tree.find('image').set('height', '48')
                self.xml.write_bytes(serialize_xml(self.tree))
                original = self.xml.read_bytes()
                native = self.native(self.prepare('--xml-version=' + version))
                boot = native.snapshot()
                self.assertEqual(self.xml.read_bytes(), original)
                self.assertEqual(boot['frames'][0], 'images/camera/one.png')
                annotations = copy.deepcopy(boot['annotations'])
                annotations['shapes'][0]['points'][0] = 12
                native.save({'view': boot['view'], 'revision': boot['revision'], 'annotations': annotations})
                saved = ET.parse(self.xml)
                self.assertEqual(saved.findtext('version'), version)
                self.assertEqual(saved.find('image').get('name'), 'camera/one.png')
                self.assertEqual(saved.find('image/box').get('xtl'), '12')
                native.issue('POST', None, {'frame': 0, 'position': [1, 2, 20, 30], 'message': 'Check boundary'})
                reviewed = self.native(self.prepare('--mode=review'))
                self.assertEqual(reviewed.snapshot()['issues'][-1]['comments'][0]['message'], 'Check boundary')
                self.assertTrue((self.data / 'reviews.json').is_file())
                self.assertEqual(list((self.data / 'images/camera').iterdir()), [self.data / 'images/camera/one.png'])

    def test_configured_version_reads_missing_tag_without_rewriting_on_open(self):
        self.tree.remove(self.tree.find('version'))
        self.tree.find('image').attrib.pop('width')
        self.tree.find('image').attrib.pop('height')
        self.xml.write_bytes(serialize_xml(self.tree))
        original = self.xml.read_bytes()
        with self.assertRaisesRegex(WorkspaceError, 'version tag is missing'):
            self.prepare()
        config = self.root / 'read.yml'
        config.write_text('import:\n  xml_version: "1.0"\n')
        from gvat_local.configuration import load_config
        with redirect_stdout(io.StringIO()):
            workspace = prepare(parser(load_config(config)).parse_args(['--data_path', str(self.data), '--mode=review']))
        self.assertEqual(workspace.document().version, '1.0')
        self.assertEqual(self.xml.read_bytes(), original)
        native = self.native(workspace)
        boot = native.snapshot()
        annotations = copy.deepcopy(boot['annotations'])
        annotations['shapes'][0]['points'][0] = 10
        native.save({'view': boot['view'], 'revision': boot['revision'], 'annotations': annotations})
        self.assertEqual(ET.parse(self.xml).findtext('version'), '1.0')

    def test_explicit_version_mismatch_and_unsupported_versions_never_change_xml(self):
        for version, flag, message in [('1.1', '1.0', 'declares version'), ('2.0', 'auto', 'Unsupported CVAT XML'), ('1.2', '1.1', 'Unsupported CVAT XML')]:
            self.tree.find('version').text = version
            self.xml.write_bytes(serialize_xml(self.tree))
            original = self.xml.read_bytes()
            with self.subTest(version=version, flag=flag), self.assertRaisesRegex(WorkspaceError, message):
                self.prepare('--xml-version=' + flag)
            self.assertEqual(self.xml.read_bytes(), original)

    def test_nested_basename_matching_is_unique_and_relative_paths_disambiguate(self):
        self.tree.find('image').set('name', 'one.png')
        self.xml.write_bytes(serialize_xml(self.tree))
        self.assertEqual(list(self.prepare().document().images), ['images/camera/one.png'])
        (self.data / 'images/other').mkdir()
        Image.new('RGB', (64, 48)).save(self.data / 'images/other/one.png')
        with self.assertRaisesRegex(WorkspaceError, 'Ambiguous CVAT XML image'):
            self.prepare()
        self.tree.find('image').set('name', 'images/camera/one.png')
        self.xml.write_bytes(serialize_xml(self.tree))
        workspace = self.prepare()
        self.assertEqual(list(workspace.document().images), ['images/camera/one.png'])
        (self.data / 'images/camera/one.png').write_bytes(b'changed')
        with self.assertRaises(Conflict):
            workspace.manifest()

    def test_hidden_and_external_symlink_folders_are_not_read(self):
        (self.data / '.hidden').mkdir()
        Image.new('RGB', (64, 48)).save(self.data / '.hidden/hidden.png')
        outside = self.root / 'outside'
        outside.mkdir()
        Image.new('RGB', (64, 48)).save(outside / 'outside.png')
        (self.data / 'linked').symlink_to(outside, target_is_directory=True)
        (self.data / 'escaped.png').symlink_to(outside / 'outside.png')
        self.xml.write_bytes(serialize_xml(self.tree))
        self.assertEqual(list(self.prepare()._frames), ['images/camera/one.png'])

    def test_headless_export_preserves_xml_paths_and_reads_configured_missing_version(self):
        self.xml.write_bytes(serialize_xml(self.tree))
        command = [sys.executable, '-m', 'gvat_local', '--data_path', str(self.data)]
        exported = self.root / 'with-media.zip'
        result = subprocess.run(command + ['--export-dataset', str(exported), '--format=CVAT for images 1.1'],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(set(archive.namelist()), {'annotations.xml', 'images/camera/one.png'})
            self.assertEqual(archive.read('annotations.xml'), self.xml.read_bytes())
        self.tree.remove(self.tree.find('version'))
        self.xml.write_bytes(serialize_xml(self.tree))
        original = self.xml.read_bytes()
        config = self.root / 'import.yml'
        config.write_text('import:\n  xml_version: "1.1"\n')
        exported = self.root / 'labels.zip'
        result = subprocess.run(command + ['--config', str(config), '--export-labels', str(exported), '--format=CVAT for images 1.1'],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(set(archive.namelist()), {'annotations.xml'})
            self.assertEqual(ET.fromstring(archive.read('annotations.xml')).findtext('version'), '1.1')
        self.assertEqual(self.xml.read_bytes(), original)


if __name__ == '__main__':
    unittest.main()
