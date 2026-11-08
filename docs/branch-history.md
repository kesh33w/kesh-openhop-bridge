# Active branches and preserved release history

This is Kesh's personal OpenHop bridge project. The active branches use Kesh/OpenHop names. iLink means Internet Link; the V4.3 TFT iLink firmware is based on the MeshCore Observer lineage.

## Current development and published releases

The hub defaults to `kesh/openhop-bridge`. Core, Repeater and Repeater UI default to `kesh/bridge`; Modem defaults to `kesh/heltec-bench`. MeshCore retains its unrelated `dev` default. Select `kesh/v43-tft-ilink` for V4.3 TFT hardware, `kesh/v43-oled-ilink` for V4.3 OLED, or `kesh/v3-oled-ilink` for Heltec V3 OLED explicitly.

Branch heads can advance. The tables below preserve the November 6 source snapshot and historical branch-creation mapping. The original branches remain available; their current heads should not be treated as immutable release identifiers.

[components.current.json](../components.current.json) now records the November 8 source snapshot, including both OLED variants. See the [current source sync record](source-sync-2026-11-08.md) for exact revisions and validation. The [November 6 development manifest](source-snapshots/components-2026-11-06.json) preserves the preceding snapshot byte-for-byte. Use `python3 tools/materialize.py --manifest components.current.json` from this hub for current source, or select the dated manifest for the older snapshot. Omitting `--manifest` still selects the original `components.json`.

The `sacmesh-v43-tft-20261105` release still points to hub commit `dd4001c39e7506b901a9c8cb4238c07c81e064c6`. Its assets, checksums and source manifest are unchanged. `components.json` and `release-manifest.json` retain the original published pins, including TFT commit `85af7e596bde8e03b09e29f47cde40187972740e`. Check out the release's hub commit and use its original manifest for historical reconstruction.

## Audited source snapshot, November 6, 2026

These are published source revisions observed during the audit. Later documentation commits in this hub do not change the source revisions below.

| Repository | Active branch | Audited source commit |
| --- | --- | --- |
| kesh-openhop-bridge | `kesh/openhop-bridge` | [`662e7f45`](https://github.com/kesh33w/kesh-openhop-bridge/commit/662e7f459891a0e72a3c25bcb1a9f2b7d31eb9d9) |
| openhop_core | `kesh/bridge` | [`8c825c3e`](https://github.com/kesh33w/openhop_core/commit/8c825c3e1ab72bd65b567d2c75c704b7f17babeb) |
| openhop_repeater | `kesh/bridge` | [`93a91613`](https://github.com/kesh33w/openhop_repeater/commit/93a91613ff4035b7c879c095e1c2ef4478607646) |
| openHop_RepeaterUI | `kesh/bridge` | [`befc3c16`](https://github.com/kesh33w/openHop_RepeaterUI/commit/befc3c168c2119d016329a2454b30494687593f6) |
| openhop_modem | `kesh/heltec-bench` | [`f8d718be`](https://github.com/kesh33w/openhop_modem/commit/f8d718be2b7142f69ad3caec5e2fcab15fd0f898) |
| MeshCore TFT | `kesh/v43-tft-ilink` | [`350f2569`](https://github.com/kesh33w/MeshCore/commit/350f2569bbc4f41b2ee93d8732f3af103075ffbf) |

The later [local60 TFT release](https://github.com/kesh33w/kesh-openhop-bridge/releases/tag/sacmesh-v43-tft-20261106-local60) retains its own original source manifest and binary checksums. Its source provenance must not be replaced with the active branch's corrected-history commit. The [TFT guide](https://github.com/kesh33w/MeshCore/blob/350f2569bbc4f41b2ee93d8732f3af103075ffbf/OPENHOP-BRIDGE-TFT.md) describes the runtime changes carried into that branch.

### Published source and Pi deployment

The November 6 read-only audit found the echo-lookup fix `c8495f1` and RF receive-age changes `d274488` installed in the production Pi's venv. Its release source checkout was older than the installed package, so the checkout alone does not establish the running version.

The coordinated peer-activity changes are published but were not deployed at the audit: hub `662e7f4`, repeater `93a9161`, and UI `befc3c1`. The installed sidecar's `adapter.py` and `transport.py` matched the preceding hub revision `c75ce18`; the repeater API and served RF-age UI also preceded peer activity. Materializing current source does not deploy it or verify hardware behavior.

## Corrected parallel histories

The hub and TFT project branches recreate the project commits with corrected attribution in three commit subjects. Each recreated commit has the same tree, author, committer and timestamps as its original. Parent hashes change to follow the recreated history. The TFT history retains the exact upstream base `d664d89df66bdacc1319900a2e2d152fa31b9dc9` and all its ancestors. No original branch was force-pushed or deleted.

The mapping below covers the recreated commits. Later documentation commits exist only on the active branches and have no original counterpart. Full hashes, tree hashes and subjects are recorded in [the machine-readable mapping](branch-history.json). Original subjects in that record describe historical metadata, not the project's current attribution.

| Repository | Original commit | Corrected commit |
| --- | --- | --- |
| kesh-openhop-bridge | [`bc75a234`](https://github.com/kesh33w/kesh-openhop-bridge/commit/bc75a2343505923f0c2f6b4a9789b5bac83d9ff6) | [`54359c30`](https://github.com/kesh33w/kesh-openhop-bridge/commit/54359c3001a32eba7a2ba83144bb4a56060878ab) |
| kesh-openhop-bridge | [`dd4001c3`](https://github.com/kesh33w/kesh-openhop-bridge/commit/dd4001c39e7506b901a9c8cb4238c07c81e064c6) | [`fa9e0f85`](https://github.com/kesh33w/kesh-openhop-bridge/commit/fa9e0f85491fa6e37c7c3f5db9abad7beaea0036) |
| kesh-openhop-bridge | [`5ce2b328`](https://github.com/kesh33w/kesh-openhop-bridge/commit/5ce2b32829dbb56a1c7375c332848467a04d9065) | [`0c7100d8`](https://github.com/kesh33w/kesh-openhop-bridge/commit/0c7100d820c94d384f3817e82a868b932bb95f2e) |
| kesh-openhop-bridge | [`e086c124`](https://github.com/kesh33w/kesh-openhop-bridge/commit/e086c1246e1ec45e1d77fbe7face8ec8b6710bb5) | [`55e743d3`](https://github.com/kesh33w/kesh-openhop-bridge/commit/55e743d3515a96dd4f42eb8957afb77a68db61cb) |
| kesh-openhop-bridge | [`a01a069c`](https://github.com/kesh33w/kesh-openhop-bridge/commit/a01a069c97e2cb32e6778512bc137ac23170f3de) | [`db910446`](https://github.com/kesh33w/kesh-openhop-bridge/commit/db910446785b26e736a7f06eebb3e2b085a798ec) |
| MeshCore | [`92297eb1`](https://github.com/kesh33w/MeshCore/commit/92297eb12bdd683fe559d05be7ff297243d24dba) | [`5495fe33`](https://github.com/kesh33w/MeshCore/commit/5495fe335fe0a2652979d58d4d0b5fa3a09f9878) |
| MeshCore | [`85af7e59`](https://github.com/kesh33w/MeshCore/commit/85af7e596bde8e03b09e29f47cde40187972740e) | [`f0ca7773`](https://github.com/kesh33w/MeshCore/commit/f0ca77732019b8b125377f217c37351f0fe97457) |
| MeshCore | [`3aa270d9`](https://github.com/kesh33w/MeshCore/commit/3aa270d953251272bb3317c0be669e05c3bac380) | [`804e330e`](https://github.com/kesh33w/MeshCore/commit/804e330ef0fc1f33d248b5349b1cae0180f9d2d8) |
| MeshCore | [`cdc887bf`](https://github.com/kesh33w/MeshCore/commit/cdc887bff9cbeeb697e872e5464e3601dbb18a48) | [`ed03d939`](https://github.com/kesh33w/MeshCore/commit/ed03d939004821fdfcc76186ebcb9a1abfa90fa2) |

## Historical branch-creation aliases

At branch creation, these four forks needed no recreated history. Each new default branch started at the exact original commit below. These are historical alias records, not current branch heads. Repeater and UI have since advanced; their audited revisions are in the source snapshot above.

| Repository | Original branch | Active branch | Commit at alias creation |
| --- | --- | --- | --- |
| openhop_core | `sacmesh/private-link-20261105` | `kesh/bridge` | [`8c825c3e`](https://github.com/kesh33w/openhop_core/commit/8c825c3e1ab72bd65b567d2c75c704b7f17babeb) |
| openhop_repeater | `sacmesh/private-link-ui-20261105` | `kesh/bridge` | [`186341f6`](https://github.com/kesh33w/openhop_repeater/commit/186341f6c299091278fca2b87c21444e5d776f16) |
| openHop_RepeaterUI | `sacmesh/private-link-ui-20261105` | `kesh/bridge` | [`e6cbec32`](https://github.com/kesh33w/openHop_RepeaterUI/commit/e6cbec327da1386460c4419a7b3bf3bcba9ae633) |
| openhop_modem | `sacmesh/heltec-bench-20261105` | `kesh/heltec-bench` | [`f8d718be`](https://github.com/kesh33w/openhop_modem/commit/f8d718be2b7142f69ad3caec5e2fcab15fd0f898) |

## Switch an existing clone

Start with a clean working tree. Save unfinished work on a local branch or in a stash before switching. These commands create new local tracking branches; they do not move or delete your existing local branches or apply your saved changes.

In an existing hub clone, update the remote URL and fetch:

```sh
	git remote set-url origin https://github.com/kesh33w/kesh-openhop-bridge.git
	git fetch origin
	git remote set-head origin -a
	git switch --create kesh/openhop-bridge --track origin/kesh/openhop-bridge
```

In an existing MeshCore clone:

```sh
	git fetch origin
	git switch --create kesh/v43-tft-ilink --track origin/kesh/v43-tft-ilink
```

In each Core, Repeater or Repeater UI clone:

```sh
	git fetch origin
	git remote set-head origin -a
	git switch --create kesh/bridge --track origin/kesh/bridge
```

In the Modem clone:

```sh
	git fetch origin
	git remote set-head origin -a
	git switch --create kesh/heltec-bench --track origin/kesh/heltec-bench
```

If the local branch already exists, use `git switch` with its name instead of creating it again. Transfer any additional local commits individually after review. The hub's original and corrected histories have different root commits; merging the old branch wholesale would reintroduce the original history.

New hub clones use the new default automatically. New MeshCore clones still use `dev` unless a project branch is selected. Original commit IDs and release pins remain available for historical builds and rollback. Select an exact commit or release manifest rather than assuming a branch is frozen.
