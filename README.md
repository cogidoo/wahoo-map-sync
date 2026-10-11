# Wahoo Map Sync

**Experimental community tool for Wahoo ELEMNT BOLT v2 on macOS Apple Silicon.**

This standard-library Python CLI downloads pre-generated Europe community map
and routing archives, validates them, and can install selected tiles on a BOLT
v2 over ADB. Germany is selected by default. The project is independent and is
not affiliated with or endorsed by Wahoo.

## Status

The tool has been tested only on an ELEMNT BOLT v2. Map display and routing
worked after a sync and device restart. This does not establish compatibility
with other device models. The project remains experimental.

Automated tests use synthetic archives, local HTTP fixtures, and a filesystem
fake ADB. They do not write to hardware. The real device flow requires an
explicit confirmation before it changes device files.

## Requirements

- macOS on Apple Silicon
- Python 3.9 or newer; no third-party Python packages are required
- Android SDK Platform-Tools with native arm64 `adb`
- An ELEMNT BOLT v2, USB data cable, internet access, and enough device storage

Install ADB from the [official Android Platform-Tools page](https://developer.android.com/tools/releases/platform-tools).
The CLI checks that ADB contains an arm64 slice on macOS. Android Studio is not
required.

## Install from a fresh clone

```sh
git clone https://github.com/YOUR-ACCOUNT/wahoo-map-sync.git
cd wahoo-map-sync
./wahoo-maps --help
```

Replace `YOUR-ACCOUNT` with the GitHub user or organization that hosts the
repository.

There is no package installation step. If `adb` is not on `PATH`, pass its path
with `--adb`.

## Run

```sh
./wahoo-maps
# or, when needed:
./wahoo-maps --adb /path/to/platform-tools/adb
```

Connect and authorize the BOLT v2 over USB. The CLI selects one ADB device,
checks the Wahoo app and reported model/build, and lists countries that have
paired map and routing archives in a shared release. Germany is preselected.
Use the arrow keys to move, Space to select or clear a country, and Enter to
confirm. The CLI displays archive dates, download size, destination device,
serial, and available storage before asking `Write maps now? [y/N]`. Only `y`
starts device writes.

Prepared map files are hashed locally, uploaded into temporary device locations,
verified by SHA-256, promoted into place, and verified again. Existing tiles
remain until their replacement has been uploaded and checked. Promotion is
atomic per file, not for the entire country set; an interruption during
promotion can leave a mixed set, and the tool does not roll back. It does not
delete unselected countries, modify firmware or themes, enable VTM, or change
`routing-config.json`.

## Recovery

If a run stops after preparation, restart the CLI and choose the offered resume
action. A retry is bound to the prepared manifest from that run and sends the
selected files again. Failed run journals and prepared files are retained.
Prepared files referenced by any unfinished journal are kept when a different
device sync succeeds. Old journals that predate manifest binding are archived
automatically with a warning that they may have left partial tiles; no device
files are changed by that local recovery step. A journal tied to missing or
invalid prepared files, or a damaged journal, blocks writes. The manager offers
`[a] Resolve blocked recovery`; after a separate `y` confirmation, valid journal
history is marked acknowledged and damaged journal bytes are preserved under a
new filename. This records the decision and permits a fresh selection, but does
not repair or change device tiles.

After target hashes pass, the CLI sends Wahoo's `PURGE` and `RELOAD_MAP` cache
commands. Successful command responses do not by themselves prove that maps
render or routing works; check the device after it reloads.

Runtime data is stored outside the repository at
`~/Library/Application Support/wahoo-map-sync`. Use `--data-dir` to choose a
different location. Keep that directory when moving the repository if you need
to retain prepared files or run journals.

## Development

```sh
python3 -m unittest discover -s tests -v
./wahoo-maps --help
```

Tests use the fake ADB and local fixtures; no connected device or archive
download is needed. GitHub Actions runs the same checks on Python 3.9 and 3.13
on Linux, and Python 3.13 on macOS.

## Sources and licensing

- Map and routing archives come from the [Wahoo Maps Creator community project](https://github.com/treee111/wahooMapsCreator) and its [public archive share](https://1drv.ms/f/s!AgQ5_SDAjeL0eIUfrjTBE8QWGZc?e=RrEqBF).
- Device and tile behavior references include [Wahoo's map support documentation](https://support.wahoofitness.com/hc/en-us/articles/115000217964-Add-update-or-delete-Maps-on-an-ELEMNT-BOLT-1-2-or-ROAM1-2-computer) and [Android Platform-Tools](https://developer.android.com/tools/releases/platform-tools).

The [MIT license](LICENSE) applies to the source code in this repository only.
It does not license third-party map/routing archives, upstream projects,
trademarks, or other referenced material. Review the archive provider's terms
before using or redistributing its data.
