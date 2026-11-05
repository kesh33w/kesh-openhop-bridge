# Kesh's OpenHop bridge

Kesh's personal bridge project extends OpenHop with native channel bridging on a Pi between two radios operating on different frequencies. For each configured mapping, the bridge authenticates and decrypts a group message with the source channel key, then re-encrypts it with the destination channel key for transmission on the other radio. Mappings are directional; a reverse mapping enables replies.

Channel translation supports ordinary group text in flood packets, not signed group subtypes, direct messages, or control traffic. The bridge needs authorized keys for both channels and is a trusted plaintext boundary, so translation is not end-to-end encryption across distinct channel keys. Channel authentication does not establish an individual sender's identity. Separately enabled direct-message and acknowledgement routes forward eligible explicit-path packets between radios without decrypting their contents.

The project changes span OpenHop Core, Repeater, Repeater UI, a separate USB Modem fork, and MeshCore TFT firmware:

- Native radio queues and airtime accounting handle crossings, with bounded queues and retries, restart-safe echo suppression, and explicit handling of failed or uncertain transmissions. Receive observations retain radio provenance.
- Bridge settings provide channel mappings, write-only channel keys, direct-message routes, queue limits, and explicit origin and companion home radios. Validated configuration changes require a restart.
- Separate USB bench modem targets support Heltec T096 and V4.3 hardware with restricted radio profiles and transmit power.
- An optional TLS Internet Link connects the Pi to a MeshCore-based Heltec V4.3 TFT radio. The TFT adds on-screen Wi-Fi setup, enrollment, and identity generation during installation. Local RF startup remains independent of optional link failures.

This hub pins compatible component commits and provides reconstruction instructions, integration tests, and firmware releases. Test scope and hardware limitations are recorded below.

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

[Releases](https://github.com/kesh33w/sacmesh-bridge/releases) provide the merged TFT image, checksum and source manifest. Flash a merged image at `0x0` with a full erase. This replaces existing firmware, identity and settings. Browser USB flashing remains unverified. The current endpoint is a trial service hosted at sacmesh.org.

One encrypted recipient message and return ACK passed through the Pi and TFT. This bounded test does not establish endurance or field reliability. The final Internet Link implementation previously passed 191 backend tests, 26 UI tests, type checking and a production build. Fresh hub integration checks passed 165 tests. Focused publication checks passed 42 backend checks, 17 UI checks, type checking and a production build. CI status is reported separately from local tests.

Kesh uses the sacmesh.org domain for this project. The [SACMesh website](https://sacmesh.org/firmware/) is managed separately. See [credits and licenses](NOTICE.md).
