# SACMesh bridge

SACMesh combines native OpenHop channel bridging on a Pi with a TLS Internet Link to a MeshCore-based Heltec V4.3 TFT radio. The TFT has Wi-Fi setup on its display and generates its identity during installation.

## Source

`components.json` pins compatible public commits. The source forks are [Core](https://github.com/kesh33w/openhop_core), [Repeater](https://github.com/kesh33w/openhop_repeater), [Repeater UI](https://github.com/kesh33w/openHop_RepeaterUI), [Modem](https://github.com/kesh33w/openhop_modem) and [MeshCore TFT](https://github.com/kesh33w/MeshCore/tree/sacmesh/v43-tft-internet-link-20261105). The OpenHop Modem branch is the separate USB bench modem. The TFT branch derives from MeshCore observer commit `d664d89df66bdacc1319900a2e2d152fa31b9dc9`.

This hub contains the Python Internet Link sidecar, portable enrollment source and integration tests. Runtime keys, client certificates, Wi-Fi passwords, live routes and enrollment records are installation inputs and are excluded.

## Reconstruct and test

Use Python 3.14 for the recorded environment. From a new checkout:

```sh
python3 tools/materialize.py
python3 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/pip install --no-deps -e sources/openhop_core -e sources/openhop_repeater
PYTHONPATH=.:sources/openhop_core/src:sources/openhop_repeater:tools .venv/bin/python -m pytest -p bench_test_guard private_link/tests tests -q --timeout=10
```

The tests use loopback sockets and simulated radios. They do not require credentials or connected hardware. An operating-system sandbox that blocks socket wakeups can prevent the threaded event-loop test from finishing.

For the frontend, use Node 22 and the UI fork's package lock, then run `npm ci`, `npm run type-check`, `npm run test:unit -- --run --maxWorkers=2`, and `npm run build-only`. For TFT build instructions see `SACMESH-TFT.md` in the MeshCore branch. The private link sidecar must be on the daemon's Python path. `enrollment/pi_enrollment_entry.py --check` checks imports without starting a service; activation requires explicit configuration, CA, issuer-key and state paths. Configure routes and trust deliberately before activation.

## Firmware and verification

The November 6 automatic-advert release restores the standard 16-second zero-hop boot advert and existing saved advert timers. Existing zero intervals stay disabled; configure `set advert.interval 60` and `set flood.advert.interval 47` for persistent hourly local and 47-hour flood adverts. This release passed the TFT build, preprocessing and image checks, with no new RF test.

[Releases](https://github.com/kesh33w/sacmesh-bridge/releases) provide the merged TFT image, checksum and source manifest. Flash a merged image at `0x0` with a full erase. This replaces existing firmware, identity and settings. Browser USB flashing remains unverified. The current endpoint is a SACMesh trial service.

One encrypted recipient message and return ACK passed through the Pi and TFT. This bounded test does not establish endurance or field reliability. The final Internet Link implementation previously passed 191 backend tests, 26 UI tests, type checking and a production build. Fresh hub integration checks passed 165 tests. Focused publication checks passed 42 backend checks, 17 UI checks, type checking and a production build. CI status is reported separately from local tests.

The [SACMesh website](https://sacmesh.org/firmware/) is managed separately. See [credits and licenses](NOTICE.md).
