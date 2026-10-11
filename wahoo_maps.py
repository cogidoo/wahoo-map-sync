"""Small stdlib-only downloader and cautious Wahoo tile synchronizer."""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import uuid
import zipfile

from map_core import Error, ROOTS, human, progress, save_json, sha
from map_preparation import extract, inspect_current_prepared, load_prepared, prepare_catalog, tile_path
from map_source import API, SHARE, UA, Source, archive_date, show_catalog
from sync_recovery import (
    TERMINAL_JOURNAL_STATES, archive_unbound_legacy_syncs, cleanup_after_success,
    mark_retried_syncs, pending_resume_info, pending_syncs, resolve_unresumable_syncs,
)

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
        previous_serial = self.serial
        self.serial = None
        try:
            rows = self.run('devices', '-l').splitlines()
        except Error:
            self.serial = previous_serial
            raise
        authorized = [r.split()[0] for r in rows if len(r.split()) >= 2 and r.split()[1] == 'device']
        if serial is None:
            if len(authorized) != 1:
                raise Error('Connect and authorize exactly one device. If it is not listed, hold Power and both zoom buttons while reconnecting USB.')
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


def menu_validation_key(data, pending):
    """Key cached tile validation to the local pointer or journal state."""
    def file_state(path):
        try:
            stat = path.lstat()
            return stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_mode
        except OSError:
            return None

    if pending:
        return ('pending', tuple(
            (str(path), journal.get('status'), journal.get('prepared_id'),
             journal.get('manifest_sha256'), file_state(path))
            for path, journal in pending
        ))
    return ('current', file_state(data / 'current.json'))


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
    validation_key = None
    validation_result = None
    while True:
        archived_legacy = archive_unbound_legacy_syncs(data, adb.serial)
        if archived_legacy:
            print(f'\nAutomatically archived {archived_legacy} legacy sync journal(s) without a prepared manifest. They may have left partial tiles on the device; the journal history is retained, and no device files were changed.')
        pending = pending_syncs(data, adb.serial)
        current_key = menu_validation_key(data, pending)
        if current_key != validation_key:
            candidate, unresumable, recovery_problem = pending_resume_info(data, pending)
            prepared_problem = None
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
                summary, prepared_problem = inspect_current_prepared(data) if not pending else (None, None)
            validation_result = candidate, unresumable, recovery_problem, summary, prepared_problem
            validation_key = current_key
        candidate, unresumable, recovery_problem, summary, prepared_problem = validation_result
        if prepared_problem:
            print(f'Prepared maps cannot be resumed: {prepared_problem}')
            print('Choose [1] to prepare a fresh selection; the invalid preparation will not be uploaded.')
        if pending and unresumable:
            detail = recovery_problem or 'Some older journal entries no longer have their prepared files and cannot be retried.'
            print(f'\nRecovery needed: {detail}')
            print('These runs may have left partial tiles on the device. Use [a] to keep their journal history and explicitly allow a fresh selection; this does not change device files.')
        resume = ''
        if candidate and not unresumable:
            resume = f'  [2] Resume interrupted maps ({", ".join(summary["countries"])}; {summary["files"]} files)'
        elif summary and not pending:
            resume = f'  [2] Resume prepared maps ({", ".join(summary["countries"])}; {summary["files"]} files)'
        resolve = '  [a] Resolve blocked recovery' if pending and unresumable else ''
        print(f'\nActions: [1] Write maps{resume}{resolve}  [q] Quit')
        action = input('Select: ').strip().lower()
        if action in ('q', 'quit', 'exit'):
            return
        if action == '1':
            if pending:
                if candidate and not unresumable:
                    print('An unfinished sync is tied to this prepared map set. Choose [2] to retry it first.')
                elif unresumable:
                    print('This sync cannot be resumed from local files. Choose [a] to record the unresolved state before starting a new selection.')
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
                show_catalog(selected_catalog)
                prepare_catalog(source, data, selected_catalog)
                sync(data, adb)
            except (Error, OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as e:
                print(f'Operation failed: {e}\nThe manager remains open.', file=sys.stderr, flush=True)
                if (data / 'downloads').is_dir():
                    print(f'Verified ZIPs remain in {data / "downloads"}; retries reuse them after checking their hashes.')
                pending_after = pending_syncs(data, adb.serial)
                if menu_validation_key(data, pending_after) == validation_key:
                    cached_candidate, cached_blocked, _, cached_summary, cached_problem = validation_result
                    if cached_summary and not cached_blocked:
                        print('Prepared files remain available. Choose [2] in the menu to resume.')
                    elif cached_problem:
                        print(f'Prepared files are not resumable: {cached_problem}')
            continue
        if action == '2' and ((candidate and not unresumable) or (summary and not pending)):
            try:
                print(f'Using prepared files in {data / "prepared"}; no new OneDrive download is needed.')
                sync(data, adb)
            except (Error, OSError, ValueError, KeyError, zipfile.BadZipFile, EOFError) as e:
                print(f'Resume failed: {e}\nPrepared files and the sync journal are retained.', file=sys.stderr, flush=True)
            continue
        if action == 'a' and pending and unresumable:
            if input('Acknowledge unresolved recovery, retain its history, and allow a fresh selection? [y/N] ').strip().lower() == 'y':
                resolved = resolve_unresumable_syncs(unresumable)
                print(f'Resolved {len(resolved)} blocked journal(s). Their history was retained; possible partial device tiles remain unchanged.')
            else:
                print('Recovery was not acknowledged; no changes were made.')
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
    pending = pending_syncs(data, adb.serial)
    if any(journal.get('_corrupt_journal') for _, journal in pending):
        raise Error('An unreadable sync journal blocks writes. Return to the manager and use [a] to retain and acknowledge its recovery state.')
    if any(journal.get('manifest_sha256') != manifest_hash for _, journal in pending):
        raise Error('An unfinished sync for this device belongs to a different prepared manifest; retry that prepared set before writing another selection.')
    needed = sum(f['size'] for f in changes) + (max((f['size'] for f in changes), default=0)) + 64 * 1024 ** 2
    available = free_device(adb)
    print(f'To upload: {len(changes)} files, {human(sum(f["size"] for f in changes))}\nDevice free: {human(available)}; conservative requirement: {human(needed)}', flush=True)
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
