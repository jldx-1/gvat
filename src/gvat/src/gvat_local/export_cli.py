"""Headless exports using CVAT's Python interpolation and Datumaro converters."""

import ast
import copy
import functools
import heapq
import math
import os
import shutil
import tempfile
from itertools import groupby
from pathlib import Path
from types import SimpleNamespace

from .exports import UPSTREAM
from .workspace import WorkspaceError


@functools.lru_cache(maxsize=1)
def interpolate():
    import numpy as np

    source = UPSTREAM.parent / 'annotation.py'
    tree = ast.parse(source.read_text())
    manager = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'TrackManager')
    method = copy.deepcopy(next(node for node in manager.body if isinstance(node, ast.FunctionDef) and node.name == 'get_interpolated_shapes'))
    method.decorator_list = []
    # Only this pure upstream method is needed, not Django/DRF/database bindings.
    models = ast.parse((UPSTREAM.parents[1] / 'engine/models.py').read_text())
    enums = {}
    for name in ('ShapeType', 'DimensionType'):
        definition = next(node for node in models.body if isinstance(node, ast.ClassDef) and node.name == name)
        enums[name] = SimpleNamespace(**{node.targets[0].id: ast.literal_eval(node.value)
            for node in definition.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)})
    namespace = dict(np=np, math=math, heapq=heapq, faster_deepcopy=copy.deepcopy, **enums)
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, method], type_ignores=[]))
    exec(compile(module, str(source), 'exec'), namespace)
    return namespace['get_interpolated_shapes']


def frame_records(boot):
    labels = {label['id']: label for label in boot['job']['labels']}

    def serialize(shape, parent=None):
        label = labels[(parent or shape)['label_id']]
        values = {item['spec_id']: item['value'] for item in (parent or {}).get('attributes', []) + shape.get('attributes', [])}
        record = dict(shape, label_id=label['id'], attributes={spec['name']: values.get(spec['id'], spec['default_value']) for spec in label['attributes']})
        if parent:
            record.update(track_id=parent['id'], id=parent['id'], group=parent.get('group', 0), source=parent.get('source', 'manual'))
        record['elements'] = [serialize(element) for element in shape.get('elements', [])]
        return record

    def static():
        for kind in ('shapes', 'tags'):
            for shape in boot['annotations'][kind]:
                if kind == 'tags':
                    shape = dict(shape, type='tag')
                yield shape['frame'], serialize(shape)

    def track_records(track):
        for shape in interpolate()(copy.deepcopy(track), 0, len(boot['frames']), '2d', streaming=True):
            if not shape.get('outside'):
                yield shape['frame'], serialize(shape, track)

    streams = [iter(sorted(static(), key=lambda item: item[0]))]
    streams.extend(track_records(track) for track in boot['annotations']['tracks'])
    grouped = iter(groupby(heapq.merge(*streams, key=lambda item: item[0]), key=lambda item: item[0]))
    current, values = next(grouped, (None, None))
    for frame in range(len(boot['frames'])):
        annotations = []
        if current == frame:
            annotations = [record for _, record in values]
            current, values = next(grouped, (None, None))
        yield {'frame': frame, 'annotations': annotations}


def export(workspace, output, format, images=False, overwrite=False):
    from .native import NativeSession

    output = output.expanduser().absolute()
    if output.exists() and not overwrite:
        raise WorkspaceError('Export destination exists. Choose another filename or pass --overwrite.')
    protected = {workspace.xml_file, workspace.source_root / 'reviews.json', *(source.path for source in workspace.sources)}
    if output.resolve() in protected:
        raise WorkspaceError('Export output must not replace source media, annotation XML or reviews.json')
    native = NativeSession(workspace)
    try:
        boot = native.snapshot()
        started = native.exports.begin(native.chunk_view(boot['view']), {'format': format, 'save_images': images,
                                      'revision': boot['revision'], 'name': output.name})
        job = native.exports.get(started['id'])
        if started['collect_frames']:
            frames = []
            for record in frame_records(boot):
                frames.append(record)
                if len(frames) == 100:
                    job.append({'frames': frames})
                    frames = []
            if frames:
                job.append({'frames': frames})
        archive = job.finish()
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.gvat-export-', dir=output.parent)
        try:
            with os.fdopen(descriptor, 'wb') as target, archive.open('rb') as source:
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            if overwrite:
                os.replace(temporary, output)
            else:
                os.link(temporary, output)  # Atomic refusal if a destination appears meanwhile.
        finally:
            Path(temporary).unlink(missing_ok=True)
    finally:
        native.chunks.close()
        native.exports.close()
    print(f'Exported {format}: {output}')
