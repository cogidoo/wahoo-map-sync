"""Shared primitives for Wahoo map synchronization."""
import hashlib
import json
from pathlib import Path

ROOTS = {'map': '/sdcard/maps/tiles/8', 'routing': '/sdcard/maps/routing/2/000'}
PREFERRED_COUNTRIES = ('germany', 'poland', 'czech-republic', 'denmark')


class Error(Exception):
    pass


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def save_json(path, data):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')
    tmp.replace(path)


def human(size):
    return f'{size / 1024 ** 3:.2f} GiB'


def progress(label, current, total):
    width = 24
    ratio = current / total if total else 1
    filled = min(width, int(width * ratio))
    print(f'\r{label}: [{"#" * filled}{"-" * (width - filled)}] {current}/{total} · {100 * ratio:.0f}%', end='\n' if current >= total else '', flush=True)
