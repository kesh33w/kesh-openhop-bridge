# Bridge source sync, November 9, 2026

This snapshot pins the reviewed bridge controls, protocol routing and reception diagnostics. Updating these references does not deploy software, restart a service or change live configuration.

## Current development pins

- OpenHop Core: [55c44ca1e752f7e12cf94c9fe453db7aa69b16e7](https://github.com/kesh33w/openhop_core/commit/55c44ca1e752f7e12cf94c9fe453db7aa69b16e7).
- OpenHop Repeater: [99cf1a265fd1c64167b91e54b5ec917cf6841e98](https://github.com/kesh33w/openhop_repeater/commit/99cf1a265fd1c64167b91e54b5ec917cf6841e98).
- Repeater UI: [79d5c98111686dddcd3500434469886decb0db1c](https://github.com/kesh33w/openHop_RepeaterUI/commit/79d5c98111686dddcd3500434469886decb0db1c).

The modem and firmware pins are unchanged. The preceding manifest is preserved at [components-2026-11-08.json](source-snapshots/components-2026-11-08.json). The historical `components.json`, release manifests, tags and release assets remain unchanged.

Reconstruct this snapshot in a clean checkout with:

```sh
python3 tools/materialize.py --manifest components.current.json
```

## Changes and validation

The backend and UI add bridge pause/resume, separate protocol controls, distinct native K and crossing V routing, and reception evidence. New protocol modes and independent observer reception tracking remain opt-in. A protocol routing-mode change requires a restart and cross-contact path rediscovery. Disabling or pausing legacy direct transit does not provide a local fallback; allowed group repeats and distinct native routing continue.

The published backend and UI trees exactly match the final reviewed patch trees. The recorded checks for those trees used Python 3.12.12 on macOS and the Core commit above:

- Focused backend: 537 passed.
- Broad backend excluding the three local-socket IPC files: 3,743 passed, 8 skipped, 3 failed, and 26 subtests passed. The same three failures were reproduced on the base: two MQTT neighbor fixtures lack `_reply_home_radio`, and one README test expects upstream installer wording.
- IPC checks with temporary local Unix-socket access: 16 passed.
- UI: all 524 tests, type checking and production build passed. Bridge components and tests pass targeted lint. Full UI lint retains 180 existing errors.
- Changed backend tests pass Ruff lint and formatting. The legacy engine passes F/E9 checks. Strict OpenAPI, Python compilation and diff whitespace checks pass.

See the [backend validation record](https://github.com/kesh33w/openhop_repeater/blob/99cf1a265fd1c64167b91e54b5ec917cf6841e98/docs/bridge-validation-2026-11-09.md), [protocol routing guide](https://github.com/kesh33w/openhop_repeater/blob/99cf1a265fd1c64167b91e54b5ec917cf6841e98/docs/bridge-protocol-routing.md) and [reception evidence guide](https://github.com/kesh33w/openhop_repeater/blob/99cf1a265fd1c64167b91e54b5ec917cf6841e98/docs/bridge-reception-evidence.md).

These are recorded local checks, not an all-green aggregate result or evidence of end-to-end RF delivery. This pin update did not rerun the hub integration suite. Both backend and UI remote branch heads were verified after publication. No commit statuses or GitHub Actions workflow runs were reported for these exact commits at that check.
