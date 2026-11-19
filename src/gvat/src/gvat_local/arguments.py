"""CLI schema with lazy value parsers so help needs no media/SSH imports."""

import argparse
from pathlib import Path

from . import __version__


def location(value):
    from .ssh import location as parse
    return parse(value)


def memory_size(value):
    from .buffering import memory_size as parse
    return parse(value)


def parser(settings=None):
    if settings is None:
        from .configuration import load_config
        settings = load_config()
    result = argparse.ArgumentParser(prog='gvat', description='Label or review a local directory using the CVAT canvas. No Docker, accounts, or Git integration.')
    result.set_defaults(settings=settings)
    result.add_argument('--config', type=Path, help='Use a particular config.yml instead of the user defaults')
    result.add_argument('--version', action='version', version=f'gvat {__version__}')
    result.add_argument('--data_path', '--data-path', type=location, help='Flat sample folder, local or ssh://[user@]host[:port]/path')
    result.add_argument('--labels', '--classes', type=location, help='Local/SSH shared labels.txt; used to initialize missing XML')
    result.add_argument('--annotations', '--xml', type=location, help='CVAT XML file (default: discover the single XML in DATA_PATH, or create annotations.xml)')
    result.add_argument('--class-names', help='Comma-separated class names for initializing missing XML')
    result.add_argument('--mode', choices=('auto', 'annotate', 'review'), default='auto')
    result.add_argument('--media-type', choices=('images', 'video', 'all'), default=settings['media']['preferred_type'], help='Preferred source type in mixed folders (existing XML selects its matching media)')
    result.add_argument('--step', type=int, default=settings['player']['step'], help='Native player step size in frames')
    display = result.add_mutually_exclusive_group()
    display.add_argument('--pixelated', action='store_true', default=settings['player']['pixelated'], help='Disable CVAT smooth image rendering')
    display.add_argument('--smooth-images', dest='pixelated', action='store_false', help='Enable CVAT smooth image rendering')
    result.add_argument('--buffer-seconds', type=float, default=settings['buffer']['seconds'], help='Decoded video buffer target in seconds (default from config; capped by available RAM)')
    result.add_argument('--buffer-memory', type=memory_size, default=memory_size(str(settings['buffer']['max_memory'])) if settings['buffer']['max_memory'] is not None else None, help='Upper memory limit for the decoded buffer, e.g. 4GiB')
    result.add_argument('--port', type=int, default=8080, help='Loopback port (default: 8080; 0 chooses an available port)')
    result.add_argument('--no-browser', action='store_true', help='Print the session URL without opening a browser')
    result.add_argument('--yes', '-y', action='store_true', help='Create missing XML using supplied/discovered class names')
    exports = result.add_mutually_exclusive_group()
    exports.add_argument('--export-labels', type=Path, metavar='OUTPUT.zip', help='Export annotations and exit without opening the browser')
    exports.add_argument('--export-dataset', type=Path, metavar='OUTPUT.zip', help='Export annotations with media and exit')
    result.add_argument('--format', '--export-format', help='Exact CVAT export format name, e.g. "YOLO 1.1"')
    result.add_argument('--list-formats', action='store_true', help='List available CVAT image/video export formats and exit')
    result.add_argument('--overwrite', action='store_true', help='Replace an existing CLI export destination')
    return result


def help_parser():
    # These values are used only to render help. Actual launches always load
    # the shipped/user config; help remains usable even if that file is broken.
    return parser({'media': {'preferred_type': 'images'}, 'player': {'step': 10, 'pixelated': False},
                   'buffer': {'seconds': 15, 'max_memory': None}, 'export': {}})
