import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from gvat_local.cvat_xml import CvatDocument, CvatXmlWorkspace
from gvat_local.exports import catalog
from gvat_local.native import NativeSession
from gvat_local.workspace import Conflict


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / 'sample'
        self.media.mkdir()
        Image.new('RGB', (64, 48), 'green').save(self.media / 'one.png')
        self.xml = self.media / 'annotations.xml'
        document = ET.fromstring(CvatDocument.create(['dog', 'cat'], image_count=1))
        image = ET.SubElement(document, 'image', {'id': '0', 'name': 'one.png', 'width': '64', 'height': '48'})
        ET.SubElement(image, 'box', {'label': 'dog', 'xtl': '8', 'ytl': '6', 'xbr': '40', 'ybr': '30', 'occluded': '0'})
        self.xml.write_bytes(ET.tostring(document, encoding='utf-8', xml_declaration=True))
        self.environment = patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.root / 'cache')})
        self.environment.start()
        self.workspace = CvatXmlWorkspace(self.media, self.xml, native_ui=True)
        self.session = NativeSession(self.workspace)
        self.boot = self.session.snapshot()
        self.before = {file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in self.media.iterdir()}

    def tearDown(self):
        self.session.exports.close()
        self.session.chunks.close()
        self.environment.stop()
        self.assertEqual({file.name: (file.read_bytes(), file.stat().st_mtime_ns) for file in self.media.iterdir()}, self.before)
        self.temporary.cleanup()

    def export(self, format, images=False):
        started = self.session.exports.begin(self.session.chunk_view(self.boot['view']), {'format': format,
            'revision': self.boot['revision'], 'save_images': images, 'name': 'my-export.zip'})
        job = self.session.exports.get(started['id'])
        try:
            if started['collect_frames']:
                job.append({'frames': [{'frame': 0, 'annotations': [{'type': 'rectangle', 'label_id': 0,
                    'points': [8, 6, 40, 30], 'attributes': {}, 'id': 1, 'group': 0}]}]})
            result = job.finish().read_bytes()
        finally:
            self.session.exports.release(started['id'])
        return zipfile.ZipFile(io.BytesIO(result))

    def test_complete_cvat_catalog_has_converters_for_every_image_video_format(self):
        from datumaro.components.project import Environment
        environment = Environment()
        self.assertEqual(len(catalog()), 29)
        for plan in catalog().values():
            for name, _ in plan['outputs']:
                self.assertIn(name, environment.exporters)

    def test_every_catalog_format_can_download_with_and_without_media(self):
        for format in catalog():
            for images in (False, True):
                with self.subTest(format=format, images=images), self.export(format, images) as archive:
                    self.assertTrue(archive.namelist())
                    self.assertIsNone(archive.testzip())

    def test_cvat_xml_export_preserves_saved_bytes_and_can_include_media(self):
        with self.export('CVAT for images 1.1', images=True) as archive:
            self.assertEqual(archive.read('annotations.xml'), self.xml.read_bytes())
            self.assertEqual(archive.read('images/one.png'), (self.media / 'one.png').read_bytes())

    def test_coco_yolo_voc_and_segmentation_exports(self):
        with self.export('COCO 1.0') as archive:
            file = next(name for name in archive.namelist() if name.endswith('.json'))
            data = json.loads(archive.read(file))
            self.assertEqual(data['annotations'][0]['bbox'], [8, 6, 32, 24])
        with self.export('YOLO 1.1') as archive:
            label = next(name for name in archive.namelist() if name.endswith('/one.txt'))
            self.assertEqual(archive.read(label).decode().strip().split(), ['0', '0.375000', '0.375000', '0.500000', '0.500000'])
        with self.export('PASCAL VOC 1.1') as archive:
            file = next(name for name in archive.namelist() if name.endswith('one.xml'))
            self.assertEqual(ET.fromstring(archive.read(file)).findtext('object/name'), 'dog')
        with self.export('Segmentation mask 1.1') as archive:
            self.assertTrue(any(name.endswith('.png') for name in archive.namelist()))

    def test_image_annotations_can_export_as_cvat_video(self):
        with self.export('CVAT for video 1.1') as archive:
            root = ET.fromstring(archive.read('annotations.xml'))
            self.assertEqual(root.findtext('meta/task/mode'), 'interpolation')
            self.assertEqual(root.find('track').get('label'), 'dog')
            self.assertEqual(root.findtext('meta/task/original_size/width'), '64')

    def test_export_rejects_stale_xml_and_cleans_temporary_files(self):
        with self.assertRaises(Conflict):
            self.session.exports.begin(self.session.chunk_view(self.boot['view']), {'format': 'COCO 1.0', 'revision': 'old'})
        started = self.session.exports.begin(self.session.chunk_view(self.boot['view']), {'format': 'COCO 1.0', 'revision': self.boot['revision']})
        root = self.session.exports.get(started['id']).root
        self.session.exports.release(started['id'])
        self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
