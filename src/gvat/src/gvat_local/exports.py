"""CVAT's image/video export catalog and pinned Datumaro conversion engine."""

from __future__ import annotations

import ast
import copy
import functools
import importlib.util
import io
import json
import math
import re
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace

from .cvat_xml import CvatDocument, CvatXmlWorkspace, serialize_xml
from .workspace import Conflict, WorkspaceError, digest

UPSTREAM = Path(__file__).resolve().parents[3] / 'cvat/cvat/apps/dataset_manager/formats'


@functools.lru_cache(maxsize=1)
def catalog():
    """Read names, options and transform order from this checkout's CVAT source."""
    formats = {}
    for source in sorted(UPSTREAM.glob('*.py')):
        tree = ast.parse(source.read_text())
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            for decorator in function.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Name) or decorator.func.id != 'exporter':
                    continue
                metadata = {entry.arg: entry.value for entry in decorator.keywords}
                # This local workflow handles images/video, not audio/point clouds.
                if 'dimension' in metadata or ast.literal_eval(metadata.get('enabled', ast.Constant(True))) is False:
                    continue
                name = ast.literal_eval(metadata['name']) + ' ' + ast.literal_eval(metadata['version'])
                transforms, outputs = [], []
                for node in ast.walk(function):
                    if not isinstance(node, ast.Call):
                        continue
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'transform':
                        transform = node.args[0]
                        transforms.append((transform.id if isinstance(transform, ast.Name) else ast.literal_eval(transform),
                                           {arg.arg: ast.literal_eval(arg.value) for arg in node.keywords}))
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'export':
                        output = ast.literal_eval(node.args[1]) if len(node.args) > 1 else next(ast.literal_eval(arg.value) for arg in node.keywords if arg.arg == 'format')
                        options = {}
                        for arg in node.keywords:
                            if arg.arg in ('save_media', 'format'):
                                continue
                            try:
                                options[arg.arg] = ast.literal_eval(arg.value)
                            except (ValueError, TypeError):
                                if arg.arg == 'label_map':
                                    options[arg.arg] = 'colormap_rgb' if isinstance(arg.value, ast.DictComp) else 'colormap'
                                else:
                                    raise WorkspaceError(f'Unsupported upstream export option: {name}/{arg.arg}')
                        outputs.append((output, options))
                    if isinstance(node.func, ast.Name) and node.func.id == '_export_common':
                        options = {arg.arg: ast.literal_eval(arg.value) for arg in node.keywords if arg.arg}
                        outputs.append((options.pop('format_name'), options))
                formats[name] = {'name': name, 'ext': 'ZIP', 'version': ast.literal_eval(metadata['version']),
                                 'dimension': '2d', 'enabled': True, 'module': source.stem,
                                 'transforms': transforms, 'outputs': outputs}
    return formats


def public_formats():
    return {'importers': [], 'exporters': [{key: plan[key] for key in ('name', 'ext', 'version', 'dimension', 'enabled')}
                                     for plan in catalog().values()]}


@functools.lru_cache(maxsize=None)
def upstream_transform(name, module):
    """Load CVAT's standalone transforms without importing its Django bindings."""
    import datumaro as dm
    if module == 'transformations':
        spec = importlib.util.spec_from_file_location('gvat_cvat_transforms', UPSTREAM / 'transformations.py')
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return getattr(loaded, name)
    # These three CVAT format helpers are pure Datumaro classes in modules whose
    # other imports require Django. Compile only the trusted, pinned class body.
    tree = ast.parse((UPSTREAM / f'{module}.py').read_text())
    node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    namespace = {key: getattr(dm, key) for key in ('ItemTransform', 'AnnotationType', 'Label', 'Caption', 'LabelCategories')}
    namespace['__name__'] = 'gvat_cvat_export_helpers'
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(UPSTREAM / f'{module}.py'), 'exec'), namespace)
    return namespace[name]


def _annotation(shape, labels, width, height):
    import datumaro as dm
    kind = shape['type']
    label = shape['label_id']
    if not 0 <= label < len(labels):
        raise WorkspaceError('Unknown annotation export label')
    attributes = dict(shape.get('attributes', {}))
    for spec in labels[label]['attributes']:
        if spec['name'] not in attributes:
            continue
        if spec['input_type'] == 'checkbox':
            attributes[spec['name']] = str(attributes[spec['name']]).lower() == 'true'
        elif spec['input_type'] == 'number':
            attributes[spec['name']] = float(attributes[spec['name']])
    attributes.update(occluded=shape.get('occluded', False), rotation=shape.get('rotation', 0),
                      source=shape.get('source', 'manual'), outside=False, keyframe=shape.get('keyframe', True))
    if shape.get('track_id') is not None:
        attributes['track_id'] = shape['track_id']
    common = {'label': label, 'attributes': attributes, 'id': shape.get('id', 0), 'group': shape.get('group', 0)}
    points = shape.get('points', [])
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in points):
        raise WorkspaceError('Export coordinates must be finite')
    if kind == 'tag':
        return dm.Label(**common)
    common['z_order'] = shape.get('z_order', 0)
    if kind == 'rectangle':
        return dm.Bbox(points[0], points[1], points[2] - points[0], points[3] - points[1], **common)
    if kind in ('polygon', 'polyline', 'points'):
        return {'polygon': dm.Polygon, 'polyline': dm.PolyLine, 'points': dm.Points}[kind](points, **common)
    if kind == 'ellipse':
        cx, cy, right, top = points
        return dm.Ellipse(2 * cx - right, top, right, 2 * cy - top, **common)
    if kind == 'mask':
        converter = upstream_transform('MaskConverter', 'transformations')
        return converter.cvat_rle_to_dm_rle(SimpleNamespace(points=points), height, width).wrap(**common)
    if kind == 'cuboid':
        return dm.Bbox(min(points[::2]), min(points[1::2]), max(points[::2]) - min(points[::2]),
                       max(points[1::2]) - min(points[1::2]), **common)
    if kind == 'skeleton':
        return dm.Skeleton([_annotation(element, labels, width, height) for element in shape.get('elements', [])], **common)
    raise WorkspaceError(f'Unsupported annotation export shape: {kind}')


class ExportJob:
    def __init__(self, workspace, view, body):
        self.workspace, self.view = workspace, copy.deepcopy(view)
        self.format = body['format']
        self.plan = catalog()[self.format]
        self.save_images = bool(body.get('save_images'))
        self.revision = body['revision']
        self.saved_xml = workspace.xml_file.read_bytes()
        if digest(self.saved_xml) != self.revision:
            raise Conflict('XML changed while starting the export. Reload and try again.')
        self.lock = threading.RLock()
        self.temporary = tempfile.TemporaryDirectory(prefix='.export-', dir=workspace.data)
        self.root = Path(self.temporary.name)
        self.records = self.root / 'frames.jsonl'
        self.records.touch()
        self.received = 0
        requested = Path(str(body.get('name') or 'annotations.zip')).name
        self.filename = re.sub(r'[^\w. -]', '_', requested)
        if not self.filename.lower().endswith('.zip'):
            self.filename += '.zip'
        mode = workspace.document().root.findtext('meta/task/mode', 'annotation')
        self.exact_xml = (self.format == ('CVAT for video 1.1' if mode == 'interpolation' else 'CVAT for images 1.1')
                          and ET.fromstring(self.saved_xml).findtext('version') == '1.1')

    def append(self, payload):
        frames = payload.get('frames')
        if not isinstance(frames, list) or not frames:
            raise WorkspaceError('Expected a nonempty export frame batch')
        with self.lock:
            for offset, frame in enumerate(frames):
                if not isinstance(frame, dict) or frame.get('frame') != self.received + offset or not isinstance(frame.get('annotations'), list):
                    raise WorkspaceError('Export frames must arrive in order')
                if frame['frame'] >= len(self.view['keys']):
                    raise WorkspaceError('Export frame is outside this task')
            with self.records.open('a') as output:
                for frame in frames:
                    output.write(json.dumps(frame, allow_nan=False) + '\n')
            self.received += len(frames)

    def items(self):
        with self.records.open() as source:
            for line in source:
                yield json.loads(line)

    def _check_revision(self):
        with self.workspace.lock:
            self.workspace._check_sources()
            if digest(self.workspace.xml_file.read_bytes()) != self.revision:
                raise Conflict('XML changed while preparing this export. Save/reload and export again.')

    def _dataset(self):
        import datumaro as dm
        import numpy as np
        labels = self.view['labels']
        categories = dm.LabelCategories.from_iterable(label['name'] for label in labels)
        job = self

        class LocalExtractor(dm.DatasetBase):
            @property
            def is_stream(self):
                return True

            def media_type(self):
                return dm.Image

            def categories(self):
                return {dm.AnnotationType.label: categories}

            def __len__(self):
                return job.received

            def __iter__(self):
                for item in job.items():
                    frame = item['frame']
                    key = job.view['keys'][frame]
                    metadata = job.view['frames'][frame]
                    width, height = metadata['width'], metadata['height']
                    def load(path=key):
                        from PIL import Image
                        with job.workspace.lock:
                            source = job.workspace._ensure_frame(path)
                        with Image.open(source) as opened:
                            # Datumaro's media API uses BGR pixel arrays.
                            return np.asarray(opened.convert('RGB'))[:, :, ::-1].copy()
                    media = dm.Image.from_numpy(load, size=(height, width), ext='.png')
                    yield dm.DatasetItem(id=Path(key).with_suffix('').as_posix(), subset='default', media=media,
                                         attributes={'frame': job.view['numbers'][frame]},
                                         annotations=[_annotation(shape, labels, width, height) for shape in item['annotations']])
        # Some CVAT formats (VOC/masks, for example) require random access.
        # The input spool stays on disk; Datumaro chooses the needed caching.
        return dm.Dataset.from_extractors(LocalExtractor())

    def finish(self):
        self._check_revision()
        result = self.root / 'annotations.zip'
        if self.exact_xml:
            document = self.workspace.document()
            with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('annotations.xml', self.saved_xml)
                if self.save_images:
                    for key in self.view['keys']:
                        source, page = self.workspace._frames[key]
                        file = source.path if source.kind == 'image' and source.frames == 1 else self.workspace._ensure_frame(key)
                        image = document.images.get(key)
                        # Match the unchanged XML's paths, even when the source
                        # came from an extracted images/ wrapper directory.
                        name = '/'.join(filter(None, (image.get('subset', ''), image.get('name')))) if image is not None else key
                        archive.write(file, 'images/' + name)
            self._check_revision()
            return result
        if self.received != len(self.view['keys']):
            raise WorkspaceError('Export is missing annotation frames')
        if self.format.startswith('CVAT for '):
            converted = self._converted_xml(result)
            self._check_revision()
            return converted
        dataset = self._dataset()
        for name, options in self.plan['transforms']:
            module = {'LabelToCaption': 'icdar', 'LabelAttrToAttr': 'market1501', 'KeepTracks': 'mots'}.get(name, 'transformations')
            transform = name if not name[:1].isupper() else upstream_transform(name, module)
            dataset.transform(transform, **options)
        format_name, options = self.plan['outputs'][0 if self.save_images or len(self.plan['outputs']) == 1 else -1]
        options = dict(options)
        colormap = {'background': [(0, 0, 0), [], []]}
        for number, label in enumerate(self.view['labels']):
            color = label.get('color') or '#%06x' % ((number + 1) * 123457 % 0xffffff)
            colormap[label['name']] = [tuple(int(color[index:index + 2], 16) for index in (1, 3, 5)), [], []]
        if options.get('label_map') == 'colormap':
            options['label_map'] = colormap
        elif options.get('label_map') == 'colormap_rgb':
            options['label_map'] = {name: values[0] for name, values in colormap.items()}
        output = self.root / 'output'
        dataset.export(str(output), format_name, save_media=self.save_images, **options)
        with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
            for file in sorted(output.rglob('*')):
                if file.is_file():
                    archive.write(file, file.relative_to(output).as_posix())
        self._check_revision()
        return result

    def _converted_xml(self, result):
        # Reuse the same CVAT XML writer used by Save; this conversion never
        # writes to the selected annotation file or touches source media.
        from .native import NativeSession
        root = copy.deepcopy(self.workspace.document().root)
        legacy = root.findtext('version') != '1.1'
        video = self.format == 'CVAT for video 1.1'
        root.find('version').text = '1.1'
        first = self.view['frames'][0]
        defaults = ET.fromstring(CvatDocument.create([label['name'] for label in self.view['labels']],
            (first['width'], first['height'], len(self.view['keys'])) if video else None, image_count=len(self.view['keys'])))
        task = root.find('meta/task')
        if task is None:
            task = copy.deepcopy(defaults.find('meta/task'))
            root.find('meta').append(task)
        else:
            for child in defaults.find('meta/task'):
                if task.find(child.tag) is None:
                    task.append(copy.deepcopy(child))
        task.find('mode').text = 'interpolation' if video else 'annotation'
        if legacy:
            for label in task.findall('labels/label'):
                definition = next((item for item in self.view['labels'] if item['name'] == label.findtext('name')), None)
                if definition is None:
                    continue
                attributes = label.find('attributes')
                if attributes is None:
                    attributes = ET.SubElement(label, 'attributes')
                attributes.clear()
                for spec in definition['attributes']:
                    attribute = ET.SubElement(attributes, 'attribute')
                    for field, value in (('name', spec['name']), ('mutable', str(spec['mutable'])),
                                         ('input_type', spec['input_type']), ('default_value', spec['default_value']),
                                         ('values', '\n'.join(spec['values']))):
                        ET.SubElement(attribute, field).text = value
        for child in list(root):
            if child.tag in ('image', 'track'):
                root.remove(child)
        if not video:
            for number, key in enumerate(self.view['keys']):
                frame = self.view['frames'][number]
                ET.SubElement(root, 'image', {'id': str(number), 'name': key, 'width': str(frame['width']), 'height': str(frame['height'])})
        destination = self.root / 'annotations.xml'
        destination.write_bytes(serialize_xml(root))
        workspace = CvatXmlWorkspace(self.workspace.source_root, destination, native_ui=True)
        native = NativeSession(workspace)
        view = copy.deepcopy(self.view)
        view.update(revision=digest(destination.read_bytes()), nodes={}, shape_tracks={})
        native.views['export'] = view
        collection = {'shapes': [], 'tags': [], 'tracks': []}
        for item in self.items():
            for shape in item['annotations']:
                shape = copy.deepcopy(shape)
                shape['frame'] = item['frame']
                shape['clientID'] = len(collection['shapes']) + len(collection['tags']) + 1
                shape['attributes'] = [{'spec_id': spec['id'], 'value': shape.get('attributes', {}).get(spec['name'], spec['default_value'])}
                                       for spec in view['labels'][shape['label_id']]['attributes']]
                collection['tags' if shape['type'] == 'tag' else 'shapes'].append(shape)
        try:
            native.save({'view': 'export', 'revision': view['revision'], 'annotations': collection})
        finally:
            native.chunks.close()
        with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(destination, 'annotations.xml')
            if self.save_images:
                for key in self.view['keys']:
                    archive.write(self.workspace._ensure_frame(key), 'images/' + key)
        return result

    def close(self):
        self.temporary.cleanup()


class LocalExports:
    def __init__(self, workspace):
        self.workspace = workspace
        self.jobs = {}
        self.lock = threading.RLock()

    def begin(self, view, body):
        if body.get('format') not in catalog():
            raise WorkspaceError('Unknown CVAT annotation export format')
        with self.workspace.lock:
            self.workspace._check_sources()
            if body.get('revision') != digest(self.workspace.xml_file.read_bytes()):
                raise Conflict('XML changed on disk. Reload before exporting.')
            job = ExportJob(self.workspace, view, body)
        with self.lock:
            if len(self.jobs) >= 4:
                job.close()
                raise WorkspaceError('Too many unfinished exports. Finish an export or restart GVAT.')
            identifier = str(uuid.uuid4())
            self.jobs[identifier] = job
        return {'id': identifier, 'collect_frames': not job.exact_xml, 'filename': job.filename}

    def get(self, identifier):
        with self.lock:
            if identifier not in self.jobs:
                raise WorkspaceError('Unknown or completed export')
            return self.jobs[identifier]

    def release(self, identifier):
        with self.lock:
            job = self.jobs.pop(identifier, None)
        if job:
            job.close()

    def close(self):
        for identifier in list(self.jobs):
            self.release(identifier)
