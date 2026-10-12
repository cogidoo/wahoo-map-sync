import io
import unittest
from unittest.mock import patch

import wahoo_maps as w


class SelectionTests(unittest.TestCase):
    def test_no_device_message_explains_how_to_connect_bolt(self):
        adb = w.ADB('fake')
        with patch.object(adb, 'run', return_value='List of devices attached\n'), self.assertRaises(w.Error) as raised:
            adb.select()
        self.assertIn('Connect and authorize your ELEMNT BOLT v2', str(raised.exception))

    def test_country_order_and_default_selection(self):
        catalog = [{'country': c} for c in ('austria', 'denmark', 'germany', 'poland', 'czech-republic')]
        countries = w.ordered_countries(catalog)
        self.assertEqual(countries, ['germany', 'poland', 'czech-republic', 'denmark', 'austria'])
        self.assertEqual(w.default_countries(countries), {'germany'})

    def test_multiple_devices_auto_select_unique_known_bolt_v2(self):
        adb = w.ADB()
        props = {
            'PHONE': {
                'ro.product.model': 'Pixel',
                'ro.product.manufacturer': 'Google',
                'ro.product.device': 'phone',
                'ro.build.display.id': 'PHONE_BUILD',
            },
            'BOLT': {
                'ro.product.model': 'ELEMNT_BOLT2',
                'ro.product.manufacturer': 'WahooFitness',
                'ro.product.device': 'bolt2p0',
                'ro.build.display.id': 'BOLT_BUILD',
            },
        }
        calls = []

        def run(*args, live=False):
            calls.append((adb.serial, args))
            if args == ('devices', '-l'):
                return 'List of devices attached\nPHONE device\nBOLT device\nLOCKED unauthorized'
            if args[0] == 'shell' and args[1].startswith('getprop '):
                return props[adb.serial][args[1][len('getprop '):]]
            if args == ('shell', 'echo pinned'):
                return 'ok'
            raise AssertionError(args)

        with patch.object(adb, 'run', side_effect=run):
            self.assertEqual(adb.select(), 'BOLT')
            adb.shell('echo pinned')
        self.assertEqual(adb.serial, 'BOLT')
        self.assertEqual(calls[-1], ('BOLT', ('shell', 'echo pinned')))

    def test_multiple_or_ambiguous_devices_never_choose_arbitrarily(self):
        adb = w.ADB()
        props = {
            serial: {
                'ro.product.model': 'ELEMNT_BOLT2',
                'ro.product.manufacturer': 'WahooFitness',
                'ro.product.device': 'bolt2p0',
                'ro.build.display.id': 'TEST',
            }
            for serial in ('BOLT_A', 'BOLT_B')
        }

        def run(*args, live=False):
            if args == ('devices', '-l'):
                return 'List of devices attached\nBOLT_A device\nBOLT_B device'
            return props[adb.serial][args[1][len('getprop '):]]

        with patch.object(adb, 'run', side_effect=run), self.assertRaisesRegex(
            w.Error, 'Disconnect other devices and retry'
        ):
            adb.select()
        self.assertIsNone(adb.serial)

    def test_cli_does_not_offer_serial_argument(self):
        output = io.StringIO()
        with patch('sys.stdout', output), self.assertRaises(SystemExit) as raised:
            w.main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        self.assertNotIn('--serial', output.getvalue())

    def test_country_picker_exits_on_ctrl_d_or_stream_eof(self):
        catalog = [
            {'country': 'denmark', 'kind': kind, 'size': 1, 'release': 'release', 'name': name}
            for kind, name in (('map', 'denmark.zip'), ('routing', 'denmark-routing.zip'))
        ]

        class TerminalInput(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 0

        class TerminalOutput(io.StringIO):
            def isatty(self):
                return True

        for value in ('\x04', ''):
            with (
                self.subTest(value=repr(value)),
                patch('sys.stdin', TerminalInput(value)),
                patch('sys.stdout', TerminalOutput()),
                patch('termios.tcgetattr', return_value=[]),
                patch('termios.tcsetattr'),
                patch('tty.setraw'),
            ):
                self.assertIsNone(w.choose_countries(catalog))

    def test_x86_only_adb_is_rejected_before_launch(self):
        with patch('wahoo_maps.shutil.which', return_value='/fixture/adb'), patch('wahoo_maps.sys.platform', 'darwin'), patch('wahoo_maps.subprocess.run') as run:
            run.return_value.returncode = 1
            with self.assertRaises(w.Error):
                w.native_adb('adb')
            self.assertEqual(run.call_args[0][0], ['/usr/bin/lipo', '-verify_arch', 'arm64', '/fixture/adb'])
