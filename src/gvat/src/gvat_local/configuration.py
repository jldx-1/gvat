"""Validated user defaults, shared by the launcher and native CVAT workspace."""

import copy
import os
from pathlib import Path

import yaml

from .buffering import memory_size
from .workspace import WorkspaceError


BUNDLED_CONFIG = Path(__file__).resolve().parents[4] / 'config.yml'


def config_path():
    override = os.environ.get('GVAT_CONFIG')
    return Path(override).expanduser() if override else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'gvat/config.yml'


def load_config(path=None):
    try:
        defaults = yaml.safe_load(BUNDLED_CONFIG.read_text())
        selected = Path(path).expanduser() if path else config_path()
        if path or os.environ.get('GVAT_CONFIG') or selected.is_file():
            custom = yaml.safe_load(selected.read_text()) or {}
            if not isinstance(custom, dict):
                raise ValueError('Expected a YAML mapping')
            for section, values in custom.items():
                if section not in defaults or not isinstance(values, dict):
                    raise ValueError(f'Unknown or invalid config section: {section}')
                for key, value in values.items():
                    if key not in defaults[section]:
                        raise ValueError(f'Unknown config setting: {section}.{key}')
                    defaults[section][key] = value
        player, buffering = defaults['player'], defaults['buffer']
        if defaults['media']['preferred_type'] not in ('images', 'video', 'all'):
            raise ValueError('media.preferred_type must be images, video or all')
        if type(player['step']) is not int or not 1 <= player['step'] <= 10000:
            raise ValueError('player.step must be an integer between 1 and 10000')
        if type(player['pixelated']) is not bool:
            raise ValueError('player.pixelated must be true or false')
        if type(buffering['seconds']) not in (int, float) or not 0 < buffering['seconds'] <= 3600:
            raise ValueError('buffer.seconds must be greater than 0 and at most 3600')
        if buffering['max_memory'] is not None:
            memory_size(str(buffering['max_memory']))
        for key, value in defaults['export'].items():
            if value != 'auto':
                from .exports import catalog
                if value not in catalog():
                    raise ValueError(f'Unknown export format in export.{key}. Run gvat --list-formats for exact names.')
        return copy.deepcopy(defaults)
    except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
        raise WorkspaceError(f'Invalid GVAT config: {error}') from error
