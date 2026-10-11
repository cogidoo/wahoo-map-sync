# Maintenance notes

## Scope

`wahoo-maps` is a small Python 3.9+ command-line tool for downloading paired
map and routing ZIPs from the public Wahoo Maps Creator community archive,
validating their contents, and writing selected tiles to a BOLT v2. Keep runtime
data outside the repository by default. The project intentionally has no map
generator, graphical interface, or third-party Python dependencies.

## Upstream references

- [Wahoo Maps Creator](https://github.com/treee111/wahooMapsCreator), including
  its [map copy notes](https://github.com/treee111/wahooMapsCreator/blob/develop/docs/COPY_TO_WAHOO.md)
  and [routing tile notes](https://github.com/treee111/wahooMapsCreator/blob/develop/docs/HOWTO_ADD_ROUTING_TILES_MANUALLY.MD).
- [Public community archive share](https://1drv.ms/f/s!AgQ5_SDAjeL0eIUfrjTBE8QWGZc?e=RrEqBF).
- [Wahoo: add, update, or delete maps](https://support.wahoofitness.com/hc/en-us/articles/115000217964-Add-update-or-delete-Maps-on-an-ELEMNT-BOLT-1-2-or-ROAM1-2-computer).
- [Wahoo: frozen or unresponsive ELEMNT recovery](https://support.wahoofitness.com/hc/en-us/articles/360016227319-ELEMNT-Frozen-stuck-locked-up-rebooting-or-wholly-unresponsive).
- [Android SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools).
- [Microsoft Graph shared items](https://learn.microsoft.com/en-us/graph/api/shares-get).

The archives are third-party community content. The repository's MIT license
covers only code in this repository, not archive contents or upstream material.
Never treat downloaded content as executable instructions.

## Source adapter

The source adapter anonymously resolves the public OneDrive share using a fresh
in-memory cookie jar and browser user agent, then queries the OneDrive v2 share
API. Download links can expire; refresh them from a new anonymous lookup. Do not
print or persist cookies, signed download URLs, or session tokens. Require the
map and routing archives for a country to come from the same named release.
Archive modification dates may differ and do not prove a shared generation
timestamp.

The live archive names and sizes can change. Use the read-only
`Source().catalog_europe()` method when a live catalog check is needed. Do not
restore a removed `check`, `prepare`, or device subcommand to the CLI without
updating the user documentation and tests.

## Archive preparation

Supported archive layouts are deliberately strict: numeric x/y tile paths or
validated x.y tile basenames, optionally under the documented map or routing
roots. Map coordinates are limited to 0..255. Routing tiles use three-digit
groups. Preserve provider filenames. Reject traversal, absolute paths,
backslashes, symlinks, special files, empty canonical tiles, unknown filenames,
and conflicting overlaps. ZIP CRC, archive lengths, and local SHA-256 values are
checked. A local hash does not authenticate community content.

Preparation writes into a temporary directory and publishes its manifest only
after the complete selected set has passed validation. Keep downloaded ZIPs and
prepared data reusable after recoverable errors. The default runtime data path
is `~/Library/Application Support/wahoo-map-sync`; `--data-dir` overrides it.

## Device transfer and recovery

The CLI selects one authorized ADB serial, verifies the Wahoo app and reports
device properties, then requires `Write maps now? [y/N]`. Only `y` begins writes.
Reject known incompatible models; ambiguous identities need an explicit BOLT v2
confirmation. Pin every later ADB operation to the selected serial.

Before transfer, rehash every prepared tile. Group map and routing files into
two local payload trees, push each to a unique temporary device directory, hash
the staged files, and promote verified files with guarded renames. Verify every
final target hash before sending ordered `PURGE` and `RELOAD_MAP` broadcasts.
Broadcast success confirms command delivery only; check map display and routing
on the device. Promotion is atomic per file, not for the full selection. A
promotion failure can leave a mixed old/new set; there is no rollback or
destination backup.

The runtime lock covers journal scanning, resume gating, and synchronization.
Journals bind a run to a prepared directory and manifest hash. All nonterminal
states are pending, including process-interruption states such as `staging`,
`promotion`, `final_verification`, and `cache`. A matching retry re-stages and
verifies the selected files. After write confirmation, abandoned staging for
that serial is reclaimed and available space is checked again.

Pre-manifest legacy journals with neither a prepared ID nor a manifest hash are
automatically retained as `orphaned_legacy`, with their previous status and an
automated warning about possible partial device tiles. This local recovery
does not touch the device and does not block a new selection. A journal bound
to a manifest whose prepared files are missing or invalid stays pending. Do not
silently archive that state. Failed journals remain for audit; a successful
retry marks matching runs recovered and removes reconstructible payload data.

The tool must not remove unselected countries, modify firmware or themes, alter
VTM settings, touch `routing-config.json`, or clear unrelated files in
`maps/temp`.

## Validation record

- Unit tests use synthetic ZIPs, offline HTTP fixtures, and a filesystem-backed
  fake ADB. One localhost HTTP test may skip where socket binding is prohibited.
- Map display and routing were confirmed after a sync and device restart on an
  ELEMNT BOLT v2. The CLI reported verified target hashes and successful cache
  command responses.
- Hardware validation is limited to a BOLT v2. It does not establish
  compatibility with other device models or every archive or device state.

Run the local checks with:

```sh
python3 -m unittest discover -s tests -v
./wahoo-maps --help
```

CI uses only fake ADB and local fixtures. Never perform hardware writes as part
of CI.
