"""Small stdlib-only downloader and cautious Wahoo tile synchronizer."""
import argparse
import base64
import contextlib
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

SHARE = 'https://1drv.ms/f/s!AgQ5_SDAjeL0eIUfrjTBE8QWGZc?e=RrEqBF'
API = 'https://onedrive.live.com/_api/v2.0'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
COUNTRIES = ('germany', 'poland', 'czech-republic')
ROOTS = {'map': '/sdcard/maps/tiles/8', 'routing': '/sdcard/maps/routing/2/000'}
MONTHS = {m.lower(): i for i, m in enumerate(('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'), 1)}


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


class Source:
    def __init__(self, api=API, share=SHARE):
        self.api, self.share = api, share
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def open(self, url, headers=None):
        req = urllib.request.Request(url, headers={'User-Agent': UA, **(headers or {})})
        try:
            return self.opener.open(req, timeout=90)
        except urllib.error.HTTPError as e:
            raise Error(f'HTTP {e.code}; source request failed (session data was not exposed).') from None
        except urllib.error.URLError:
            raise Error('Network request failed. Check your connection and try again.') from None

    def get(self, url):
        with self.open(url, {'Prefer': 'redeemSharingLinkIfNecessary'}) as response:
            return json.load(response)

    def root(self):
        with self.open(self.share) as response:
            response.read(1024)
        token = 'u!' + base64.urlsafe_b64encode(self.share.encode()).decode().rstrip('=')
        return self.get(f'{self.api}/shares/{token}/driveItem?ump=1')

    def item_url(self, item):
        drive = urllib.parse.quote(item['parentReference']['driveId'], safe='')
        ident = urllib.parse.quote(item['id'], safe='')
        return f'{self.api}/drives/{drive}/items/{ident}'

    def children(self, item):
        url = self.item_url(item) + '/children'
        result = []
        while url:
            data = self.get(url)
            result.extend(data.get('value', []))
            url = data.get('@odata.nextLink')
            if url and (urllib.parse.urlsplit(url).scheme != 'https' or urllib.parse.urlsplit(url).hostname != urllib.parse.urlsplit(self.api).hostname):
                raise Error('Unexpected API pagination target.')
        return result

    def folder(self, item, name):
        matches = [x for x in self.children(item) if x['name'] == name and 'folder' in x]
        if len(matches) != 1:
            raise Error(f'Source folder is missing or ambiguous: {name}')
        return matches[0]

    def catalog(self, countries=COUNTRIES):
        root = self.folder(self.root(), 'wahooMapsCreator-Maps')
        folders = self.children(root)
        result = []
        release_sets = {}
        prefixes = {'map': 'Maps Without Contour Lines ', 'routing': 'Routing '}
        for kind, prefix in prefixes.items():
            candidates = {}
            for folder in folders:
                match = re.fullmatch(re.escape(prefix) + r'([A-Za-z]+)-(20\d{2})', folder['name'])
                if match and match[1].lower() in MONTHS and 'folder' in folder:
                    candidates[(int(match[2]), MONTHS[match[1].lower()])] = folder
            if not candidates:
                raise Error(f'No dated source folder found for {kind}.')
            release_sets[kind] = candidates
        common = set(release_sets['map']) & set(release_sets['routing'])
        if not common:
            raise Error('No shared map and routing release was found.')
        stamp = max(common)
        map_release = release_sets['map'][stamp]
        route_release = release_sets['routing'][stamp]
        for kind in ('map', 'routing'):
            release = release_sets[kind][stamp]
            files = self.children(self.folder(release, 'europe'))
            for country in countries:
                name = country + ('-routing' if kind == 'routing' else '') + '.zip'
                matches = [f for f in files if f.get('name') == name]
                if len(matches) != 1:
                    available = sorted(f.get('name', '') for f in files if f.get('name', '').endswith('.zip'))
                    raise Error(f'Archive is missing or ambiguous: {name} in {release["name"]}/europe. ZIP entries: {json.dumps(available[:30], ensure_ascii=False)}')
                archive = matches[0]
                link = archive.get('@microsoft.graph.downloadUrl') or archive.get('@content.downloadUrl')
                if not link:
                    # Child listings need not contain transient download annotations.
                    detail = self.get(self.item_url(archive))
                    if detail.get('id') != archive['id'] or detail.get('name') != name:
                        raise Error(f'Archive details do not match the listing: {name}')
                    archive = {**archive, **detail}
                    link = archive.get('@microsoft.graph.downloadUrl') or archive.get('@content.downloadUrl')
                if 'file' not in archive or not link:
                    facet = 'yes' if 'file' in archive else 'no'
                    raise Error(f'Archive found: {name}; download link is missing after lookup or the entry is not a file (file field: {facet}).')
                parsed = urllib.parse.urlsplit(link)
                if parsed.scheme != 'https' and not (parsed.scheme == 'http' and self.api.startswith('http://127.0.0.1:') and parsed.hostname == '127.0.0.1'):
                    raise Error(f'Unsafe download protocol for {name}.')
                result.append({**archive, '@microsoft.graph.downloadUrl': link, 'kind': kind, 'country': country, 'release': map_release['name'], 'map_release': map_release['name'], 'routing_release': route_release['name']})
        return result

    def catalog_europe(self, progress_callback=None):
        """Return paired map/routing archives from one shared Europe release."""
        report = progress_callback or (lambda _message: None)
        report('Connecting to the public OneDrive share …')
        root = self.folder(self.root(), 'wahooMapsCreator-Maps')
        folders = self.children(root)
        report('Finding the latest shared map and routing release …')
        releases = {}
        for kind, prefix in [('map', 'Maps Without Contour Lines '), ('routing', 'Routing ')]:
            candidates = {}
            for folder in folders:
                match = re.fullmatch(re.escape(prefix) + r'([A-Za-z]+)-(20\d{2})', folder['name'])
                if match and match[1].lower() in MONTHS and 'folder' in folder:
                    stamp = (int(match[2]), MONTHS[match[1].lower()])
                    candidates[stamp] = folder
            releases[kind] = candidates
        common = set(releases['map']) & set(releases['routing'])
        if not common:
            raise Error('No shared map and routing release was found for Europe.')
        stamp = max(common)
        map_release, route_release = releases['map'][stamp], releases['routing'][stamp]
        report('Reading paired Europe archives and modification dates …')
        map_files = self.children(self.folder(map_release, 'europe'))
        route_files = self.children(self.folder(route_release, 'europe'))
        maps = {f['name'][:-4].lower(): f for f in map_files if re.fullmatch(r'[a-z]+(?:-[a-z]+)*\.zip', f.get('name', '')) and 'file' in f}
        routes = {f['name'][:-12].lower(): f for f in route_files if re.fullmatch(r'[a-z]+(?:-[a-z]+)*-routing\.zip', f.get('name', '')) and 'file' in f}
        countries = set(maps) & set(routes)
        if not countries:
            raise Error(f'No paired map and routing archives found in {map_release["name"]} / {route_release["name"]}.')
        preferred = ('germany', 'poland', 'czech-republic', 'denmark')
        ordered = [c for c in preferred if c in countries] + sorted(countries - set(preferred))
        result = []
        for country in ordered:
            for kind, archive in [('map', maps[country]), ('routing', routes[country])]:
                link = archive.get('@microsoft.graph.downloadUrl') or archive.get('@content.downloadUrl')
                if not link:
                    detail = self.get(self.item_url(archive))
                    if detail.get('id') != archive['id'] or detail.get('name') != archive['name']:
                        raise Error(f'Archive details do not match the listing: {archive["name"]}')
                    archive = {**archive, **detail}
                    link = archive.get('@microsoft.graph.downloadUrl') or archive.get('@content.downloadUrl')
                if not link or 'file' not in archive:
                    raise Error(f'Download link is missing or the entry is not a file: {archive["name"]}')
                parsed = urllib.parse.urlsplit(link)
                if parsed.scheme != 'https' and not (parsed.scheme == 'http' and self.api.startswith('http://127.0.0.1:') and parsed.hostname == '127.0.0.1'):
                    raise Error(f'Unsafe download protocol for {archive["name"]}.')
                result.append({**archive, '@microsoft.graph.downloadUrl': link, 'kind': kind, 'country': country, 'release': map_release['name'], 'map_release': map_release['name'], 'routing_release': route_release['name']})
        return result

    def download(self, item, directory):
        if not isinstance(item.get('size'), int) or item['size'] <= 0:
            raise Error(f'Invalid archive size: {item.get("name", "(unknown)")}')
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / item['name']
        meta = target.with_suffix('.json')
        identity = {k: item.get(k) for k in ('id', 'eTag', 'size', 'release', 'lastModifiedDateTime')}
        if target.exists() and meta.exists():
            old = json.loads(meta.read_text())
            if old.get('source') == identity and target.stat().st_size == item['size'] and sha(target) == old.get('sha256'):
                print(f'Already verified: {item["name"]}', flush=True)
                return target
        if shutil.disk_usage(directory).free < item['size'] + 64 * 1024 ** 2:
            raise Error('Not enough local storage for the download.')
        partial = target.with_suffix('.zip.part')
        print(f'Downloading {item["name"]} ({human(item["size"])}) …', flush=True)
        try:
            with self.open(item['@microsoft.graph.downloadUrl']) as response, partial.open('wb') as out:
                count = 0
                next_report = max(1, item['size'] // 40)
                for block in iter(lambda: response.read(1024 * 1024), b''):
                    count += len(block)
                    if count > item['size']:
                        raise Error('Download is larger than announced.')
                    out.write(block)
                    if count >= next_report:
                        width = 24
                        filled = min(width, int(width * count / item['size']))
                        print(f'\r  [{"#" * filled}{"-" * (width - filled)}] {100 * count / item["size"]:5.1f}%  {human(count)} / {human(item["size"])}', end='', flush=True)
                        next_report += max(1, item['size'] // 40)
                print(f'\r  [{"#" * 24}] 100.0%  {human(count)} / {human(item["size"])}', flush=True)
            if count != item['size']:
                raise Error('Download is incomplete.')
            digest = sha(partial)
            # OneDrive SHA-256, when supplied, adds an independent source check.
            expected = item.get('file', {}).get('hashes', {}).get('sha256Hash')
            if expected and digest.lower() != expected.lower():
                raise Error('Source hash does not match.')
            partial.replace(target)
            save_json(meta, {'source': identity, 'sha256': digest})
            return target
        finally:
            partial.unlink(missing_ok=True)


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


def prepare(source, data, countries):
    catalog = source.catalog(countries)
    show_catalog(catalog)
    prepared = data / 'prepared'
    prepared.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=prepared))
    entries = {}
    try:
        for item in catalog:
            archive = source.download(item, data / 'downloads')
            print(f'Verifying and extracting {item["name"]} …', flush=True)
            extract(archive, item['kind'], stage, entries, item['country'])
        manifest = {'version': 1, 'countries': list(countries), 'sources': [{k: i[k] for k in ('name', 'size', 'release', 'id')} for i in catalog], 'files': sorted(entries.values(), key=lambda f: f['path'])}
        save_json(stage / 'manifest.json', manifest)
        final = prepared / uuid.uuid4().hex
        stage.rename(final)
        save_json(data / 'current.json', {'directory': final.name})
        print(f'Prepared: {len(entries)} files, {human(sum(f["size"] for f in entries.values()))}\n{final}', flush=True)
        return final
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def prepare_catalog(source, data, catalog):
    if not catalog or len({i['release'] for i in catalog}) != 1:
        raise Error('Selection does not contain a shared map and routing release.')
    countries = list(dict.fromkeys(i['country'] for i in catalog))
    expected = {(c, k) for c in countries for k in ('map', 'routing')}
    if {(i['country'], i['kind']) for i in catalog} != expected:
        raise Error('Each country must include both a map and routing archive.')
    show_catalog(catalog)
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


def load_prepared(data):
    pointer = json.loads((data / 'current.json').read_text())['directory']
    if not re.fullmatch(r'[a-f0-9]{32}', pointer):
        raise Error('Invalid preparation pointer.')
    folder = data / 'prepared' / pointer
    manifest = json.loads((folder / 'manifest.json').read_text())
    if manifest.get('version') != 1 or not manifest.get('files'):
        raise Error('Invalid or empty manifest.')
    seen = set()
    for f in manifest['files']:
        kind = f['kind']
        if kind not in ROOTS or tile_path(f['relative'], kind) != f['relative'] or f['path'] != f'{kind}/{f["relative"]}' or f['path'] in seen:
            raise Error('Invalid or duplicate target path in manifest.')
        seen.add(f['path'])
        local = folder / f['path']
        if any(p.is_symlink() for p in (folder, local.parent, local)) or not local.is_file() or local.stat().st_size != f['size'] or sha(local) != f['sha256']:
            raise Error(f'Prepared file has changed: {f["path"]}')
    return folder, manifest


def current_prepared_summary(data):
    pointer_path = data / 'current.json'
    try:
        if pointer_path.is_symlink() or not pointer_path.is_file():
            return None
        pointer = json.loads(pointer_path.read_text()).get('directory', '')
        if not re.fullmatch(r'[a-f0-9]{32}', pointer):
            return None
        folder = data / 'prepared' / pointer
        manifest_path = folder / 'manifest.json'
        if folder.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        manifest = json.loads(manifest_path.read_text())
        files = manifest.get('files', [])
        if manifest.get('version') != 1 or not files:
            return None
        return {'countries': manifest.get('countries', []), 'files': len(files), 'bytes': sum(int(f['size']) for f in files)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def pending_syncs(data, serial):
    pending = []
    runs = data / 'runs'
    if not runs.is_dir() or runs.is_symlink():
        return pending
    for run_dir in runs.iterdir():
        journal_path = run_dir / 'journal.json'
        try:
            if run_dir.is_dir() and not run_dir.is_symlink() and journal_path.is_file() and not journal_path.is_symlink():
                journal = json.loads(journal_path.read_text())
                # Any durable nonterminal state may be a hard-aborted process.
                if journal.get('serial') == serial and journal.get('status') not in ('complete', 'recovered', 'legacy_acknowledged', 'orphaned_legacy'):
                    pending.append((journal_path, journal))
        except (OSError, ValueError, TypeError):
            continue
    return pending


def pending_resume_info(data, pending):
    """Return a verified prepared set and journals that cannot resume from it."""
    resumable = []
    unresumable = []
    for journal_path, journal in pending:
        prepared_id = journal.get('prepared_id')
        manifest_hash = journal.get('manifest_sha256')
        if not isinstance(prepared_id, str) or not re.fullmatch(r'[a-f0-9]{32}', prepared_id) or not isinstance(manifest_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', manifest_hash):
            unresumable.append((journal_path, journal))
            continue
        folder = data / 'prepared' / prepared_id
        manifest_path = folder / 'manifest.json'
        try:
            if folder.is_symlink() or not folder.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file() or sha(manifest_path) != manifest_hash:
                unresumable.append((journal_path, journal))
                continue
            manifest = json.loads(manifest_path.read_text())
            files = manifest.get('files', [])
            if manifest.get('version') != 1 or not files:
                unresumable.append((journal_path, journal))
                continue
            resumable.append((journal_path, journal, prepared_id, manifest_hash, manifest))
        except (OSError, ValueError, TypeError):
            unresumable.append((journal_path, journal))
    keys = {(item[2], item[3]) for item in resumable}
    if len(keys) > 1:
        return None, unresumable + [(path, journal) for path, journal, *_ in resumable], 'More than one prepared set has an unfinished sync.'
    if not resumable:
        return None, unresumable, None
    _, _, prepared_id, manifest_hash, manifest = resumable[0]
    summary = {'countries': manifest.get('countries', []), 'files': len(manifest['files']), 'bytes': sum(int(f['size']) for f in manifest['files'])}
    return (prepared_id, manifest_hash, summary), unresumable, None


def archive_unbound_legacy_syncs(data, serial):
    """Archive pre-manifest journals automatically; they cannot be resumed."""
    archived = 0
    for journal_path, journal in pending_syncs(data, serial):
        if journal.get('prepared_id') is None and journal.get('manifest_sha256') is None:
            journal['previous_status'] = journal.get('status')
            journal['status'] = 'orphaned_legacy'
            journal['archived_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_json(journal_path, journal)
            archived += 1
    return archived


def mark_retried_syncs(data, serial, manifest_hash, run_id):
    for journal_path, journal in pending_syncs(data, serial):
        if journal.get('manifest_sha256') == manifest_hash:
            journal['status'] = 'recovered'
            journal['recovered_by'] = run_id
            save_json(journal_path, journal)


def cleanup_after_success(data):
    """Remove temporary map data and completed run journals, retain failures."""
    errors = []
    for path in (data / 'current.json', data / 'downloads', data / 'prepared'):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            errors.append(str(path))
    runs = data / 'runs'
    if runs.is_dir() and not runs.is_symlink():
        for path in runs.iterdir():
            journal_path = path / 'journal.json'
            try:
                if path.is_dir() and not path.is_symlink() and journal_path.is_file() and not journal_path.is_symlink():
                    status = json.loads(journal_path.read_text()).get('status')
                    payload = path / 'payload'
                    if status == 'complete':
                        shutil.rmtree(path)
                    elif payload.is_dir() and not payload.is_symlink():
                        shutil.rmtree(payload)
            except (OSError, ValueError, TypeError, AttributeError):
                errors.append(str(path))
        try:
            runs.rmdir()
        except OSError:
            pass
    return errors


class ADB:
    def __init__(self, executable='adb', serial=None):
        self.executable, self.serial = executable, serial

    def run(self, *args, live=False):
        cmd = [self.executable] + (['-s', self.serial] if self.serial else []) + list(args)
        try:
            # Large map and routing directory pushes can legitimately take much
            # longer than metadata and shell commands.
            timeout = 6 * 60 * 60 if args and args[0] == 'push' else 60
            proc = subprocess.run(cmd, capture_output=not live, text=True, timeout=timeout)
        except FileNotFoundError:
            raise Error('ADB is missing. Install native macOS Platform Tools; see README.md.') from None
        except subprocess.TimeoutExpired:
            raise Error('ADB timed out; the sync journal will be retained.') from None
        if proc.returncode:
            detail = proc.stderr.strip().splitlines()[-1][:240] if proc.stderr else ''
            suffix = f' {detail}' if detail else ''
            raise Error(f'ADB command failed: {args[0]} (status {proc.returncode}).{suffix}')
        return '' if live else proc.stdout.strip()

    def shell(self, command):
        if not self.serial:
            raise Error('No device selected.')
        return self.run('shell', command)

    def select(self, serial=None):
        rows = self.run('devices', '-l').splitlines()
        authorized = [r.split()[0] for r in rows if len(r.split()) >= 2 and r.split()[1] == 'device']
        if serial is None:
            if len(authorized) != 1:
                raise Error('Connect exactly one authorized device, or use --serial. Hold Power and both zoom buttons, then connect USB.')
            serial = authorized[0]
        if serial not in authorized or not re.fullmatch(r'[A-Za-z0-9_.:-]+', serial):
            raise Error('Serial number is missing or the device is not authorized.')
        self.serial = serial
        return serial

    def inspect(self):
        props = {key: self.shell(f'getprop {key}') for key in ('ro.product.model', 'ro.product.manufacturer', 'ro.product.device', 'ro.build.display.id')}
        package = self.shell('dumpsys package com.wahoofitness.bolt')
        if not re.search(r'Package \[com\.wahoofitness\.bolt\]', package) or not re.search(r'versionName=\S+', package):
            raise Error('Could not verify the Wahoo app; sync is blocked.')
        version = re.search(r'versionName=(\S+)', package)[1]
        print(f'Device: {self.serial}\nModel: {props["ro.product.model"]}\nManufacturer: {props["ro.product.manufacturer"]}\nProduct: {props["ro.product.device"]}\nBuild: {props["ro.build.display.id"]}\nWahoo app: {version}', flush=True)
        return props

    def digest(self, remote):
        # Only generated, numeric tile paths are passed here.
        parent = remote.rsplit('/', 1)[0]
        out = self.shell(f'if [ -L {parent} ] || [ -L {remote} ]; then echo UNSAFE; elif [ -f {remote} ]; then sha256sum {remote}; elif [ -e {remote} ]; then echo UNSAFE; else echo MISSING; fi')
        if out == 'MISSING':
            return None
        if not re.fullmatch(r'[a-fA-F0-9]{64}\s+\*?' + re.escape(remote), out):
            raise Error('Device hash is unavailable or the target path is unsafe; sync aborted.')
        return out.split()[0].lower()

    def ensure_directory(self, remote):
        """Create a generated remote directory one component at a time."""
        parts = PurePosixPath(remote).parts
        if not parts or parts[0] != '/' or any(not re.fullmatch(r'[A-Za-z0-9_.-]+', p) for p in parts[1:]):
            raise Error('Unsafe device directory path.')
        current = ''
        for part in parts[1:]:
            current += '/' + part
            if current == '/sdcard':  # Common Android-managed symlink.
                continue
            result = self.shell(f'if [ -L {current} ]; then echo UNSAFE; elif [ -e {current} ] && [ ! -d {current} ]; then echo UNSAFE; else mkdir -p {current} && echo OK; fi')
            if result != 'OK':
                raise Error('Unsafe device target directory.')

    def verify_files(self, files, remote_for):
        """Verify remote files in bounded shell commands."""
        for offset in range(0, len(files), 80):
            group = files[offset:offset + 80]
            paths = [remote_for(f) for f in group]
            checks = ' '.join(f'if [ -L {path} ] || [ ! -f {path} ]; then echo UNSAFE; exit 41; fi;' for path in paths)
            out = self.shell(checks + ' sha256sum ' + ' '.join(paths))
            found = {}
            for line in out.splitlines():
                match = re.fullmatch(r'([a-fA-F0-9]{64})\s+\*?(.+)', line)
                if not match:
                    raise Error('Device hash is unavailable; sync aborted.')
                found[match[2]] = match[1].lower()
            if any(found.get(path) != f['sha256'] for f, path in zip(group, paths)):
                raise Error('Device file hash did not match; sync aborted.')

    def promote_files(self, files, staged_for, target_for, on_batch=None):
        """Atomically move staged files into place after validating all parents."""
        promoted = []
        for offset in range(0, len(files), 20):
            commands = []
            for f in files[offset:offset + 20]:
                target = target_for(f)
                parent = target.rsplit('/', 1)[0]
                components = PurePosixPath(parent).parts
                checks = []
                current = '/sdcard'
                for part in components[2:]:  # Skip / and the allowed /sdcard symlink.
                    current += '/' + part
                    checks.append(f'[ ! -L {current} ]')
                checks.append(f'[ ! -L {target} ]')
                checks.append(f'{{ [ ! -e {target} ] || [ -f {target} ]; }}')
                commands.append(
                    f'if ! ( ' + ' && '.join(checks) + f' ); then exit 41; fi; '
                    f'mkdir -p {parent} && mv {staged_for(f)} {target} || exit 42'
                )
            try:
                self.shell('; '.join(commands))
                promoted.extend(files[offset:offset + 20])
                if on_batch:
                    on_batch(promoted)
            except Error as error:
                raise Error('Could not safely promote staged map files; the previous tile set may be partly updated.') from error

    def remove_stage(self, remote):
        if not re.fullmatch(r'/sdcard/maps/(?:tiles|routing/2)/\.wahoo-sync-[0-9-]+-[a-f0-9]{8}', remote):
            raise Error('Refusing to remove an unexpected staging path.')
        self.shell(f'rm -rf {remote}')


def free_device(adb):
    output = adb.shell('df -k /sdcard')
    for line in reversed(output.splitlines()):
        parts = line.split()
        if len(parts) >= 4 and all(p.isdigit() for p in parts[1:4]):
            return int(parts[3]) * 1024
    raise Error('Could not determine free device storage.')



def ordered_countries(catalog):
    countries = {item['country'] for item in catalog}
    preferred = ('germany', 'poland', 'czech-republic', 'denmark')
    return [c for c in preferred if c in countries] + sorted(countries - set(preferred))


def default_countries(countries):
    return {'germany'} if 'germany' in countries else ({countries[0]} if countries else set())


def archive_date(item):
    value = item.get('lastModifiedDateTime')
    try:
        return dt.date.fromisoformat(value[:10]).strftime('%Y-%m-%d') if value else 'date missing'
    except (TypeError, ValueError):
        return 'date missing'


def choose_countries(catalog):
    """Arrow-key country picker; Space toggles and Enter confirms."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Error('Country selection requires an interactive terminal.')
    import termios
    import tty
    countries = ordered_countries(catalog)
    totals = {c: sum(i['size'] for i in catalog if i['country'] == c) for c in countries}
    selected = default_countries(countries)
    cursor = 0
    fd = sys.stdin.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            height = shutil.get_terminal_size((80, 24)).lines
            page_size = max(1, height - 6)
            start = max(0, min(cursor - page_size // 2, len(countries) - page_size))
            end = min(len(countries), start + page_size)
            sys.stdout.write('\x1b[2J\x1b[H')
            map_release = catalog[0].get('map_release', catalog[0]['release']).replace('Maps Without Contour Lines ', '')
            routing_release = catalog[0].get('routing_release', catalog[0]['release']).replace('Routing ', '')
            sys.stdout.write(f'Europe · Maps {map_release} / Routing {routing_release}\r\n')
            sys.stdout.write('↑/↓ move · Space toggle · Enter confirm · q cancel\r\n')
            sys.stdout.write('Same release folder; ZIP dates: M=map, R=routing.\r\n')
            if start:
                sys.stdout.write('  … more countries above\r\n')
            for index in range(start, end):
                country = countries[index]
                mark = 'x' if country in selected else ' '
                label = country.replace('-', ' ').title()
                pointer = '>' if index == cursor else ' '
                map_item = next(i for i in catalog if i['country'] == country and i['kind'] == 'map')
                route_item = next(i for i in catalog if i['country'] == country and i['kind'] == 'routing')
                dates = f'M {archive_date(map_item)} / R {archive_date(route_item)}'
                sys.stdout.write(f'{pointer} [{mark}] {label} · {dates} · {human(totals[country])}\r\n')
            if end < len(countries):
                sys.stdout.write('  … more countries below\r\n')
            sys.stdout.flush()
            key = sys.stdin.read(1)
            if key == '\x1b':
                sequence = sys.stdin.read(2)
                if sequence == '[A':
                    cursor = (cursor - 1) % len(countries)
                elif sequence == '[B':
                    cursor = (cursor + 1) % len(countries)
            elif key == ' ':
                country = countries[cursor]
                if country in selected:
                    selected.remove(country)
                else:
                    selected.add(country)
            elif key in ('\r', '\n'):
                if selected:
                    return [c for c in countries if c in selected]
            elif key.lower() == 'q':
                return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        sys.stdout.write('\x1b[0m\r\n')
        sys.stdout.flush()


def interactive(adb, data):
    print('Connect your ELEMNT BOLT v2: power it on and connect it to your Mac with a USB data cable.')
    print('If ADB reports “unauthorized”: unplug USB, hold Power and both right zoom buttons, reconnect USB, then approve the device prompt.')
    while True:
        try:
            adb.select()
            props = adb.inspect()
            break
        except Error as e:
            print(str(e))
            if input('Retry device connection/authorization? [Enter/q] ').strip().lower() == 'q':
                return
    model = props.get('ro.product.model', '(unknown)')
    build = props.get('ro.build.display.id', props.get('ro.build.id', '(unknown)'))
    print(f'Connected: {model} · Build {build} · Serial {adb.serial}')
    while True:
        archived_legacy = archive_unbound_legacy_syncs(data, adb.serial)
        if archived_legacy:
            print(f'\nAutomatically archived {archived_legacy} legacy sync journal(s) without a prepared manifest. They may have left partial tiles on the device; the journal history is retained, and no device files were changed.')
        pending = pending_syncs(data, adb.serial)
        candidate, unresumable, recovery_problem = pending_resume_info(data, pending)
        if candidate:
            prepared_id, _, summary = candidate
            pointer_path = data / 'current.json'
            try:
                current_id = json.loads(pointer_path.read_text()).get('directory') if pointer_path.is_file() and not pointer_path.is_symlink() else None
            except (OSError, ValueError, TypeError):
                current_id = None
            if current_id != prepared_id:
                save_json(pointer_path, {'directory': prepared_id})
        else:
            summary = current_prepared_summary(data) if not pending else None
        if pending and unresumable:
            detail = recovery_problem or 'Some older journal entries no longer have their prepared files and cannot be retried.'
            print(f'\nRecovery needed: {detail}')
            print('Those runs may have left partial tiles on the device. Acknowledge them only if you want to keep that state and allow a fresh map selection.')
        resume = ''
        if candidate and not unresumable:
            resume = f'  [2] Resume interrupted maps ({", ".join(summary["countries"])}; {summary["files"]} files)'
        elif summary and not pending:
            resume = f'  [2] Resume prepared maps ({", ".join(summary["countries"])}; {summary["files"]} files)'
        print(f'\nActions: [1] Write maps{resume}  [q] Quit')
        action = input('Select: ').strip().lower()
        if action in ('q', 'quit', 'exit'):
            return
        if action == '1':
            if pending:
                if candidate and not unresumable:
                    print('An unfinished sync is tied to this prepared map set. Choose [2] to retry it first.')
                elif unresumable:
                    print('This sync cannot be resumed from local files. Its journal remains pending; restore its prepared files or resolve the journal before starting a new selection.')
                continue
            try:
                source = Source()
                print('Reading European countries and map/routing releases from OneDrive …', flush=True)
                catalog = source.catalog_europe(lambda message: print(message, flush=True))
                selected = choose_countries(catalog)
                if selected is None:
                    print('Cancelled; no archives downloaded.')
                    continue
                selected_catalog = [item for item in catalog if item['country'] in selected]
                total = sum(item['size'] for item in selected_catalog)
                labels = ', '.join(c.replace('-', ' ').title() for c in selected)
                print(f'Selection confirmed: {labels}\nMaps: {catalog[0]["map_release"]} · Routing: {catalog[0]["routing_release"]}\nArchives: {len(selected_catalog)} (map + routing per country) · Download: {human(total)}', flush=True)
                prepare_catalog(source, data, selected_catalog)
                sync(data, adb)
            except (Error, OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as e:
                print(f'Operation failed: {e}\nThe manager remains open.', file=sys.stderr, flush=True)
                if (data / 'downloads').is_dir():
                    print(f'Verified ZIPs remain in {data / "downloads"}; retries reuse them after checking their hashes.')
                if current_prepared_summary(data):
                    print('Prepared files remain available. Choose [2] in the menu to resume.')
            continue
        if action == '2' and ((candidate and not unresumable) or (summary and not pending)):
            try:
                print(f'Using prepared files in {data / "prepared"}; no new OneDrive download is needed.')
                sync(data, adb)
            except (Error, OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as e:
                print(f'Resume failed: {e}\nPrepared files and the sync journal are retained.', file=sys.stderr, flush=True)
            continue
        print('Invalid selection.')


def sync(data, adb, confirm=None):
    folder, manifest = load_prepared(data)
    props = adb.inspect()
    model = re.sub(r'[^A-Z0-9]', '', props.get('ro.product.model', '').upper())
    device = re.sub(r'[^A-Z0-9]', '', props.get('ro.product.device', '').upper())
    if any(token in model or token in device for token in ('BOLTV1', 'BOLT1', 'BOLTV3', 'BOLT3', 'ROAM', 'ACE')):
        raise Error('Reported model is not a BOLT v2; sync is blocked.')
    if not sys.stdin.isatty() and confirm is None:
        raise Error('Sync requires an interactive terminal.')
    ask = confirm or input
    known_bolt2 = model == 'ELEMNTBOLT2' or (device == 'BOLT2P0' and 'wahoo' in props.get('ro.product.manufacturer', '').lower())
    if not known_bolt2:
        print('The reported properties do not uniquely identify a BOLT v2.', flush=True)
        if ask('Confirm this is a Wahoo ELEMNT BOLT v2? [y/N] ').strip().lower() != 'y':
            raise Error('BOLT v2 identity was not confirmed; nothing was written.')
    print(f'Device: {props.get("ro.product.model", "(unknown)")} · Build {props.get("ro.build.display.id", props.get("ro.build.id", "(unknown)"))} · Serial {adb.serial}\nCountries: {", ".join(c.replace("-", " ").title() for c in manifest["countries"])}\nPrepared: {len(manifest["files"])} files, {human(sum(f["size"] for f in manifest["files"]))}', flush=True)
    # Ensure every ancestor is a real directory, never a symlink.
    for root in ROOTS.values():
        parts = PurePosixPath(root).parts
        # /sdcard itself is an Android-managed symlink on many devices.
        for i in range(3, len(parts) + 1):
            path = str(PurePosixPath(*parts[:i]))
            if adb.shell(f'if [ -L {path} ]; then echo UNSAFE; elif [ -e {path} ] && [ ! -d {path} ]; then echo UNSAFE; else echo OK; fi') != 'OK':
                raise Error('Unsafe device target directory.')
    changes = [{**f, 'remote': ROOTS[f['kind']] + '/' + f['relative']} for f in manifest['files']]
    manifest_hash = sha(folder / 'manifest.json')
    if any(journal.get('manifest_sha256') != manifest_hash for _, journal in pending_syncs(data, adb.serial)):
        raise Error('An unfinished sync for this device belongs to a different prepared manifest; retry that prepared set before writing another selection.')
    needed = sum(f['size'] for f in changes) + (max((f['size'] for f in changes), default=0)) + 64 * 1024 ** 2
    available = free_device(adb)
    print(f'To upload: {len(changes)} files, {human(sum(f["size"] for f in changes))}\nDevice free: {human(available)}; conservative requirement: {human(needed)}', flush=True)
    pending = pending_syncs(data, adb.serial)
    stale_stages = [path for _, old in pending for path in old.get('stage_parents', [])]
    if available < needed and not stale_stages:
        raise Error('Not enough free device storage.')
    if available < needed:
        print('Temporary data from an unfinished sync can be reclaimed after confirmation.', flush=True)
    if ask('Write maps now? [y/N] ').strip().lower() != 'y':
        raise Error('Cancelled; nothing was written.')
    run_id = dt.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    run_dir = data / 'runs' / run_id
    run_dir.mkdir(parents=True)
    stage_parent = {kind: ROOTS[kind].rsplit('/', 1)[0] + '/.wahoo-sync-' + run_id for kind in ROOTS}
    stage_root = {kind: stage_parent[kind] + '/' + ('8' if kind == 'map' else '000') for kind in ROOTS}
    journal = {'serial': adb.serial, 'properties': props, 'prepared_id': folder.name,
               'manifest_sha256': manifest_hash, 'status': 'staging',
               'stage_parents': list(stage_parent.values()),
               'files': [f['path'] for f in changes], 'completed': []}
    journal_path = run_dir / 'journal.json'
    save_json(journal_path, journal)
    print(f'Sync journal: {journal_path}', flush=True)
    try:
        # Reclaim abandoned device staging from earlier failed runs on this device.
        if stale_stages:
            print('Removing temporary data from previous unfinished syncs…', flush=True)
        for old_stage in stale_stages:
            adb.remove_stage(old_stage)
        available = free_device(adb)
        if stale_stages:
            print(f'Device free after temporary-data cleanup: {human(available)}.', flush=True)
        if available < needed:
            raise Error('Not enough free device storage after reclaiming failed-run temporary data.')
        # Validate the complete local selection before beginning either device write.
        for f in changes:
            local = folder / f['path']
            if sha(local) != f['sha256']:
                raise Error('Local tile changed after preparation.')
        payload = run_dir / 'payload'
        grouped = {'map': payload / '8', 'routing': payload / '000'}
        for kind, target in grouped.items():
            target.mkdir(parents=True)
            for f in changes:
                if f['kind'] != kind:
                    continue
                source = folder / f['path']
                destination = target / f['relative']
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, destination)
        for kind in ROOTS:
            adb.ensure_directory(stage_parent[kind])
            adb.ensure_directory(ROOTS[kind])
        journal['stage_roots'] = stage_root
        save_json(journal_path, journal)
        print('Uploading map tiles to temporary device storage (1/2)…', flush=True)
        adb.run('push', str(grouped['map']), stage_parent['map'], live=True)
        print('Map staging upload finished.', flush=True)
        print('Uploading routing tiles to temporary device storage (2/2)…', flush=True)
        adb.run('push', str(grouped['routing']), stage_parent['routing'], live=True)
        print('Routing staging upload finished.', flush=True)
        journal['status'] = 'staged_verification'
        save_json(journal_path, journal)
        print('Verifying temporary device files…', flush=True)
        adb.verify_files(changes, lambda f: stage_root[f['kind']] + '/' + f['relative'])
        journal['status'] = 'promotion'
        save_json(journal_path, journal)
        print('Replacing map and routing tiles…', flush=True)
        def record_promoted(files):
            journal['completed'] = [f['path'] for f in files]
            save_json(journal_path, journal)
            progress('Replacing verified tiles', len(files), len(changes))
        adb.promote_files(changes, lambda f: stage_root[f['kind']] + '/' + f['relative'], lambda f: f['remote'], record_promoted)
        journal['status'] = 'final_verification'
        save_json(journal_path, journal)
        print('Verifying final device files…', flush=True)
        adb.verify_files(changes, lambda f: f['remote'])
        for kind in ROOTS:
            adb.remove_stage(stage_parent[kind])
        journal['status'] = 'cache'
        save_json(journal_path, journal)
        for action in ('PURGE', 'RELOAD_MAP'):
            print(f'{action}: refreshing device map cache…', flush=True)
            output = adb.shell(f'am broadcast -a com.wahoofitness.bolt.service.BMapManager.{action}')
            if 'Broadcast completed: result=0' not in output or re.search(r'exception|error|denial', output, re.I):
                raise Error(f'Cache command {action} was not confirmed as successful.')
            print(f'{action}: complete.', flush=True)
        journal['status'] = 'complete'
        save_json(journal_path, journal)
        mark_retried_syncs(data, adb.serial, manifest_hash, run_id)
        print('Sync fully verified; cache commands confirmed successful.')
        cleanup_errors = cleanup_after_success(data)
        if cleanup_errors:
            print('Sync succeeded; local cleanup was incomplete: ' + ', '.join(cleanup_errors), file=sys.stderr)
        else:
            print('Local ZIPs and extracted maps were deleted. Incomplete and recovered sync journals are retained.')
    except BaseException:
        journal['status'] = 'failed'
        save_json(journal_path, journal)
        print(f'Sync incomplete. No rollback was attempted; sync journal retained: {journal_path}', file=sys.stderr)
        raise


def show_catalog(catalog):
    for item in catalog:
        print(f'{item["release"]}: {item["name"]} — {archive_date(item)} · {human(item["size"])}', flush=True)
    print(f'Total archive size: {human(sum(i["size"] for i in catalog))}', flush=True)


def native_adb(executable):
    resolved = shutil.which(executable)
    if not resolved:
        raise Error('ADB is missing. Install native macOS Platform Tools; see README.md.')
    if sys.platform == 'darwin':
        check = subprocess.run(['/usr/bin/lipo', '-verify_arch', 'arm64', resolved], capture_output=True)
        if check.returncode:
            raise Error('ADB is not a native arm64 build; Rosetta is not used.')
    return resolved


@contextlib.contextmanager
def lock(data):
    import fcntl
    with (data / '.lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error('Another CLI run is using this data directory.') from None
        yield


class EnglishParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, 'Error: Invalid arguments. Available options: --help\n')


def main(argv=None):
    p = EnglishParser(add_help=False, usage='%(prog)s [options]', description='Safely manage community maps and routing for ELEMNT BOLT v2.')
    p._optionals.title = 'Options'
    p.add_argument('-h', '--help', action='help', help='Show this help and exit')
    p.add_argument('--data-dir', type=Path, default=Path.home() / 'Library/Application Support/wahoo-map-sync', help='External data directory')
    p.add_argument('--adb', default='adb', help='Path to native ADB')
    args = p.parse_args(argv)
    try:
        data = args.data_dir.expanduser().resolve()
        repo = Path(__file__).resolve().parent
        if data == repo or repo in data.parents:
            raise Error('Runtime data must be outside the project directory.')
        data.mkdir(parents=True, exist_ok=True)
        with lock(data):
            adb = ADB(native_adb(args.adb))
            interactive(adb, data)
        return 0
    except (Error, OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Cancelled. Any sync journal was retained.', file=sys.stderr)
        return 130
