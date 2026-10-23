# Bluetooth Meter Camera — work in progress

Photograph an electricity meter where the meter is too far away for a dependable Wi-Fi connection. The camera sends the JPEG over Bluetooth to a nearby proxy, and the proxy carries it over its network connection to Home Assistant.

**This has delivered real photographs. It does not yet run reliably enough for unattended meter monitoring.** Both the original ESP32-CAM and the XIAO ESP32-S3 Sense have been tested. The failures differ, and a missed photo does not automatically mean that the ESP32 crashed.

```mermaid
flowchart LR
    A[ESP32 camera and microSD] -->|Encrypted BLE GATT / JPEG chunks| B[XIAO ESP32-C6 active Bluetooth proxy]
    B -->|Wi-Fi / encrypted ESPHome API| C[Home Assistant custom integration]
    C --> D[Validated local photographs and status]
```

The camera does not need Wi-Fi for this route. The proxy needs a working network connection and must also be within Bluetooth reach of the camera. The meter's Wi-Fi distance is the reason for this design; an installed-meter range or readability test has **not** yet demonstrated the complete solution. Recorded image tests used indoor subjects, including a calendar.

## What works, and what still fails

| Hardware / tested software | What actually worked | Remaining or observed failure |
|---|---|---|
| Original ESP32-CAM / OV2640, final SD-dedup firmware and HA receiver 0.5.7 | Complete 1600×1200 JPEGs, SD storage, Bluetooth proxy delivery, dashboard display; two final warm scheduled captures succeeded | Cold power-on could produce no fresh advertisements until manual reset. An earlier overnight run delivered only 29 of 56 expected slots. Sustained operation remains unproved. |
| XIAO ESP32-S3 Sense / OV3660, final firmware and HA receiver 0.5.12 | Independent cold-start photo, recovery after a deliberate proxy restart, and automatic scheduled photos; complete decoded JPEGs | Later SD maintenance timed out and occupied the receiver at a scheduled boundary. The 18:00 photo was skipped without increasing the photo-failure counter. Long-run operation remains incomplete. |

Earlier issues included connection failures, metadata timeouts, an outer READY deadline that was too short, S3 authentication/configuration errors, and image orientation. These are documented separately in the [test history](docs/TEST_HISTORY.md) and [known issues](docs/KNOWN_ISSUES.md). No universal hardware or Bluetooth root cause has been established.

## Source in this repository

- `firmware/esp32-cam/`: original OV2640 camera firmware.
- `firmware/xiao-esp32s3/`: XIAO ESP32-S3 Sense / OV3660 firmware.
- `custom_components/ble_meter_camera/`: HA receiver **0.5.12**, including the known maintenance/scheduling defect.
- `historical/receiver-0.5.7/`: receiver paired with the last successful original-camera tests.
- `esphome/`: dedicated XIAO ESP32-C6 proxy configuration template.
- `tests/`: existing simulated receiver tests; these do not prove radio or hardware reliability.

These are snapshots of partially functioning code, with private firmware configuration removed. Public firmware must be configured and rebuilt locally; it is **not byte-for-byte the tested binary**. No device flash dumps, credentials, household photographs, original logs, meter identifiers or personal information are included. See [source provenance](docs/PROVENANCE.md).

**Historical bench policy:** captures every 900 seconds, seven-day test-photo retention, and a fixed stop at **2026-10-24 19:25 UTC**. The receiver retains that historical cutoff. After it passes, this snapshot stops TEST_PHOTO collection; it is not a rolling three-day timer. A new trial requires a deliberate, consistent policy change in both firmware and receiver. Do not interpret the stopped historical trial as a new radio failure.

Read [setup and limitations](docs/SETUP.md), the [firmware guide](firmware/README.md), and [protocol](docs/protocol.md) before attempting a bench build. No production OCR, usage calculations, CSV export or billing pipeline has been validated.

**S3 setup limitation:** the tested S3 port reuses an existing owned SD archive from the original camera. It refuses a fresh or differently owned card. Fresh-card provisioning for a new S3-only installation is not implemented.

## Help get it reliable

Use [Issues](https://github.com/quantum-pioneer/ble-meter-camera/issues) to share a test result, report a problem, or offer a tip. Pull requests are welcome. Please include the board, firmware/receiver version, exact failure phase, power/reset conditions, proxy behavior, and whether a complete new JPEG arrived. Redact identifying details before attaching anything.

Useful starting points are cold-start diagnosis on the original ESP32, SD scan/maintenance duration on the S3, scheduling while maintenance is busy, accurate missed-slot accounting, proxy reconnection, and repeatable installed-meter tests. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [roadmap](docs/ROADMAP.md).
