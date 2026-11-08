# Source sync, November 8, 2026

This record describes published source and recovery files. Updating these references does not install firmware, deploy a service, or change live configuration.

## Current source pins

`components.current.json` selects these immutable commits. The materializer accepts all seven entries and checks out each component separately.

| Component | Branch | Commit |
| --- | --- | --- |
| OpenHop Core | `kesh/bridge` | `9f609aac7a204a5db8ed7bc6361426ca80a9cd3c` |
| OpenHop Repeater | `kesh/bridge` | `045edf76a8737f139c789e89626d8f2f207cea1a` |
| Repeater UI | `kesh/bridge` | `ef9427b5270e44a2909e264ea97dd4eedde9e712` |
| OpenHop Modem | `kesh/heltec-bench` | `05a042c034d019b8bf8c77a15dee4b4abbc4761f` |
| MeshCore V4.3 TFT | `kesh/v43-tft-ilink` | `350f2569bbc4f41b2ee93d8732f3af103075ffbf` |
| MeshCore V4.3 OLED | `kesh/v43-oled-ilink` | `ddc27fb99cb34b21e6f8a0943bf4e64237961012` |
| MeshCore Heltec V3 OLED | `kesh/v3-oled-ilink` | `efa67849b4ad7fb4912a8625944c7a63ece31a31` |

The preceding development manifest is preserved at [components-2026-11-06.json](source-snapshots/components-2026-11-06.json). `components.json`, `release-manifest.json`, release-specific manifests, tags and published assets are unchanged.

## Published changes and checks

- Repeater [ff4b63d](https://github.com/kesh33w/openhop_repeater/commit/ff4b63d26e98554270be3e72e5d8932c3fe8a69d) adds per-radio observer signing identities, configuration validation, per-radio counters, safe API reporting and tests. The [observer guide](https://github.com/kesh33w/openhop_repeater/blob/ff4b63d26e98554270be3e72e5d8932c3fe8a69d/docs/separate-observer-identities.md) contains the complete configuration example. The optional four-line addition to `config.yaml.example` was not included; runtime defaults and behavior are unaffected.
- Repeater [045edf7](https://github.com/kesh33w/openhop_repeater/commit/045edf76a8737f139c789e89626d8f2f207cea1a) preserves the effective radio association on queued local companion loopback while keeping the transmitted packet's RF receive metadata unchanged. Tests cover decoded and raw delivery, channel and radio isolation, and failed sends.
- Combined Repeater testing against Core `9f609aa` passed 3,403 tests and 26 subtests. Seven failures and four errors exactly match an unchanged `f598136` baseline. Both runs skipped eight tests. The baseline passed 3,292 tests and 22 subtests. The focused observer, token-refresh and packet-router run passed 209 tests and 26 subtests. Changed Python files pass Ruff 0.15.14 and AST parsing. The OpenAPI check retains the same four undocumented endpoints found on baseline. These are not all-green aggregate results.
- The integration suite passed 179 tests against the selected Core and Repeater source with the loopback/simulated-hardware guard. Python tests used 3.12 on macOS; the README's recorded Python 3.14 environment remains a separate configuration.
- V3 [efa6784](https://github.com/kesh33w/MeshCore/commit/efa67849b4ad7fb4912a8625944c7a63ece31a31) publishes three source/build files. A fresh PlatformIO 6.2.0 native run passed 37 tests. Packaging exactly reconstructed both released binaries, rejected seven invalid input cases and passed dependency-pin checks. This sync did not perform a fresh full firmware build. The V3 guide distinguishes recorded flash/boot/RF checks from unverified visual OLED, enrollment, MQTT/iLink, soak and recipient-delivery checks.
- Website recovery [e85d4b0](https://github.com/kesh33w/sacmesh-website/commit/e85d4b01fd56dad2a448483afa51a3c2519552b2) refreshes the public firmware content and 15-asset list. Five binary hashes were measured; four match published checksums. The older November 6 TFT binary has a measured hash only. All 35 website tests and an isolated restore passed. The CMS was unreachable, so firmware records public-rendered provenance and an unknown revision. Homepage and navigation retain their earlier capture. See the [website validation record](https://github.com/kesh33w/sacmesh-website/blob/e85d4b01fd56dad2a448483afa51a3c2519552b2/docs/firmware-refresh-2026-11-08.md).

Remote refs and published file contents were checked after publication. No commit statuses or pull-request-triggered workflow runs were reported for these source-sync commits. Local checks do not establish end-to-end RF behavior. Original working trees and historical release files were preserved. No operational keys, credentials, databases, device dumps or live configuration were added.

## Companion isolation and mapping examples

A separate approved Pi configuration change was applied and verified on November 8 at 04:10 UTC. The persisted configuration was reread after restart and matched its prepared candidate. This source sync did not apply that change.

For ordinary companion channel traffic, companions homed on `host-rf` select `receive_radio_ids: [host-rf]`; the companion homed on `bay` selects `receive_radio_ids: [bay]`. The home radio controls transmission and does not replace the receive filter. Already queued messages and app history are not retroactively removed.

The verified directional mappings are:

- `host-rf #sfsacbridge` to `bay #sfsacbridge`, plus the reverse direction.
- `host-rf #thegoodplace` to `bay #thegoodplace`, plus the reverse direction.
- Existing `bay Public` to `host-rf #910public`, plus the reverse direction.

An intentional mapping can deliver a message to companions on its destination radio. An unrelated `#test` channel is not included in these mappings. Six offline mapping simulations, echo-protection tests and decoded/raw receive-filter checks passed. No on-air test messages were sent, and no live RF end-to-end delivery claim follows from these checks. Installations must supply their own authorized channel keys outside source control.
