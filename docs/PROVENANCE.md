# Source provenance and publication changes

This repository preserves two historical Bluetooth camera checkpoints and their test record. The code is intentionally presented as partially working. No new radio or hardware test is implied by publishing it.

| Checkpoint | Tested camera application SHA-256 | Tested HA receiver |
|---|---|---|
| Original ESP32-CAM / OV2640, SD dedup | `ef4b5ccbf9bac71743437927ab963927356aac3149a154d537ceab76a6d4eb64` | 0.5.7, retained under `historical/receiver-0.5.7` |
| XIAO ESP32-S3 Sense / OV3660, final upright/unmirrored | `7752cbc91df49ee47b7d8b213532759d71c11f2d6b74c7819aa117654459bfe1` | 0.5.12, root `custom_components` |

These hashes identify the **privately tested historical applications**, not a binary distributed here. Firmware publication replaces installation-specific central-address/archive-owner configuration with required local inputs and removes private machine paths/recovery artifacts from build helpers. The public source must be rebuilt and revalidated. Host tests and source/build provenance do not replace physical acceptance.

Receiver implementation snapshots preserve the observed behavior, including the 0.5.12 maintenance/scheduler issue. Tests that contained real device addresses now use synthetic locally administered addresses. Operator prose is rewritten from project-specific records rather than publishing raw conversations, logs or household data. The proxy YAML is a redacted configuration template, not the exact private configuration or its build hash. No binary, flash dump, bond key, network credential, Home Assistant token, host address, meter serial, photograph, screenshot of the home system or unrelated configuration is included.

The test record distinguishes complete decoded/persisted JPEG checks, metadata-only observations, source inferences, intentionally interrupted trials and untested ideas. Aggregate lifetime counters are not used as an S3 reliability statistic. Full raw evidence is not public because it contains installation details and household imagery; contributors can reproduce the tests and submit sanitized measurements.

Upstream software keeps its own licensing. A minimal pinned Home Assistant test fixture is included under Apache-2.0 with its license and attribution in `tests/fixtures`; the repository's MIT license does not replace that license. Arduino, Espressif, NimBLE and ESPHome libraries are not bundled.
