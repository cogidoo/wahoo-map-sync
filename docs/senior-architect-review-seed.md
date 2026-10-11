# Senior architect review seed

Use this brief to start an independent architecture and reliability review.
Inspect the current code and tests before relying on this summary. Return
findings and recommendations without changing files.

## Objective

Assess whether this experimental Python CLI safely prepares third-party Europe
community map/routing archives and installs selected tiles on a Wahoo ELEMNT
BOLT v2. Focus on correctness at failure boundaries, recoverability, and
whether the implementation remains appropriately small.

## Current design

- `wahoo-maps` launches `wahoo_maps.py`. It uses the Python standard library
  and supports Python 3.9+.
- The interactive flow resolves paired archives from a public OneDrive share,
  downloads selected ZIPs, validates and extracts tiles, and publishes a local
  manifest. Germany is preselected.
- Runtime data defaults to
  `~/Library/Application Support/wahoo-map-sync`; `--data-dir` overrides it.
- Device operations use one selected ADB serial. The CLI reports device/app
  details and requires interactive `y` confirmation before writes.
- The CLI hashes prepared files locally, stages grouped map and routing trees
  with two ADB pushes, verifies staged SHA-256 values, promotes files using
  guarded renames, verifies final hashes, and sends `PURGE` and `RELOAD_MAP`.
- Promotion is atomic per file, not for a complete selection. A promotion
  interruption may leave mixed old/new tiles. There is no rollback or target
  backup.
- Journals bind a run to the prepared directory and manifest hash. Hard-aborted
  nonterminal states are pending. Pre-manifest legacy journals without either
  binding field are automatically retained as `orphaned_legacy`; journals with
  a binding but missing/invalid prepared content remain pending. Corrupt
  journals block writes until explicitly acknowledged, with the damaged bytes
  retained. Successful cleanup preserves prepared data and payloads referenced
  by unfinished journals for any device.
- Tests use synthetic ZIPs, offline HTTP fixtures, and a filesystem-backed fake
  ADB. One localhost HTTP test may skip if socket binding is prohibited.

## Safety properties to verify

1. Inspection and preparation never write to a device.
2. ADB operations remain pinned to the selected serial.
3. Incompatible or ambiguous devices are handled explicitly.
4. Only interactive `y` begins device writes.
5. Unsafe ZIP paths, symlinks, special files, empty/invalid tiles, and
   conflicting overlaps are rejected before writes.
6. Prepared, staged, and final files are checked against SHA-256 values.
7. A sync is not reported complete until final checks and both cache commands
   succeed.
8. Interrupted runs bind to the correct manifest; legacy recovery does not
   silently discard journals or claim to repair partial device state.
9. Unselected countries, firmware, themes, VTM settings, and
   `routing-config.json` are not changed as side effects.
10. Tests protect existing device files in failure cases and do not overstate
    what fake ADB establishes.

## Review questions

- Are source discovery, archive preparation, device operations, recovery, and
  CLI responsibilities clear without unnecessary framework structure?
- Are journal transitions and local manifests safe across process termination
  during preparation, either ADB push, promotion, verification, or cache reload?
- Are hard-link payloads and cleanup safe for supported filesystem layouts?
- Are device path checks, shell command construction, serial pinning, and time
  limits robust against unexpected device output or state?
- Does the UI make it clear when an ADB subprocess is progressing, when device
  hashes have been checked, and when cache commands have completed?
- Which gaps block a controlled hardware trial, and which are later improvements?

## Evidence limits

Map display and routing were confirmed after a sync and device restart on a
BOLT v2. The CLI reported target verification and successful cache responses.
This evidence is limited to BOLT v2 and does not establish universal archive
compatibility or durability under power loss.

Tests remain synthetic/fake-device evidence. Do not perform archive downloads,
device writes, external messaging, or hardware actions during the review.

## Expected output

1. Findings first, ordered by severity, with affected function/file, scenario,
   consequence, and a concrete recommendation.
2. A short architecture assessment and explanation of the staging/promotion
   tradeoff.
3. Prioritized actions separating controlled hardware-trial blockers from
   later improvements.
4. Clear limits between source review, tests, live source/archive checks, and
   physical-device validation.
