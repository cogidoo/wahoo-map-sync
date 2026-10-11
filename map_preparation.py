"""Archive validation and immutable local map preparations."""
import json
import re
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from map_core import Error, ROOTS, human, progress, save_json, sha


class PreparedTileError(Error):
    """A manifested tile is missing, unsafe, or no longer matches its hash."""


def tile_path(name, kind):
    """Accept only documented numeric tile layouts; preserve provider basenames."""
    if '\\' in name or '\x00' in name:
        raise Error(f'Unsafe archive path: {name!r}')
    parts = name.rstrip('/').split('/')
    if any(p in ('', '.', '..') for p in parts) or PurePosixPath(name).is_absolute():
        raise Error(f'Unsafe archive path: {name!r}')
    # Archives may include the full device hierarchy or just numeric tile paths.
    prefixes = {'map': ('maps/tiles/8/', 'tiles/8/'), 'routing': ('maps/routing/2/000/', 'routing/2/000/', '2/000/', '000/')}
    for prefix in prefixes[kind]:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    suffix = r'\.map\.lzma(?:\.17)?' if kind == 'map' else r'\.gph\.lzma'
    match = re.fullmatch(r'(\d{1,3})/(\d{1,3})' + suffix, name)
    if not match:
        # Some archives name tiles x.y.map.lzma instead of using x/y paths.
        match = re.fullmatch(r'(?:(\d{1,3})/)?(\d{1,3})\.(\d{1,3})' + suffix, name)
        if not match or (match[1] is not None and match[1] != match[2]):
            raise Error(f'Unknown tile name: {name!r}')
        x, y = match[2], match[3]
        relative = f'{x}/{name.rsplit("/", 1)[-1]}'
    else:
        x, y = match[1], match[2]
        relative = name
    if kind == 'map' and (int(x) > 255 or int(y) > 255):
        raise Error('Map coordinate is outside zoom level 8.')
    if kind == 'routing' and (len(x) != 3 or len(y) != 3):
        raise Error('Routing coordinates must have three digits.')
    return relative


def extract(archive, kind, dest, entries, country='unknown'):
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        files = []
        for info in infos:
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise Error('Archive contains links or special files.')
            # Validate directories too, without trying to interpret them as tiles.
            raw = info.filename.rstrip('/')
            if '\\' in raw or '\x00' in raw or any(p in ('', '.', '..') for p in raw.split('/')) or raw.startswith('/'):
                raise Error('Unsafe archive path.')
            if not info.is_dir():
                # The provided country bundles contain zero-byte numbered
                # version sidecars beside the canonical tile. Ignore only this
                # observed empty sidecar form; never accept an empty tile.
                if info.file_size == 0 and re.search(r'\.(?:map|gph)\.lzma\.\d+$', info.filename):
                    continue
                relative = tile_path(info.filename, kind)
                if info.file_size <= 0:
                    raise Error('Empty tile file.')
                files.append((info, relative))
        if not files:
            raise Error('Empty archive.')
        expanded = sum(i.file_size for i, _ in files)
        if expanded > 40 * 1024 ** 3 or shutil.disk_usage(dest).free < expanded + 64 * 1024 ** 2:
            raise Error('Archive is too large or there is not enough storage to extract it.')
        extract_step = max(1, len(files) // 40)
        for index, (info, relative) in enumerate(files, 1):
            key = f'{kind}/{relative}'
            target = dest / key
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(target.name + '.part')
            try:
                with z.open(info) as src, temp.open('wb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                digest = sha(temp)
                if key in entries and entries[key]['sha256'] != digest:
                    raise Error(f'Conflicting overlapping tile: {key}')
                temp.replace(target)
                if key in entries:
                    entries[key]['countries'] = sorted(set(entries[key].get('countries', []) + [country]))
                else:
                    entries[key] = {'path': key, 'kind': kind, 'relative': relative, 'size': info.file_size, 'sha256': digest, 'countries': [country]}
            finally:
                temp.unlink(missing_ok=True)
            if index % extract_step == 0 or index == len(files):
                progress(f'Extracting {Path(archive).name}', index, len(files))


def prepare_catalog(source, data, catalog):
    if not catalog or len({i['release'] for i in catalog}) != 1:
        raise Error('Selection does not contain a shared map and routing release.')
    countries = list(dict.fromkeys(i['country'] for i in catalog))
    expected = {(c, k) for c in countries for k in ('map', 'routing')}
    if {(i['country'], i['kind']) for i in catalog} != expected:
        raise Error('Each country must include both a map and routing archive.')
    prepared = data / 'prepared'
    prepared.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    stage = prepared / ('.prepare-' + run_id)
    final = prepared / run_id
    stage.mkdir()
    downloads = data / 'downloads'
    print(f'ZIP downloads: {downloads}\nTemporary extraction and verification: {stage}\nAfter verification: {final}', flush=True)
    entries = {}
    try:
        for item in catalog:
            archive = source.download(item, downloads)
            print(f'Verifying and extracting {item["name"]} …', flush=True)
            extract(archive, item['kind'], stage, entries, item['country'])
        manifest = {'version': 1, 'countries': countries, 'sources': [{k: i.get(k) for k in ('name', 'size', 'release', 'map_release', 'routing_release', 'lastModifiedDateTime', 'id')} for i in catalog], 'files': sorted(entries.values(), key=lambda f: f['path'])}
        save_json(stage / 'manifest.json', manifest)
        stage.rename(final)
        save_json(data / 'current.json', {'directory': final.name})
        total = sum(f['size'] for f in entries.values())
        print(f'Prepared: {len(entries)} files, {human(total)}\n{final}', flush=True)
        return final
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def load_prepared(data, prepared_id=None, expected_manifest_hash=None):
    """Load one complete, verified preparation through a single trust boundary."""
    data = Path(data)
    if prepared_id is None:
        pointer_path = data / 'current.json'
        if pointer_path.is_symlink() or not pointer_path.is_file():
            raise Error('The preparation pointer is missing or is not a regular file.')
        try:
            prepared_id = json.loads(pointer_path.read_text()).get('directory', '')
        except (ValueError, TypeError, AttributeError) as exc:
            raise Error('The preparation pointer is invalid.') from exc
    if not isinstance(prepared_id, str) or not re.fullmatch(r'[a-f0-9]{32}', prepared_id):
        raise Error('Invalid preparation pointer.')

    prepared_root = data / 'prepared'
    if prepared_root.is_symlink() or not prepared_root.is_dir():
        raise Error('Prepared maps directory is missing or unsafe.')
    folder = prepared_root / prepared_id
    if folder.is_symlink() or not folder.is_dir():
        raise Error('Prepared map set is missing or unsafe.')
    manifest_path = folder / 'manifest.json'
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise Error('Prepared manifest is missing or is not a regular file.')
    if expected_manifest_hash is not None and sha(manifest_path) != expected_manifest_hash:
        raise Error('Prepared manifest does not match the interrupted sync.')

    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise Error('Prepared manifest is invalid JSON.') from exc
    if not isinstance(manifest, dict) or manifest.get('version') != 1 or not isinstance(manifest.get('files'), list) or not manifest['files']:
        raise Error('Invalid or empty manifest.')
    validate_prepared_files(folder, manifest)
    return folder, manifest

def validate_prepared_files(folder, manifest):
    """Reject missing, changed, or unsafe tiles in a prepared set."""
    seen = set()
    for f in manifest['files']:
        if not isinstance(f, dict):
            raise Error('Invalid tile entry in prepared manifest.')
        kind, relative, path = f.get('kind'), f.get('relative'), f.get('path')
        if (kind not in ROOTS or not isinstance(relative, str) or tile_path(relative, kind) != relative
                or path != f'{kind}/{relative}' or path in seen
                or not isinstance(f.get('size'), int) or f['size'] <= 0
                or not isinstance(f.get('sha256'), str) or not re.fullmatch(r'[a-f0-9]{64}', f['sha256'])):
            raise Error('Invalid or duplicate target path in manifest.')
        seen.add(path)
        local = folder / path
        parents = list(local.parents)
        if (folder.is_symlink() or not local.is_file() or local.is_symlink()
                or any(parent.is_symlink() for parent in parents if parent == folder or folder in parent.parents)
                or local.stat().st_size != f['size'] or sha(local) != f['sha256']):
            raise PreparedTileError(f'Prepared file has changed: {path}')


def inspect_current_prepared(data):
    """Return the verified current preparation and any reason it cannot resume."""
    pointer_path = Path(data) / 'current.json'
    if not pointer_path.exists() and not pointer_path.is_symlink():
        return None, None
    try:
        _, manifest = load_prepared(data)
        return {
            'countries': manifest.get('countries', []),
            'files': len(manifest['files']),
            'bytes': sum(int(f['size']) for f in manifest['files']),
        }, None
    except (Error, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return None, str(exc)
