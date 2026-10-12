# Project instructions

## Purpose

Maintain a small, standard-library Python CLI for preparing pre-generated Europe
community maps and routing tiles for a Wahoo ELEMNT BOLT v2. Keep the tool
focused; do not add local map generation, Electron, or third-party Python
dependencies without a clear need.

## Safety invariants

- Preparation and inspection must never write to a connected device.
- Select the sole authorized ADB device or uniquely identify a BOLT v2 among
  multiple authorized devices. Pin later commands to its serial.
- Verify the Wahoo app and display the reported model and build. Do not silently
  treat a generic Android device, BOLT v1, or BOLT v3 as a BOLT v2.
- Show the selected device and serial, then require an interactive `y` before
  device writes.
- Verify prepared hashes and reject unsafe archive paths, symlinks, special
  files, empty archives, invalid tile names, and conflicting overlapping tiles.
- Upload to temporary device locations, verify SHA-256, then promote files.
- Never delete unselected countries, modify firmware or themes, change VTM, or
  touch `routing-config.json` as a side effect of map updates.
- Do not report a complete sync until target hashes and cache commands succeed.
  Preserve journals after failures.
- Keep hardware evidence distinct from fake-device and fixture tests.

## Development

- Read `README.md` and `docs/maintenance.md` before changing source discovery,
  archive preparation, or device-transfer behavior.
- Run `python3 -m unittest discover -s tests -v` and `./wahoo-maps --help` after
  implementation changes.
- Use local HTTP fixtures and `tests/fake_adb.py` for transfer tests. Include
  failures that protect existing device files.
- Do not download archives or perform device writes for routine development.
  Any real hardware write requires the user's explicit confirmation.
- Keep user-facing CLI text and project documentation in English.
