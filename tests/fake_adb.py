#!/usr/bin/env python3
"""Filesystem-backed ADB protocol fixture, never accesses USB."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys

root = Path(os.environ['FAKE_ADB_ROOT'])
args = sys.argv[1:]
with (root / 'calls.jsonl').open('a') as log:
    log.write(json.dumps(args) + '\n')
mode = os.environ.get('FAKE_ADB_FAIL', '')
if args == ['devices', '-l']:
    print('List of devices attached\nTEST123 device product:bolt')
    sys.exit()
if args[:2] != ['-s', 'TEST123']:
    sys.exit(8)
args = args[2:]


def local(path):
    assert path.startswith('/sdcard/'), repr(path)
    return root / path.lstrip('/')


if args[0] == 'pull':
    if mode == 'backup':
        Path(args[2]).write_bytes(b'broken backup')
    else:
        shutil.copyfile(local(args[1]), args[2])
elif args[0] == 'push':
    source = Path(args[1])
    destination = local(args[2])
    if source.is_dir():
        target = destination / source.name if destination.is_dir() else destination
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob('*'):
            relative = item.relative_to(source)
            out = target / relative
            if item.is_dir():
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, out)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    if mode == 'upload':
        sys.exit(9)
    if mode == 'routing-upload' and 'routing' in args[2]:
        sys.exit(9)
elif args[0] == 'shell':
    command = args[1]
    if command.startswith('getprop '):
        key = command.split()[1]
        defaults = {'ro.product.model': 'ELEMNT_BOLT2', 'ro.product.manufacturer': 'WahooFitness', 'ro.product.device': 'bolt2p0', 'ro.build.display.id': 'TEST-BUILD'}
        env_key = {'ro.product.model': 'FAKE_ADB_MODEL', 'ro.product.manufacturer': 'FAKE_ADB_MANUFACTURER', 'ro.product.device': 'FAKE_ADB_DEVICE'}.get(key)
        print(os.environ.get(env_key, defaults[key]) if env_key else defaults[key])
    elif command.startswith('dumpsys '):
        print('Package [com.wahoofitness.bolt]\n versionName=TEST' if mode != 'package' else 'Unknown package')
    elif ' sha256sum ' in command or command.startswith('sha256sum '):
        paths_text = command.rsplit('sha256sum ', 1)[1]
        for path in shlex.split(paths_text):
            p = local(path)
            if p.is_symlink() or p.parent.is_symlink() or not p.is_file():
                sys.exit(10)
            print(hashlib.sha256(p.read_bytes()).hexdigest(), path)
    elif command.startswith('if ! ( '):
        pattern = r'if ! \( (.*?) \); then exit 41; fi; mkdir -p (\S+) && mv (\S+) (\S+) \|\| exit 42'
        for match in re.finditer(pattern, command):
            checks, parent, source, target = match.groups()
            paths = re.findall(r'\[ ! -L (\S+) \]', checks)
            if any(local(path).is_symlink() for path in paths):
                sys.exit(41)
            target_path = local(target)
            if target_path.is_symlink() or (target_path.exists() and not target_path.is_file()):
                sys.exit(41)
            if mode == 'rename':
                sys.exit(42)
            local(parent).mkdir(parents=True, exist_ok=True)
            local(source).replace(target_path)
        if not list(re.finditer(pattern, command)):
            sys.exit(11)
    elif command.startswith('rm -rf '):
        shutil.rmtree(local(command[len('rm -rf '):]), ignore_errors=True)
    elif command.startswith('if [ -L ') and '&& echo OK' in command:
        path = re.search(r'else mkdir -p (\S+) && echo OK', command)[1]
        p = local(path + '/placeholder').parent
        if p.is_symlink() or (p.exists() and not p.is_dir()):
            print('UNSAFE')
        else:
            p.mkdir(parents=True, exist_ok=True)
            print('OK')
    elif command.startswith('if [ -L '):
        path = command.split()[3]
        p = local(path)
        print('UNSAFE' if p.is_symlink() or (p.exists() and not p.is_dir()) else 'OK')
    elif command == 'df -k /sdcard':
        stale_stage = any(root.rglob('.wahoo-sync-*'))
        low_space = mode == 'space' or (mode == 'space-reclaimable' and stale_stage)
        print('Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/mock 100000000 1 ' + ('1' if low_space else '99999999') + ' 1% /sdcard')
    elif command.startswith('wc -c < '):
        print(local(command.split()[-1]).stat().st_size)
    elif command.startswith('for p in ') and 'echo MISSING; fi; done' in command:
        paths = shlex.split(command[len('for p in '):].split('; do', 1)[0])
        for path in paths:
            p = local(path)
            if p.is_symlink() or p.parent.is_symlink() or (p.exists() and not p.is_file()):
                print('UNSAFE')
            elif p.is_file():
                print('EXISTS:' + str(p.stat().st_size))
            else:
                print('MISSING')
    elif command.startswith('mkdir -p '):
        local(command.split()[-1]).mkdir(parents=True, exist_ok=True)
    elif command.startswith('mv '):
        if mode == 'rename':
            sys.exit(9)
        _, src, dst = command.split()
        local(src).replace(local(dst))
    elif command.startswith('am broadcast '):
        action = command.rsplit('.', 1)[-1].lower()
        print('Error: cache unavailable' if mode in ('cache', 'cache-' + action) else 'Broadcast completed: result=0')
    else:
        sys.exit(7)
else:
    sys.exit(6)
