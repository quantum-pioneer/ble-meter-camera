# XIAO ESP32-S3 Sense / OV3660 tests

Status: **real JPEG delivery works, but unattended operation is not reliable yet.** A short commissioning checkpoint passed, then a later scheduled slot was missed during SD maintenance. These are separate outcomes, and the later result supersedes any earlier implication that the whole bench passed.

The physically tested board was a XIAO ESP32-S3 Sense with an **OV3660 sensor (PID 0x3660), 8 MiB flash and 8 MiB PSRAM**. The route was camera JPEG → encrypted Bluetooth LE → dedicated XIAO ESP32-C6 proxy → Home Assistant. The Home Assistant receiver component runs on Home Assistant; it is separate from the proxy firmware.

The final physically tested camera application was 815,424 bytes, SHA256 `7752cbc91df49ee47b7d8b213532759d71c11f2d6b74c7819aa117654459bfe1`, with `vflip=1`, `hmirror=0`. It was paired with Home Assistant component **0.5.12** and proxy firmware **ESPHome 2026.9.1**. The proxy was not reflashed during this camera migration. A public configuration edit changes the compiled application, so a configured public build must not claim to be that exact tested binary.

### What actually succeeded

The test subject was a calendar. These tests demonstrate image capture and delivery; they do not establish installed-meter reception or readable meter digits.

| Final application test | Capture → receipt UTC, 2026-10-23 | JPEG bytes | Result |
|---|---|---:|---|
| Independent cold power, USB disconnected, no reset or boot buttons | 21:36:21 → 21:38:55 | 77,506 | Complete 1600 × 1200 JPEG decoded, SD committed, checksum verified, stored in Home Assistant |
| Fresh photo after proxy power cycle, same independently powered camera boot | 21:49:04 → 21:51:38 | 76,832 | Complete decode, SD commitment, checksum and stored history/latest checks passed |
| Automatic quarter-hour slot | 22:00:26 → 22:03:08 | 75,516 | Complete decode, SD commitment, checksum and stored history/latest checks passed |

Those three receipts recorded zero chunk retries and zero metadata retries. The last verified orientation was upright with text reading left to right. Capture-to-receipt times and whole-attempt times are different measurements: the above images took roughly 154–162 seconds from capture timestamp to receipt; recorded whole attempts took roughly 178–198 seconds.

Later Home Assistant history and stored manifests show further automatic receipts:

| Scheduled slot UTC | Recorded capture → receipt UTC | JPEG bytes | Recorded outcome |
|---|---|---:|---|
| 22:15 | 22:15:48 → 22:18:23 | 76,471 | Stored JPEG present; SD committed; error 0; zero chunk/metadata retries |
| 22:30 | 22:30:28 → 22:34:06 | 115,686 | Stored JPEG present; SD committed; error 0; zero chunk/metadata retries |
| 22:45 | 22:45:44 → 22:49:27 | 118,219 | Stored JPEG present; SD committed; error 0; zero chunk/metadata retries |

These later records were checked as manifests and existing files without copying private photos. They are not a new independent full-decode acceptance run. The success/failure totals displayed by the integration include earlier camera hardware, older applications and development attempts; they are not a clean S3 reliability trial or success rate.

### Different failures encountered during the S3 port

- **BLE startup ownership:** a copied manual controller prestart conflicted with NimBLE owning startup in this Arduino/SDK configuration. It was removed before deployment. This correction was supported by source/link inspection, not by a claim that it explains later scheduled failures.
- **Insufficient authentication before capture:** the first actual S3 connection reported encrypted, bonded Secure Connections with a 16-byte key, but CONFIG failed with ATT error 5. No new photo was taken. The SDK reference ATT permission check showed that a global SC-only setting also demanded authenticated/MITM access, incompatible with the chosen Secure Connections Just Works pairing. The correction retained encrypted and authorized attributes plus connection, bond, SC and key-size validation. Just Works does not provide MITM protection. The source reference was not claimed to be an exact checkout of the precompiled SDK library.
- **Policy/SD synchronization timeout before capture:** following attempts timed out during clock/SD policy synchronization. Serial evidence showed CONFIG eventually accepted and persisted, with maintenance completed after the connection had become invalid. This supports investigation of deadline and worker cost; it does not prove an exact SD scan duration, a specific electrical fault or one cause for every timeout.
- **Image orientation:** an earlier successful image was upside down; an intermediate independently powered success was upright but mirrored. The final application changed sensor orientation to `vflip=1`, `hmirror=0`, and a new real image verified the result. Existing images were not edited to create this proof.
- **Transfer interrupted by proxy restart:** the requested physical proxy restart overlapped a 21:45 UTC photo transfer. That slot was not completed or counted as delivered. No partial JPEG was published, the previous verified photo remained displayed and the original remained on SD. Cleanup resumed collection automatically. A fresh full image after restart passed on the same camera boot.
- **SD maintenance collision and a missed scheduled slot:** the 23:00 UTC / 18:00 CDT slot produced no new image. Maintenance was active across the boundary and later reported a timeout. Details follow.

### Open issue: SD maintenance can consume a scheduled slot

The last delivered photo in this review was captured **2026-10-23 22:45:44 UTC (17:45:44 CDT)** and received **22:49:27 UTC (17:49:27 CDT)**. The next expected slot was **23:00 UTC (18:00 CDT)**.

Observed Home Assistant recorder data:

1. Before the boundary, collection and periodic capture were enabled, status was idle and the next slot was 18:00 CDT.
2. At 23:00:00.003 UTC, the latest image was unchanged, status remained idle, a retained BLE client was connected and the displayed next slot advanced to 18:15 CDT.
3. At 23:00:28.020 UTC, `sd_cleanup_error` became `TimeoutError: `. The client was disconnected. No fresh capture or receipt appeared. The photo-failure counter did not increment, and `last_error` remained empty.
4. The last-successful-maintenance timestamp stayed at 22:28:45 UTC. **That timestamp is not the timeout time.**

The frozen 0.5.12 receiver source explains how this silent skip is possible. Its monitor launches SD maintenance independently every 1,800 monitor ticks without checking available time before the next capture. At a scheduled boundary, the periodic loop calls `capture()`. That method rejects an existing maintenance task before creating a photo collection task. The periodic loop catches that error without recording the missed slot and computes the next quarter-hour. As no photo collection attempt began, the ordinary photo-failure counter can stay unchanged.

This source path is consistent with the observed maintenance connection and unchanged image at the missed boundary. It does **not** establish the exact lower-level cause or phase of the maintenance timeout. The data does not prove that the camera rebooted, that the SD card was erased, or that a JPEG transfer failed midway at 18:00; a new photo transfer was not recorded for that slot.

The public baseline preserves this behavior so contributors can reproduce and review it. A useful fix must reserve time for captures, defer maintenance safely, record every expected slot as delivered/deferred/missed, bound any catch-up attempt, and keep original retention, cutoff and SD commitment requirements. This remains an open issue until independently verified with the physical BLE route.

### Timing, SD behavior and remaining acceptance

The S3 receiver uses distinct budgets: CONFIG/context 120 seconds, READY 120 seconds, maintenance 170 seconds, each GATT operation 5 seconds and total collection 600 seconds. The camera has a 660-second hard session cap. An inner GATT timeout can occur before the larger outer deadline; increasing one limit does not identify or solve every timeout.

The S3 SD connection is SPI at 4 MHz: CS 21, SCK 7, MISO 8, MOSI 9. GPIO21 is also the board's user LED and is not driven as a status light. The old SD archive was reused without formatting. JPEGs commit and read back on SD before READY; the receiver validates length/checksum, full JPEG decode and durable local storage before acknowledging delivery. The Sense assembly has no equivalent original-camera white capture LED, so its capture-light setting is zero.

Live HEALTH file/pending counts can temporarily read 0/0 while an archive scan rebuilds its counters. That snapshot alone is not evidence of an empty or erased card. SD commitment, settled maintenance and persisted JPEG manifests are separate evidence.

The historical bench interval was 900 seconds. Its fixed cutoff was 2026-10-24 19:25 UTC, and owned test images used 168-hour retention from original capture, with an immutable fallback for legacy records lacking trusted capture UTC. A retry must not extend image age. The code contains a dated bench policy; it needs explicit local configuration and review before a new run. At this evidence checkpoint, future physical cutoff and seven-day expiry observations had not occurred.

Still unproved: sustained unattended reliability; recovery from all maintenance/connection failures; reception and optical readability at the actual meter; and future physical cutoff/expiry behavior. OCR and automatic meter-reading/billing output were not active. This is a partially working image transport project seeking reproducible tests and fixes.
