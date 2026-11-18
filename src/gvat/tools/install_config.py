"""Create the user's default config once; never replace an existing preference file."""

import os
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1])
configured = os.environ.get('GVAT_CONFIG')
destination = Path(configured).expanduser() if configured else Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'gvat/config.yml'
destination.parent.mkdir(parents=True, exist_ok=True)
try:
    with destination.open('xb') as target, source.open('rb') as template:
        shutil.copyfileobj(template, target)
    print('Created default GVAT config.')
except FileExistsError:
    print('Preserved existing GVAT config.')
