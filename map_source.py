"""OneDrive source catalog and archive download."""
import base64
import datetime as dt
import http.cookiejar
import json
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from map_core import Error, PREFERRED_COUNTRIES, human, save_json, sha

SHARE = 'https://1drv.ms/f/s!AgQ5_SDAjeL0eIUfrjTBE8QWGZc?e=RrEqBF'
API = 'https://onedrive.live.com/_api/v2.0'
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'
MONTHS = {m.lower(): i for i, m in enumerate(('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'), 1)}


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
        ordered = [c for c in PREFERRED_COUNTRIES if c in countries] + sorted(countries - set(PREFERRED_COUNTRIES))
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


def archive_date(item):
    value = item.get('lastModifiedDateTime')
    try:
        return dt.date.fromisoformat(value[:10]).strftime('%Y-%m-%d') if value else 'date missing'
    except (TypeError, ValueError):
        return 'date missing'


def show_catalog(catalog):
    for item in catalog:
        print(f'{item["release"]}: {item["name"]} — {archive_date(item)} · {human(item["size"])}', flush=True)
    print(f'Total archive size: {human(sum(i["size"] for i in catalog))}', flush=True)
