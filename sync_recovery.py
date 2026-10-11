"""Journal state, interrupted-sync recovery, and post-sync cleanup."""
import datetime as dt
import json
import os
import re
import shutil
from pathlib import Path

from map_core import Error, save_json, sha
from map_preparation import PreparedTileError, load_prepared

TERMINAL_JOURNAL_STATES = ('complete', 'recovered', 'legacy_acknowledged', 'orphaned_legacy', 'acknowledged_unresumable')


def sync_journals(data):
    """Read retained journals, representing unreadable entries explicitly."""
    runs = data / 'runs'
    if runs.is_symlink():
        raise Error('The sync journal directory is a symlink; device writes are blocked until it is restored.')
    if not runs.exists():
        return []
    if not runs.is_dir():
        raise Error('The sync journal path is not a directory; device writes are blocked until it is restored.')
    journals = []
    for run_dir in sorted(runs.iterdir()):
        if not run_dir.is_dir() or run_dir.is_symlink():
            continue
        journal_path = run_dir / 'journal.json'
        if not journal_path.exists() and not journal_path.is_symlink():
            continue
        try:
            if journal_path.is_symlink() or not journal_path.is_file():
                raise ValueError('journal is not a regular file')
            journal = json.loads(journal_path.read_text())
            if not isinstance(journal, dict) or not isinstance(journal.get('serial'), str) or not journal.get('serial') or not isinstance(journal.get('status'), str) or not journal.get('status'):
                raise ValueError('journal has an invalid schema')
            journals.append((journal_path, journal))
        except (OSError, ValueError, TypeError, UnicodeError):
            journals.append((journal_path, {'_corrupt_journal': True, 'status': 'unreadable'}))
    return journals


def pending_syncs(data, serial):
    pending = []
    for journal_path, journal in sync_journals(data):
        if journal.get('_corrupt_journal'):
            # The device serial cannot be trusted or read, so fail closed for
            # every device using this data directory.
            pending.append((journal_path, {**journal, 'serial': serial}))
        elif journal.get('serial') == serial and journal.get('status') not in TERMINAL_JOURNAL_STATES:
            # Any durable nonterminal state may be a hard-aborted process.
            pending.append((journal_path, journal))
    return pending


def pending_resume_info(data, pending):
    """Return one verified prepared set and any journals that cannot resume."""
    resumable = []
    unresumable = []
    damaged_prepared_files = False
    for journal_path, journal in pending:
        if journal.get('_corrupt_journal'):
            unresumable.append((journal_path, journal))
            continue
        prepared_id = journal.get('prepared_id')
        manifest_hash = journal.get('manifest_sha256')
        if (not isinstance(prepared_id, str) or not re.fullmatch(r'[a-f0-9]{32}', prepared_id)
                or not isinstance(manifest_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', manifest_hash)):
            unresumable.append((journal_path, journal))
            continue
        try:
            folder, manifest = load_prepared(data, prepared_id, manifest_hash)
        except (Error, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            if isinstance(exc, PreparedTileError):
                damaged_prepared_files = True
            unresumable.append((journal_path, journal))
            continue
        resumable.append((journal_path, journal, prepared_id, manifest_hash, manifest))

    keys = {(item[2], item[3]) for item in resumable}
    if len(keys) > 1:
        return None, unresumable + [(path, journal) for path, journal, *_ in resumable], 'More than one prepared set has an unfinished sync.'
    if any(journal.get('_corrupt_journal') for _, journal in unresumable):
        return None, unresumable + [(path, journal) for path, journal, *_ in resumable], 'One or more sync journals are unreadable or malformed; their device state cannot be determined.'
    if damaged_prepared_files:
        return None, unresumable + [(path, journal) for path, journal, *_ in resumable], 'One or more prepared tile files are missing, changed, or unsafe; this sync cannot be resumed from verified local data.'
    if not resumable:
        return None, unresumable, None

    _, _, prepared_id, manifest_hash, manifest = resumable[0]
    summary = {'countries': manifest.get('countries', []), 'files': len(manifest['files']), 'bytes': sum(int(f['size']) for f in manifest['files'])}
    return (prepared_id, manifest_hash, summary), unresumable, None

def archive_unbound_legacy_syncs(data, serial):
    """Archive pre-manifest journals automatically; they cannot be resumed."""
    archived = 0
    for journal_path, journal in pending_syncs(data, serial):
        if journal.get('_corrupt_journal'):
            continue
        if journal.get('prepared_id') is None and journal.get('manifest_sha256') is None:
            journal['previous_status'] = journal.get('status')
            journal['status'] = 'orphaned_legacy'
            journal['archived_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_json(journal_path, journal)
            archived += 1
    return archived


def resolve_unresumable_syncs(journals):
    """Keep unresolved history while explicitly clearing the retry gate."""
    resolved = []
    for journal_path, journal in journals:
        if journal.get('_corrupt_journal'):
            base = journal_path.with_name('journal.acknowledged-corrupt.json')
            destination = base
            suffix = 2
            while destination.exists() or destination.is_symlink():
                destination = base.with_name(f'journal.acknowledged-corrupt-{suffix}.json')
                suffix += 1
            os.replace(journal_path, destination)
            resolved.append(destination)
        else:
            journal['previous_status'] = journal.get('status')
            journal['status'] = 'acknowledged_unresumable'
            journal['acknowledged_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
            save_json(journal_path, journal)
            resolved.append(journal_path)
    return resolved


def mark_retried_syncs(data, serial, manifest_hash, run_id):
    for journal_path, journal in pending_syncs(data, serial):
        if journal.get('manifest_sha256') == manifest_hash:
            journal['status'] = 'recovered'
            journal['recovered_by'] = run_id
            save_json(journal_path, journal)


def cleanup_after_success(data):
    """Clean completed-run data without breaking another device's retry."""
    errors = []
    runs = data / 'runs'
    retained_prepared = set()
    retain_all_prepared = False
    journals = sync_journals(data)
    for _, journal in journals:
        if journal.get('_corrupt_journal'):
            retain_all_prepared = True
        elif journal.get('status') not in TERMINAL_JOURNAL_STATES:
            prepared_id = journal.get('prepared_id')
            if isinstance(prepared_id, str) and re.fullmatch(r'[a-f0-9]{32}', prepared_id):
                retained_prepared.add(prepared_id)
            else:
                retain_all_prepared = True

    for path in (data / 'current.json', data / 'downloads'):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
        except OSError:
            errors.append(str(path))

    prepared = data / 'prepared'
    if prepared.is_dir() and not prepared.is_symlink():
        for path in prepared.iterdir():
            try:
                if retain_all_prepared or path.name in retained_prepared:
                    continue
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path)
            except OSError:
                errors.append(str(path))
        try:
            prepared.rmdir()
        except OSError:
            pass

    if runs.is_dir() and not runs.is_symlink():
        for path in list(runs.iterdir()):
            journal_path = path / 'journal.json'
            try:
                if path.is_dir() and not path.is_symlink() and journal_path.exists():
                    entry = next((journal for source, journal in journals if source == journal_path), None)
                    if entry is None or entry.get('_corrupt_journal'):
                        continue
                    status = entry.get('status')
                    payload = path / 'payload'
                    if status == 'complete':
                        shutil.rmtree(path)
                    elif status in TERMINAL_JOURNAL_STATES and payload.is_dir() and not payload.is_symlink():
                        shutil.rmtree(payload)
            except (OSError, ValueError, TypeError, AttributeError):
                errors.append(str(path))
        try:
            runs.rmdir()
        except OSError:
            pass
    return errors
