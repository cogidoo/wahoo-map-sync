import contextlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import wahoo_maps as w


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def archive(self, names):
        path = self.root / 'test.zip'
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in names:
                z.writestr(name, data)
        return path

    def test_map_routing_and_identical_overlap(self):
        entries = {}
        archive = self.archive([('tiles/8/133/84.map.lzma.17', b'map')])
        w.extract(archive, 'map', self.root, entries)
        w.extract(archive, 'map', self.root, entries)
        archive = self.archive([('000/123/456.gph.lzma', b'route')])
        w.extract(archive, 'routing', self.root, entries)
        self.assertEqual(set(entries), {'map/133/84.map.lzma.17', 'routing/123/456.gph.lzma'})

    def test_conflicting_overlap(self):
        entries = {}
        w.extract(self.archive([('133/84.map.lzma', b'one')]), 'map', self.root, entries)
        with self.assertRaises(w.Error):
            w.extract(self.archive([('133/84.map.lzma', b'two')]), 'map', self.root, entries)

    def test_invalid_archives(self):
        for names in ([], [('../escape', b'x')], [('/absolute', b'x')], [('133\\84.map.lzma', b'x')], [('256/84.map.lzma', b'x')], [('133/84.map.lzma', b'')], [('routing-config.json', b'x')], [('x/../', b'')]):
            with self.subTest(names=names), self.assertRaises(w.Error):
                w.extract(self.archive(names), 'map', self.root, {})

    def test_symlink(self):
        item = zipfile.ZipInfo('133/84.map.lzma')
        item.create_system = 3
        item.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(w.Error):
            w.extract(self.archive([(item, b'/etc/passwd')]), 'map', self.root, {})

    def test_flat_coordinate_filename(self):
        self.assertEqual(w.tile_path('133.84.map.lzma', 'map'), '133/133.84.map.lzma')
        with self.assertRaises(w.Error):
            w.tile_path('132/133.84.map.lzma', 'map')


class FakeDeviceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.device = self.root / 'device'
        self.device.mkdir()
        self.folder = self.data / 'prepared' / ('a' * 32)
        self.folder.mkdir(parents=True)
        entries = []
        self.originals = {}
        tiles = [
            ('map', '133/84.map.lzma'),
            ('map', '133/85.map.lzma'),
            ('map', '133/86.map.lzma'),
            ('map', '133/87.map.lzma'),
            ('routing', '123/456.gph.lzma'),
            ('routing', '123/457.gph.lzma'),
        ]
        for kind, rel in tiles:
            path = f'{kind}/{rel}'
            f = self.folder / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b'new-' + kind.encode())
            entries.append({'path': path, 'relative': rel, 'kind': kind, 'sha256': w.sha(f), 'size': f.stat().st_size})
            remote = self.device / (w.ROOTS[kind] + '/' + rel).lstrip('/')
            remote.parent.mkdir(parents=True, exist_ok=True)
            remote.write_bytes(b'old-' + kind.encode())
            self.originals[remote] = remote.read_bytes()
        w.save_json(self.folder / 'manifest.json', {'version': 1, 'countries': ['germany', 'poland', 'czech-republic'], 'files': entries})
        w.save_json(self.data / 'current.json', {'directory': 'a' * 32})
        self.env = patch.dict(os.environ, {'FAKE_ADB_ROOT': str(self.device), 'FAKE_ADB_FAIL': ''})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.adb = w.ADB(str(Path(__file__).with_name('fake_adb.py')))
        self.adb.select()

    def run_sync(self, answers=('y',), keep_artifacts=False):
        iterator = iter(answers)
        self.prompts = []
        def answer(prompt):
            self.prompts.append(prompt)
            return next(iterator)
        cleanup = patch.object(w, 'cleanup_after_success', return_value=[]) if keep_artifacts else contextlib.nullcontext()
        stdout, stderr = io.StringIO(), io.StringIO()
        with cleanup, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            w.sync(self.data, self.adb, answer)
        self.output = stdout.getvalue()
        self.error_output = stderr.getvalue()

    def calls(self):
        return [json.loads(line) for line in (self.device / 'calls.jsonl').read_text().splitlines()]

    def journals(self):
        return [json.loads(p.read_text()) for p in self.data.glob('runs/*/journal.json')]

    def assert_originals(self):
        for path, old in self.originals.items():
            self.assertEqual(path.read_bytes(), old)

    def test_confirmed_sync_stages_hashes_and_promotes_two_directory_pushes(self):
        self.run_sync(keep_artifacts=True)
        self.assertEqual(self.prompts, ['Write maps now? [y/N] '])
        calls = self.calls()
        self.assertTrue(all(c[:2] == ['-s', 'TEST123'] for c in calls[1:]))
        pushes = [c for c in calls if len(c) > 2 and c[2] == 'push']
        self.assertEqual(len(pushes), 2)
        self.assertTrue(all(Path(c[3]).is_dir() for c in pushes))
        self.assertTrue(any(c[3].endswith('/payload/8') and '/.wahoo-sync-' in c[4] and c[4].startswith('/sdcard/maps/tiles/') for c in pushes))
        self.assertTrue(any(c[3].endswith('/payload/000') and '/.wahoo-sync-' in c[4] and c[4].startswith('/sdcard/maps/routing/2/') for c in pushes))
        self.assertTrue(any(c[2] == 'shell' and 'sha256sum ' in c[3] for c in calls if len(c) > 3))
        self.assertFalse(any(c[2] == 'pull' for c in calls if len(c) > 2))
        self.assertEqual(self.journals()[0]['status'], 'complete')
        for stage in self.journals()[0]['stage_parents']:
            self.assertFalse((self.device / stage.lstrip('/')).exists())
        self.assertFalse((self.data / 'backups').exists())
        self.assertEqual(len(pushes), 2)
        self.assertNotEqual(next(iter(self.originals)).read_bytes(), b'old-map')
        broadcasts = [c[-1] for c in calls if 'am broadcast' in c[-1]]
        self.assertTrue(broadcasts[0].endswith('.PURGE'))
        self.assertTrue(broadcasts[1].endswith('.RELOAD_MAP'))
        for message in ('Map staging upload finished.', 'Routing staging upload finished.', 'Verifying final device files…', 'PURGE: complete.', 'RELOAD_MAP: complete.'):
            self.assertIn(message, self.output)

    def test_failed_push_retains_journal_and_skips_cache_reload(self):
        for failure in ('upload',):
            with self.subTest(failure=failure), patch.dict(os.environ, {'FAKE_ADB_FAIL': failure}):
                with self.assertRaises(w.Error):
                    self.run_sync()
                self.assertEqual(self.journals()[-1]['completed'], [])
                self.assertEqual(self.journals()[-1]['status'], 'failed')
                self.assertTrue(any(c[-1].startswith('am broadcast') for c in self.calls()) is False)

    def test_routing_push_failure_does_not_promote_map_tiles(self):
        with patch.dict(os.environ, {'FAKE_ADB_FAIL': 'routing-upload'}), self.assertRaises(w.Error):
            self.run_sync()
        self.assert_originals()
        self.assertEqual(self.journals()[-1]['completed'], [])
        self.assertFalse(any('am broadcast' in c[-1] for c in self.calls()))

    def test_promotion_failure_preserves_adb_status_and_existing_tiles(self):
        with (
            patch.dict(os.environ, {'FAKE_ADB_FAIL': 'rename'}),
            self.assertRaisesRegex(w.Error, r'ADB command failed: shell \(status 42\)'),
        ):
            self.run_sync()
        self.assert_originals()
        self.assertEqual(self.journals()[-1]['completed'], [])
        self.assertFalse(any('am broadcast' in c[-1] for c in self.calls()))

    def test_partial_promotion_failure_keeps_journal_and_retry_repairs_every_tile(self):
        manifest = w.load_prepared(self.data)[1]
        with patch.dict(os.environ, {'FAKE_ADB_FAIL': 'rename-after-3'}), self.assertRaises(w.Error):
            self.run_sync()
        journal = self.journals()[0]
        self.assertEqual(journal['status'], 'failed')
        self.assertEqual(journal['completed'], [])
        for index, entry in enumerate(manifest['files']):
            target = self.device / (w.ROOTS[entry['kind']] + '/' + entry['relative']).lstrip('/')
            expected = (self.folder / entry['path']).read_bytes() if index < 3 else self.originals[target]
            self.assertEqual(target.read_bytes(), expected)
        self.assertFalse(any('am broadcast' in c[-1] for c in self.calls()))

        self.run_sync()

        self.assertEqual(self.journals()[0]['status'], 'recovered')
        for entry in manifest['files']:
            target = self.device / (w.ROOTS[entry['kind']] + '/' + entry['relative']).lstrip('/')
            self.assertEqual(w.sha(target), entry['sha256'])

    def test_hard_killed_process_during_promotion_is_recovered_on_retry(self):
        repo = Path(__file__).resolve().parents[1]
        fake_adb = str(Path(__file__).with_name('fake_adb.py'))
        script = (
            'import os, pathlib, sys; os.environ["FAKE_ADB_PARENT_PID"] = str(os.getpid()); import wahoo_maps as w; '
            'w.sync(pathlib.Path(sys.argv[1]), w.ADB(sys.argv[2], serial="TEST123"), confirm=lambda _: "y")'
        )
        env = os.environ.copy()
        env.update({
            'PYTHONPATH': str(repo),
            'FAKE_ADB_ROOT': str(self.device),
            'FAKE_ADB_FAIL': 'kill-during-promotion',
        })
        process = subprocess.Popen(
            [sys.executable, '-c', script, str(self.data), fake_adb],
            cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        process.communicate(timeout=30)
        self.assertEqual(process.returncode, -signal.SIGKILL)
        journal = self.journals()[0]
        self.assertEqual(journal['status'], 'promotion')
        manifest = w.load_prepared(self.data)[1]
        for index, entry in enumerate(manifest['files']):
            target = self.device / (w.ROOTS[entry['kind']] + '/' + entry['relative']).lstrip('/')
            expected = (self.folder / entry['path']).read_bytes() if index < 3 else self.originals[target]
            self.assertEqual(target.read_bytes(), expected)

        self.run_sync()

        self.assertEqual(self.journals()[0]['status'], 'recovered')
        for entry in manifest['files']:
            target = self.device / (w.ROOTS[entry['kind']] + '/' + entry['relative']).lstrip('/')
            self.assertEqual(w.sha(target), entry['sha256'])

    def test_missing_and_wrong_final_hashes_keep_journal_and_retry(self):
        manifest = w.load_prepared(self.data)[1]
        for failure in ('hash-missing-final', 'hash-wrong-final'):
            (self.device / 'calls.jsonl').write_text('')
            with self.subTest(failure=failure), patch.dict(os.environ, {'FAKE_ADB_FAIL': failure}), self.assertRaises(w.Error):
                self.run_sync()
            self.assertTrue(any(journal['status'] == 'failed' for journal in self.journals()))
            self.assertFalse(any('am broadcast' in c[-1] for c in self.calls()))
            self.run_sync(keep_artifacts=True)
            self.assertTrue(any(journal['status'] == 'recovered' for journal in self.journals()))
            for entry in manifest['files']:
                target = self.device / (w.ROOTS[entry['kind']] + '/' + entry['relative']).lstrip('/')
                self.assertEqual(w.sha(target), entry['sha256'])

    def test_sync_preserves_unselected_tiles_themes_and_routing_config(self):
        controls = {
            self.device / 'sdcard/maps/tiles/8/200/10.map.lzma': b'unselected map tile',
            self.device / 'sdcard/maps/routing/2/000/900/10.gph.lzma': b'unselected routing tile',
            self.device / 'sdcard/maps/themes/custom.theme': b'user theme',
            self.device / 'sdcard/maps/routing/2/routing-config.json': b'{"keep": true}',
        }
        for path, value in controls.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)

        self.run_sync()

        for path, value in controls.items():
            self.assertEqual(path.read_bytes(), value)

    def test_successful_device_a_sync_preserves_device_b_recovery_data(self):
        device_b_id = 'b' * 32
        device_b_folder = self.data / 'prepared' / device_b_id
        shutil.copytree(self.folder, device_b_folder)
        manifest = json.loads((device_b_folder / 'manifest.json').read_text())
        manifest['countries'] = ['denmark']
        w.save_json(device_b_folder / 'manifest.json', manifest)
        manifest_hash = w.sha(device_b_folder / 'manifest.json')
        run_b = self.data / 'runs' / 'device-b-interrupted'
        run_b.mkdir(parents=True)
        w.save_json(run_b / 'journal.json', {
            'serial': 'DEVICE_B', 'status': 'promotion', 'prepared_id': device_b_id,
            'manifest_sha256': manifest_hash, 'stage_parents': [], 'files': [], 'completed': [],
        })
        payload_marker = run_b / 'payload' / 'marker'
        payload_marker.parent.mkdir()
        payload_marker.write_bytes(b'recovery payload')

        self.run_sync()

        self.assertTrue((device_b_folder / 'manifest.json').is_file())
        self.assertTrue(payload_marker.is_file())
        self.assertEqual(json.loads((run_b / 'journal.json').read_text())['status'], 'promotion')
        self.assertFalse(self.folder.exists())

    def test_missing_final_roots_are_created_before_staging_and_preserve_layout(self):
        for kind in w.ROOTS:
            remote_root = self.device / w.ROOTS[kind].lstrip('/')
            shutil.rmtree(remote_root)
        self.run_sync(keep_artifacts=True)
        for remote in self.originals:
            self.assertTrue(remote.is_file())
            self.assertEqual(w.sha(remote), next(f['sha256'] for f in w.load_prepared(self.data)[1]['files'] if w.ROOTS[f['kind']] + '/' + f['relative'] == '/' + str(remote.relative_to(self.device))))

    def test_symlink_in_tile_parent_blocks_promotion(self):
        outside = self.root / 'outside'
        outside.mkdir()
        protected = outside / '84.map.lzma'
        protected.write_bytes(b'keep')
        map_parent = self.device / (w.ROOTS['map'] + '/133').lstrip('/')
        shutil.rmtree(map_parent)
        map_parent.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(
            w.Error, r'ADB command failed: shell \(status 41\)'
        ):
            self.run_sync()
        self.assertEqual(protected.read_bytes(), b'keep')
        self.assertFalse(any('am broadcast' in c[-1] for c in self.calls()))

    def test_unrecognized_device_requires_explicit_bolt2_confirmation(self):
        with patch.dict(os.environ, {'FAKE_ADB_MODEL': 'ELEMNT BOLT', 'FAKE_ADB_DEVICE': 'generic'}), self.assertRaises(w.Error):
            self.run_sync(('n',))
        self.assertEqual(self.prompts, ['Confirm this is a Wahoo ELEMNT BOLT v2? [y/N] '])
        self.assertFalse(self.journals())
        self.assert_originals()

    def test_known_incompatible_model_is_blocked(self):
        for model in ('ELEMNT_BOLT1', 'ELEMNT_BOLT3'):
            with self.subTest(model=model), patch.dict(os.environ, {'FAKE_ADB_MODEL': model, 'FAKE_ADB_DEVICE': 'unknown'}), self.assertRaises(w.Error):
                self.run_sync()
            self.assertFalse(self.journals())
            self.assert_originals()

    def test_decline_is_read_only(self):
        with self.assertRaises(w.Error):
            self.run_sync(('n',))
        self.assert_originals()
        self.assertFalse(self.journals())
        self.assertFalse(any(c[2] in ('push', 'pull') or c[-1].startswith(('mv ', 'mkdir ', 'am broadcast')) for c in self.calls()[1:]))

    def test_insufficient_space_and_missing_package(self):
        for failure in ('space', 'package'):
            with self.subTest(failure=failure), patch.dict(os.environ, {'FAKE_ADB_FAIL': failure}), self.assertRaises(w.Error):
                self.run_sync()
        self.assert_originals()
        self.assertFalse(self.journals())

    def test_exact_upload_size_plus_safety_margin_is_enough_device_space(self):
        manifest = w.load_prepared(self.data)[1]
        upload_size = sum(item['size'] for item in manifest['files'])
        available = upload_size + 64 * 1024 ** 2
        with patch.object(w, 'free_device', return_value=available):
            self.run_sync(keep_artifacts=True)
        self.assertIn('requirement including 64 MiB safety margin', self.output)

    def test_corrupt_prepared_prevents_writes(self):
        (self.folder / 'map/133/84.map.lzma').write_bytes(b'tampered')
        with self.assertRaises(w.Error):
            self.run_sync()
        self.assertEqual(len(self.calls()), 1)
        self.assert_originals()

    def test_cache_failure_keeps_journal(self):
        with patch.dict(os.environ, {'FAKE_ADB_FAIL': 'cache'}), self.assertRaises(w.Error):
            self.run_sync()
        self.assertEqual(self.journals()[0]['status'], 'failed')
        self.assertEqual(len(self.journals()[0]['completed']), 6)

    def test_each_cache_broadcast_failure_is_reported_and_stops_progression(self):
        for failure, expected in (('cache-purge', 1), ('cache-reload_map', 2)):
            (self.device / 'calls.jsonl').write_text('')
            with self.subTest(failure=failure), patch.dict(os.environ, {'FAKE_ADB_FAIL': failure}), self.assertRaises(w.Error):
                self.run_sync()
            broadcasts = [c[-1] for c in self.calls() if 'am broadcast' in c[-1]]
            self.assertEqual(len(broadcasts), expected)
            self.assertEqual(self.journals()[-1]['status'], 'failed')

    def test_adb_push_timeout_exceeds_short_command_timeout(self):
        with patch.object(w.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ''
            run.return_value.stderr = ''
            self.adb.run('push', 'source', '/sdcard/maps', live=True)
            self.assertEqual(run.call_args.kwargs['timeout'], 6 * 60 * 60)
            self.adb.run('shell', 'true')
            self.assertEqual(run.call_args.kwargs['timeout'], 60)

    def test_repeated_update_pushes_all_selected_tiles_without_pulling_them(self):
        self.run_sync(keep_artifacts=True)
        (self.device / 'calls.jsonl').write_text('')
        self.run_sync(keep_artifacts=True)
        calls = self.calls()
        self.assertEqual(sum(c[2] == 'push' for c in calls if len(c) > 2), 2)
        self.assertEqual(sum(c[2] == 'pull' for c in calls if len(c) > 2), 0)
        self.assertTrue(any(c[2] == 'shell' and 'sha256sum ' in c[3] for c in calls if len(c) > 3))

    def test_successful_sync_cleans_generated_maps_and_completed_journal(self):
        (self.data / 'downloads').mkdir()
        (self.data / 'downloads' / 'germany.zip').write_bytes(b'archive')
        self.run_sync()
        self.assertFalse((self.data / 'current.json').exists())
        self.assertFalse((self.data / 'prepared').exists())
        self.assertFalse((self.data / 'downloads').exists())
        self.assertFalse((self.data / 'runs').exists())

    def test_successful_retry_removes_old_failed_payload_but_keeps_journal(self):
        with patch.dict(os.environ, {'FAKE_ADB_FAIL': 'routing-upload'}), self.assertRaises(w.Error):
            self.run_sync()
        failed_payload = next((self.data / 'runs').glob('*/payload'))
        self.assertTrue(failed_payload.exists())
        old_stages = self.journals()[0]['stage_parents']
        with patch.dict(os.environ, {'FAKE_ADB_FAIL': 'space-reclaimable'}):
            self.run_sync()
        journals = self.journals()
        self.assertEqual(len(journals), 1)
        self.assertEqual(journals[0]['status'], 'recovered')
        self.assertFalse(next((self.data / 'runs').glob('*/payload'), None))
        for stage in old_stages:
            self.assertFalse((self.device / stage.lstrip('/')).exists())

    def test_failed_journal_rejects_a_different_prepared_manifest(self):
        with patch.dict(os.environ, {'FAKE_ADB_FAIL': 'upload'}), self.assertRaises(w.Error):
            self.run_sync()
        journal_path = next((self.data / 'runs').glob('*/journal.json'))
        journal = json.loads(journal_path.read_text())
        journal['status'] = 'promotion'  # Simulate a process killed between durable phases.
        w.save_json(journal_path, journal)
        manifest_path = self.folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['sources'] = [{'name': 'different selection'}]
        w.save_json(manifest_path, manifest)
        with self.assertRaises(w.Error):
            self.run_sync()
        self.assertEqual(self.prompts, [])
        self.assertEqual(len(self.journals()), 1)

    def test_interrupted_journal_states_bind_manifest_and_reclaim_staging(self):
        manifest_hash = w.sha(self.folder / 'manifest.json')
        statuses = ('staging', 'promotion', 'final_verification', 'cache')
        old_stages = []
        for index, status in enumerate(statuses):
            run_dir = self.data / 'runs' / f'interrupted-{index}'
            run_dir.mkdir(parents=True)
            stage_parents = []
            for kind in w.ROOTS:
                base = w.ROOTS[kind].rsplit('/', 1)[0]
                stage = f'{base}/.wahoo-sync-20990101-12000{index}-deadbeef'
                (self.device / stage.lstrip('/')).mkdir(parents=True)
                (self.device / stage.lstrip('/') / 'partial-upload').write_bytes(b'partial')
                stage_parents.append(stage)
                old_stages.append(stage)
            w.save_json(run_dir / 'journal.json', {
                'serial': 'TEST123', 'status': status, 'prepared_id': self.folder.name,
                'manifest_sha256': manifest_hash, 'stage_parents': stage_parents,
                'files': [], 'completed': [],
            })

        self.run_sync()

        journals = self.journals()
        recovered = [journal for journal in journals if journal['status'] == 'recovered']
        self.assertEqual(len(recovered), len(statuses))
        for journal in recovered:
            self.assertEqual(journal['prepared_id'], self.folder.name)
            self.assertEqual(journal['manifest_sha256'], manifest_hash)
        for stage in old_stages:
            self.assertFalse((self.device / stage.lstrip('/')).exists())

    def test_interrupted_prepared_set_is_restored_to_resume_menu(self):
        manifest_hash = w.sha(self.folder / 'manifest.json')
        self.folder_id = self.folder.name
        run_dir = self.data / 'runs' / 'hard-aborted'
        run_dir.mkdir(parents=True)
        w.save_json(run_dir / 'journal.json', {
            'serial': 'TEST123', 'status': 'promotion', 'prepared_id': self.folder_id,
            'manifest_sha256': manifest_hash, 'stage_parents': [], 'files': [], 'completed': [],
        })
        (self.data / 'current.json').unlink()
        self.adb.serial = None
        output = io.StringIO()
        with patch('builtins.input', side_effect=['q']), contextlib.redirect_stdout(output):
            w.interactive(self.adb, self.data)
        pointer = self.data / 'current.json'
        self.assertTrue(pointer.is_file(), output.getvalue())
        self.assertEqual(json.loads(pointer.read_text())['directory'], self.folder_id)
        self.assertIn('[2] Resume interrupted maps', output.getvalue())

    def test_unbound_legacy_journal_is_archived_automatically(self):
        run_dir = self.data / 'runs' / 'legacy-failure'
        run_dir.mkdir(parents=True)
        w.save_json(run_dir / 'journal.json', {'serial': 'TEST123', 'status': 'failed', 'files': ['map/133/84.map.lzma']})
        self.adb.serial = None
        output = io.StringIO()
        with patch('builtins.input', side_effect=['q']), contextlib.redirect_stdout(output):
            w.interactive(self.adb, self.data)
        result = json.loads((run_dir / 'journal.json').read_text())
        self.assertEqual(result['status'], 'orphaned_legacy')
        self.assertEqual(result['previous_status'], 'failed')
        self.assertNotIn('[a] Acknowledge legacy sync', output.getvalue())
        self.assertIn('Automatically archived 1 legacy sync journal', output.getvalue())
        self.assertIn('may have left partial tiles', output.getvalue())
        self.assertIn('no device files were changed', output.getvalue())
        self.assertFalse(w.pending_syncs(self.data, 'TEST123'))

    def test_manifest_bound_journal_without_prepared_files_stays_blocked(self):
        run_dir = self.data / 'runs' / 'missing-prepared'
        run_dir.mkdir(parents=True)
        w.save_json(run_dir / 'journal.json', {
            'serial': 'TEST123', 'status': 'failed', 'prepared_id': 'a' * 32,
            'manifest_sha256': 'b' * 64, 'files': ['map/133/84.map.lzma'],
        })
        self.adb.serial = None
        output = io.StringIO()
        with patch('builtins.input', side_effect=['1', 'q']), contextlib.redirect_stdout(output):
            w.interactive(self.adb, self.data)
        result = json.loads((run_dir / 'journal.json').read_text())
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Recovery needed:', output.getvalue())
        self.assertIn('[a] Resolve blocked recovery', output.getvalue())
        self.assertTrue(w.pending_syncs(self.data, 'TEST123'))

        recovery_output = io.StringIO()
        with patch('builtins.input', side_effect=['a', 'y', 'q']), contextlib.redirect_stdout(recovery_output):
            w.interactive(self.adb, self.data)
        result = json.loads((run_dir / 'journal.json').read_text())
        self.assertIn('Resolved 1 blocked journal', recovery_output.getvalue())
        self.assertEqual(result['status'], 'acknowledged_unresumable')
        self.assertFalse(w.pending_syncs(self.data, 'TEST123'))

    def test_missing_or_damaged_prepared_tile_offers_blocked_recovery(self):
        manifest_hash = w.sha(self.folder / 'manifest.json')
        tile = self.folder / 'map/133/84.map.lzma'
        original = tile.read_bytes()
        for index, damage in enumerate(('missing', 'changed')):
            tile.write_bytes(original)
            run_dir = self.data / 'runs' / f'damaged-prepared-{index}'
            run_dir.mkdir(parents=True)
            w.save_json(run_dir / 'journal.json', {
                'serial': 'TEST123', 'status': 'promotion', 'prepared_id': self.folder.name,
                'manifest_sha256': manifest_hash, 'files': [], 'completed': [],
            })
            if damage == 'missing':
                tile.unlink()
            else:
                tile.write_bytes(b'changed tile')

            output = io.StringIO()
            with patch('builtins.input', side_effect=['1', 'q']), contextlib.redirect_stdout(output):
                w.interactive(self.adb, self.data)
            self.assertIn('prepared tile files are missing, changed, or unsafe', output.getvalue())
            self.assertIn('[a] Resolve blocked recovery', output.getvalue())
            self.assertFalse(any(call[2] == 'push' for call in self.calls() if len(call) > 2))

            with patch('builtins.input', side_effect=['a', 'y', 'q']), contextlib.redirect_stdout(io.StringIO()):
                w.interactive(self.adb, self.data)
            result = json.loads((run_dir / 'journal.json').read_text())
            self.assertEqual(result['status'], 'acknowledged_unresumable')

    def test_corrupt_journals_block_writes_until_explicitly_acknowledged(self):
        invalid_journals = {}
        for name, content in (('bad-json', b'{invalid'), ('bad-schema', b'{"serial":"TEST123"}')):
            run_dir = self.data / 'runs' / name
            run_dir.mkdir(parents=True)
            journal_path = run_dir / 'journal.json'
            journal_path.write_bytes(content)
            invalid_journals[journal_path] = content
        self.adb.serial = None
        output = io.StringIO()
        with patch('builtins.input', side_effect=['1', 'q']), contextlib.redirect_stdout(output):
            w.interactive(self.adb, self.data)
        self.assertIn('unreadable or malformed', output.getvalue())
        self.assertIn('[a] Resolve blocked recovery', output.getvalue())
        self.assertEqual(len(w.pending_syncs(self.data, 'TEST123')), 2)
        self.assertFalse(any(call[2] == 'push' for call in self.calls() if len(call) > 2))

        recovery_output = io.StringIO()
        with patch('builtins.input', side_effect=['a', 'y', 'q']), contextlib.redirect_stdout(recovery_output):
            w.interactive(self.adb, self.data)

        self.assertIn('Resolved 2 blocked journal', recovery_output.getvalue())
        for old_path, content in invalid_journals.items():
            preserved = list(old_path.parent.glob('journal.acknowledged-corrupt*.json'))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].read_bytes(), content)
        self.assertFalse(w.pending_syncs(self.data, 'TEST123'))


class CatalogTests(unittest.TestCase):
    def catalog(self, annotation, missing=False):
        source = w.Source()
        folder = {'id': 'europe'}
        releases = [{'id': 'map', 'name': 'Maps Without Contour Lines January-2099', 'folder': {}}, {'id': 'routing', 'name': 'Routing January-2099', 'folder': {}}]
        archives = [{'id': 'archive', 'name': 'germany.zip', 'file': {}, 'size': 100, 'parentReference': {'driveId': 'D'}}]
        routing = [{**archives[0], 'id': 'route-archive', 'name': 'germany-routing.zip'}]
        details = {'archive': {**archives[0], **annotation}, 'route-archive': {**routing[0], **annotation}}
        with patch.object(source, 'root', return_value={}), patch.object(source, 'folder', return_value=folder), patch.object(source, 'children', side_effect=[releases, [] if missing else archives, routing]), patch.object(source, 'get', side_effect=lambda url: details[url.rsplit('/', 1)[-1]]) as get:
            result = source.catalog_europe()
            return result, get

    def test_missing_listing_link_fetches_detail(self):
        result, get = self.catalog({'@microsoft.graph.downloadUrl': 'https://fixture/secret'})
        self.assertEqual(len(result), 2)
        self.assertEqual(get.call_args_list[0].args[0], w.API + '/drives/D/items/archive')
        self.assertEqual(get.call_count, 2)

    def test_europe_catalog_pairs_one_release_and_uses_preferred_order(self):
        source = w.Source()
        root = {'id': 'root'}
        release = [
            {'id': 'm', 'name': 'Maps Without Contour Lines January-2099', 'folder': {}},
            {'id': 'r', 'name': 'Routing January-2099', 'folder': {}},
            {'id': 'oldm', 'name': 'Maps Without Contour Lines March-2099', 'folder': {}},
        ]
        def archive(name, ident):
            return {'id': ident, 'name': name, 'file': {}, 'size': 100, 'parentReference': {'driveId': 'D'}, '@microsoft.graph.downloadUrl': 'https://fixture/' + ident}
        maps = [archive('austria.zip', 'am'), archive('germany.zip', 'gm'), archive('denmark.zip', 'dm')]
        routes = [archive('austria-routing.zip', 'ar'), archive('germany-routing.zip', 'gr'), archive('denmark-routing.zip', 'dr')]
        with patch.object(source, 'root', return_value=root), patch.object(source, 'folder', side_effect=lambda item, name: {'id': name, 'name': name, 'folder': {}}), patch.object(source, 'children', side_effect=[release, maps, routes]):
            result = source.catalog_europe()
        self.assertEqual([item['country'] for item in result], ['germany', 'germany', 'denmark', 'denmark', 'austria', 'austria'])
        self.assertEqual({item['release'] for item in result}, {'Maps Without Contour Lines January-2099'})
        self.assertEqual([(item['kind'], item['name']) for item in result[:2]], [('map', 'germany.zip'), ('routing', 'germany-routing.zip')])

    def test_europe_catalog_refuses_different_map_and_routing_stamps(self):
        source = w.Source()
        releases = [{'id': 'm', 'name': 'Maps Without Contour Lines January-2099', 'folder': {}}, {'id': 'r', 'name': 'Routing February-2099', 'folder': {}}]
        with patch.object(source, 'root', return_value={}), patch.object(source, 'folder', return_value={}), patch.object(source, 'children', return_value=releases), self.assertRaises(w.Error):
            source.catalog_europe()

    def test_personal_content_annotation(self):
        result, get = self.catalog({'@content.downloadUrl': 'https://fixture/secret'})
        self.assertEqual(result[0]['@microsoft.graph.downloadUrl'], 'https://fixture/secret')
        self.assertEqual(get.call_count, 2)

    def test_errors_distinguish_missing_archive_and_link(self):
        with self.assertRaisesRegex(w.Error, 'No paired map and routing archives'):
            self.catalog({}, missing=True)
        with self.assertRaisesRegex(w.Error, 'Download link is missing or the entry is not a file'):
            self.catalog({'webUrl': 'https://fixture/secret'})

    def test_europe_catalog_requires_common_map_and_routing_release(self):
        source = w.Source()
        releases = [{'id': 'm', 'name': 'Maps Without Contour Lines January-2099', 'folder': {}}, {'id': 'r', 'name': 'Routing February-2099', 'folder': {}}]
        with patch.object(source, 'root', return_value={}), patch.object(source, 'folder', return_value={}), patch.object(source, 'children', return_value=releases), self.assertRaisesRegex(w.Error, 'shared map and routing release'):
            source.catalog_europe()

    def test_archive_dates_are_shown_separately(self):
        self.assertEqual(w.archive_date({'lastModifiedDateTime': '2099-01-09T13:12:28Z'}), '2099-01-09')
        self.assertEqual(w.archive_date({}), 'date missing')

    def test_selected_preparation_reports_download_and_extraction_paths(self):
        source = w.Source()
        with tempfile.TemporaryDirectory() as source_temp, tempfile.TemporaryDirectory() as temp:
            source_dir = Path(source_temp)
            catalog = []
            archives = []
            for kind, name, member in [('map', 'germany.zip', '133/84.map.lzma'), ('routing', 'germany-routing.zip', '123/456.gph.lzma')]:
                archive = source_dir / name
                with zipfile.ZipFile(archive, 'w') as z:
                    z.writestr(member, b'tile-' + kind.encode())
                archives.append(archive)
                catalog.append({'country': 'germany', 'kind': kind, 'name': name, 'id': kind, 'size': 100, 'release': 'Maps Without Contour Lines January-2099', 'map_release': 'Maps Without Contour Lines January-2099', 'routing_release': 'Routing January-2099'})
            with contextlib.redirect_stdout(io.StringIO()) as out, patch.object(source, 'download', side_effect=archives):
                data = Path(temp)
                folder = w.prepare_catalog(source, data, catalog)
                text = out.getvalue()
                self.assertIn(str(data / 'downloads'), text)
                self.assertIn(str(data / 'prepared' / '.prepare-'), text)
                self.assertIn(str(folder), text)
                self.assertTrue((data / 'current.json').is_file())


class PreparedLoadingTests(unittest.TestCase):
    def test_all_prepared_load_paths_reject_symlink_and_non_file_manifests(self):
        for kind in ('symlink', 'directory'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                data = Path(temp)
                prepared_id = 'c' * 32
                folder = data / 'prepared' / prepared_id
                tile = folder / 'map/133/84.map.lzma'
                tile.parent.mkdir(parents=True)
                tile.write_bytes(b'valid tile')
                entry = {'path': 'map/133/84.map.lzma', 'kind': 'map', 'relative': '133/84.map.lzma',
                         'size': tile.stat().st_size, 'sha256': w.sha(tile), 'countries': ['germany']}
                target = data / 'manifest-target.json'
                w.save_json(target, {'version': 1, 'countries': ['germany'], 'files': [entry]})
                expected_hash = w.sha(target)
                manifest = folder / 'manifest.json'
                if kind == 'symlink':
                    manifest.symlink_to(target)
                else:
                    manifest.mkdir()
                w.save_json(data / 'current.json', {'directory': prepared_id})

                with self.assertRaises(w.Error):
                    w.load_prepared(data)
                with self.assertRaises(w.Error):
                    w.load_prepared(data, prepared_id, expected_hash)
                summary, problem = w.inspect_current_prepared(data)
                self.assertIsNone(summary)
                self.assertTrue(problem)

                pending = [(data / 'runs' / 'run' / 'journal.json', {
                    'prepared_id': prepared_id, 'manifest_sha256': expected_hash,
                })]
                candidate, unresumable, _ = w.pending_resume_info(data, pending)
                self.assertIsNone(candidate)
                self.assertEqual(len(unresumable), 1)

                adb = w.ADB('unused')
                with patch.object(adb, 'inspect') as inspect, self.assertRaises(w.Error):
                    w.sync(data, adb)
                inspect.assert_not_called()



class ManagerTests(unittest.TestCase):
    def test_cleanup_preserves_failed_sync_journal(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            runs = data / 'runs'
            failed = runs / 'failed-run'
            complete = runs / 'complete-run'
            failed.mkdir(parents=True)
            complete.mkdir()
            w.save_json(failed / 'journal.json', {'serial': 'TEST', 'status': 'failed'})
            w.save_json(complete / 'journal.json', {'serial': 'TEST', 'status': 'complete'})
            self.assertEqual(w.cleanup_after_success(data), [])
            self.assertTrue((failed / 'journal.json').is_file())
            self.assertFalse(complete.exists())

    def test_recoverable_write_error_keeps_manager_open(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            data = Path(temp)
            adb = w.ADB('fake')
            with patch.object(adb, 'select'), patch.object(adb, 'inspect', return_value={'ro.product.model': 'ELEMNT-BOLT2', 'ro.build.display.id': 'fixture'}), patch.object(w.Source, 'catalog_europe', return_value=[{'country': 'germany', 'kind': 'map', 'name': 'germany.zip', 'size': 1, 'release': 'January-2099', 'map_release': 'Maps Without Contour Lines January-2099', 'routing_release': 'Routing January-2099'}, {'country': 'germany', 'kind': 'routing', 'name': 'germany-routing.zip', 'size': 1, 'release': 'January-2099', 'map_release': 'Maps Without Contour Lines January-2099', 'routing_release': 'Routing January-2099'}]), patch.object(w, 'choose_countries', return_value=['germany']), patch.object(w, 'prepare_catalog', side_effect=w.Error('fixture failure')), patch('builtins.input', side_effect=['1', 'q']):
                w.interactive(adb, data)
            self.assertIn('The manager remains open', err.getvalue())

    def test_menu_reuses_preparation_validation_until_local_state_changes(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()) as out:
            data = Path(temp)
            prepared = data / 'prepared' / ('d' * 32)
            tile = prepared / 'map/133/84.map.lzma'
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b'unchanged tile')
            entry = {'path': 'map/133/84.map.lzma', 'kind': 'map', 'relative': '133/84.map.lzma',
                     'size': tile.stat().st_size, 'sha256': w.sha(tile), 'countries': ['germany']}
            w.save_json(prepared / 'manifest.json', {'version': 1, 'countries': ['germany'], 'files': [entry]})
            w.save_json(data / 'current.json', {'directory': prepared.name})
            adb = w.ADB('unused')
            with patch.object(adb, 'select'), patch.object(adb, 'inspect', return_value={'ro.product.model': 'ELEMNT-BOLT2'}), patch.object(w, 'inspect_current_prepared', wraps=w.inspect_current_prepared) as inspect, patch('builtins.input', side_effect=['x', 'q']):
                w.interactive(adb, data)
        self.assertEqual(inspect.call_count, 1)
        self.assertEqual(out.getvalue().count('[2] Resume prepared maps'), 2)

    def test_changed_current_prepared_tile_is_not_offered_for_resume(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()) as out:
            data = Path(temp)
            prepared = data / 'prepared' / ('a' * 32)
            tile = prepared / 'map/133/84.map.lzma'
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b'original')
            entry = {'path': 'map/133/84.map.lzma', 'kind': 'map', 'relative': '133/84.map.lzma',
                     'size': len(b'original'), 'sha256': w.sha(tile), 'countries': ['germany']}
            w.save_json(prepared / 'manifest.json', {'version': 1, 'countries': ['germany'], 'files': [entry]})
            w.save_json(data / 'current.json', {'directory': 'a' * 32})
            tile.write_bytes(b'changed')
            adb = w.ADB('fake')
            with patch.object(adb, 'select'), patch.object(adb, 'inspect', return_value={'ro.product.model': 'ELEMNT-BOLT2'}), patch('builtins.input', side_effect=['q']):
                w.interactive(adb, data)
        self.assertNotIn('[2] Resume prepared maps', out.getvalue())
        self.assertIn('Prepared maps cannot be resumed:', out.getvalue())
        self.assertIn('Choose [1] to prepare a fresh selection', out.getvalue())

    def test_sync_error_offers_resume_from_prepared_files(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            data = Path(temp)
            adb = w.ADB('fake')
            catalog = [
                {'country': 'germany', 'kind': kind, 'name': f'germany{"-routing" if kind == "routing" else ""}.zip', 'size': 1, 'release': 'January-2099', 'map_release': 'Maps Without Contour Lines January-2099', 'routing_release': 'Routing January-2099'}
                for kind in ('map', 'routing')
            ]
            prepared = data / 'prepared' / ('a' * 32)
            prepared.mkdir(parents=True)
            entries = []
            for kind, relative in [('map', '133/84.map.lzma'), ('routing', '123/456.gph.lzma')]:
                tile = prepared / kind / relative
                tile.parent.mkdir(parents=True, exist_ok=True)
                tile.write_bytes(b'valid ' + kind.encode())
                entries.append({'path': f'{kind}/{relative}', 'kind': kind, 'relative': relative,
                                'size': tile.stat().st_size, 'sha256': w.sha(tile), 'countries': ['germany']})
            w.save_json(prepared / 'manifest.json', {'version': 1, 'countries': ['germany'], 'files': entries})
            w.save_json(data / 'current.json', {'directory': 'a' * 32})
            with patch.object(adb, 'select'), patch.object(adb, 'inspect', return_value={'ro.product.model': 'ELEMNT-BOLT2'}), patch.object(w.Source, 'catalog_europe', return_value=catalog), patch.object(w, 'choose_countries', return_value=['germany']), patch.object(w, 'prepare_catalog', return_value=prepared), patch.object(w, 'sync', side_effect=w.Error('fixture sync failure')), patch.object(w, 'inspect_current_prepared', wraps=w.inspect_current_prepared) as inspect, patch('builtins.input', side_effect=['1', 'q']):
                w.interactive(adb, data)
            self.assertIn('[2] Resume prepared maps', out.getvalue())
            self.assertIn('Prepared files remain available', out.getvalue())
            self.assertEqual(inspect.call_count, 1)


class HTTPTests(unittest.TestCase):
    def test_truncated_download_and_wrong_source_hash_preserve_cache(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(temp)
            target = directory / 'germany.zip'
            target.write_bytes(b'previous archive')
            item = {'name': target.name, 'id': 'fixture', 'size': 5, 'release': 'fixture', '@microsoft.graph.downloadUrl': 'https://fixture/signed-secret'}
            source = w.Source()
            with patch.object(source, 'open', return_value=io.BytesIO(b'shorter than expected')), self.assertRaises(w.Error):
                source.download(item, directory)
            with patch.object(source, 'open', return_value=io.BytesIO(b'x')), self.assertRaises(w.Error):
                source.download(item, directory)
            bad_hash = {**item, 'file': {'hashes': {'sha256Hash': '0' * 64}}}
            with patch.object(source, 'open', return_value=io.BytesIO(b'12345')), self.assertRaises(w.Error):
                source.download(bad_hash, directory)
            self.assertEqual(target.read_bytes(), b'previous archive')
            self.assertFalse(list(directory.glob('*.part')))

    def test_anonymous_listing_download_reuse_and_truncation(self):
        content = b'fixture-archive'
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                seen.append((self.path, self.headers.get('User-Agent'), self.headers.get('Cookie')))
                base = f'http://127.0.0.1:{self.server.server_port}'
                item = lambda name, ident, folder=True: {'name': name, 'id': ident, 'parentReference': {'driveId': 'D'}, 'folder' if folder else 'file': {}}
                if self.path == '/share':
                    self.send_response(200)
                    self.send_header('Set-Cookie', 'anonymous=fixture; Path=/')
                    self.end_headers()
                    self.wfile.write(b'share')
                    return
                if '/shares/' in self.path:
                    data = item('Wahoo', 'root')
                elif '/items/root/' in self.path:
                    data = {'value': [item('wahooMapsCreator-Maps', 'maps')]}
                elif '/items/maps/' in self.path:
                    data = {'value': [item('Maps Without Contour Lines January-2099', 'map'), item('Routing January-2099', 'routing')]}
                elif '/items/map/' in self.path or '/items/routing/' in self.path:
                    ident = self.path.split('/items/')[1].split('/')[0]
                    data = {'value': [item('europe', ident + '-eu')]}
                elif '-eu/children' in self.path:
                    routing = 'routing-eu' in self.path
                    name = 'germany' + ('-routing' if routing else '') + '.zip'
                    data = {'value': [{**item(name, 'archive', False), 'size': len(content), 'eTag': 'v1', '@microsoft.graph.downloadUrl': base + '/download'}]}
                elif self.path == '/download':
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(content)
                    return
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except PermissionError:
            self.skipTest('Sandbox blocks localhost bind; offline HTTP fixtures run separately.')
        with server, tempfile.TemporaryDirectory() as temp:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                source = w.Source(api=base + '/api', share=base + '/share')
                catalog = source.catalog_europe()
                self.assertEqual(len(catalog), 2)
                with contextlib.redirect_stdout(io.StringIO()):
                    result = source.download(catalog[0], Path(temp))
                    self.assertEqual(result.read_bytes(), content)
                    count = len(seen)
                    source.download(catalog[0], Path(temp))
                    self.assertEqual(count, len(seen))
                    broken = {**catalog[0], 'size': len(content) + 1}
                    with self.assertRaises(w.Error):
                        source.download(broken, Path(temp))
                    self.assertEqual(result.read_bytes(), content)
                self.assertTrue(all(ua == w.UA for _, ua, _ in seen))
                self.assertTrue(all(cookie == 'anonymous=fixture' for _, _, cookie in seen[1:]))
                self.assertFalse(list(Path(temp).glob('*.part')))
            finally:
                server.shutdown()
                thread.join()

    def test_offline_http_catalog_prepare_and_reuse(self):
        """Parse actual HTTP responses, with socket transport replaced by fixtures."""
        seen = []
        buffers = {}
        for kind, name in [('map', '133/84.map.lzma'), ('routing', '123/456.gph.lzma')]:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as z:
                z.writestr(name, kind.encode())
            buffers[kind] = buf.getvalue()

        class FixtureSocket:
            def __init__(self, body):
                self.raw = io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)

            def makefile(self, *args):
                return self.raw

        def item(name, ident):
            return {'name': name, 'id': ident, 'folder': {}, 'parentReference': {'driveId': 'D'}}

        def transport(request, timeout):
            url = request.full_url
            seen.append(url)
            if url.endswith('/share'):
                body = b'share'
            elif '/shares/' in url:
                body = json.dumps(item('Wahoo', 'root')).encode()
            elif '/items/root/' in url:
                body = json.dumps({'value': [item('wahooMapsCreator-Maps', 'maps')]}).encode()
            elif '/items/maps/' in url:
                body = json.dumps({'value': [item('Maps Without Contour Lines January-2099', 'map'), item('Routing January-2099', 'routing')]}).encode()
            elif '/items/map/' in url or '/items/routing/' in url:
                kind = 'routing' if '/routing/' in url else 'map'
                body = json.dumps({'value': [item('europe', kind + '-eu')]}).encode()
            elif '-eu/children' in url:
                kind = 'routing' if 'routing-eu' in url else 'map'
                archive = {**item('germany' + ('-routing' if kind == 'routing' else '') + '.zip', kind), 'file': {}, 'size': len(buffers[kind]), '@microsoft.graph.downloadUrl': 'https://fixture/download/' + kind}
                del archive['folder']
                body = json.dumps({'value': [archive]}).encode()
            elif '/download/' in url:
                body = buffers[url.rsplit('/', 1)[-1]]
            else:
                raise AssertionError(url)
            response = http.client.HTTPResponse(FixtureSocket(body))
            response.begin()
            return response

        source = w.Source(api='https://fixture/api', share='https://fixture/share')
        with tempfile.TemporaryDirectory() as temp, patch.object(source.opener, 'open', side_effect=transport), contextlib.redirect_stdout(io.StringIO()):
            data = Path(temp)
            first = w.prepare_catalog(source, data, source.catalog_europe())
            _, manifest = w.load_prepared(data)
            self.assertEqual(len(manifest['files']), 2)
            self.assertEqual(sum('/download/' in url for url in seen), 2)
            second = w.prepare_catalog(source, data, source.catalog_europe())
            self.assertNotEqual(first, second)
            self.assertEqual(sum('/download/' in url for url in seen), 2)
            self.assertTrue(first.exists())
            # Failure must not publish an incomplete preparation.
            pointer = (data / 'current.json').read_bytes()
            with patch('map_preparation.extract', side_effect=w.Error('broken archive')), self.assertRaises(w.Error):
                w.prepare_catalog(source, data, source.catalog_europe())
            self.assertEqual((data / 'current.json').read_bytes(), pointer)
            self.assertFalse(list((data / 'prepared').glob('.prepare-*')))


if __name__ == '__main__':
    unittest.main()
