# Camera firmware: partially working bench implementations

These are two separate hardware builds of the Bluetooth camera project. Both send JPEG data over an encrypted BLE connection to a dedicated ESPHome Bluetooth proxy; Home Assistant performs the receiver and scheduling work. Camera Wi-Fi is disabled. A running desktop process is not the relay or scheduler.

| Folder | Hardware | Historical tested revision | What the records establish |
| --- | --- | --- | --- |
| `esp32-cam` | Original ESP32-CAM / ESP32-D0WD-V3, AI-Thinker pin mapping, OV2640, 4 MiB flash and PSRAM | `ble-v3-sd-required-bench168h-bootdiag-sd-dedup` | Real 1600×1200 calendar JPEGs and two final consecutive scheduled warm-start deliveries. Repeated untouched cold starts failed to advertise; sustained delivery remained unreliable. |
| `xiao-esp32s3` | Seeed XIAO ESP32-S3 Sense, OV3660, 8 MiB flash and OPI PSRAM | `ble-v3-s3-ov3660-sd-spi-bench168h` | Final orientation correction, a complete independent cold-powered JPEG, proxy interruption recovery and one normal scheduled receipt. This does not establish long-term reliability. |

The preserved original ESP32 camera application was associated with SHA-256 `ef4b5ccbf9bac71743437927ab963927356aac3149a154d537ceab76a6d4eb64`. The final historical S3 application was associated with SHA-256 `7752cbc91df49ee47b7d8b213532759d71c11f2d6b74c7819aa117654459bfe1`. These hashes identify the historical tested applications, **not binaries built from this sanitized publication**. No tested binaries or private device dumps are distributed.

## Publication changes and required local settings

Hardware pin maps, protocol, image handling, SD write/readback checks, encryption, source allowlisting, timing, orientation and the existing expiry policy are retained. Publication changes move installation-specific values into a required, git-ignored `BleMeterCamera/local_config.h`, replace real identities in host tests with synthetic fixtures, and replace workstation-specific build/recovery paths with portable build-only scripts. These source and configuration changes mean a rebuilt application is not byte-identical to the historical tested binary; another installation still requires bench validation.

In the selected hardware folder, copy `BleMeterCamera/local_config.example.h` to `BleMeterCamera/local_config.h`. Fill every value and remove the template's deliberate `#error` only after doing so:

- `METER_ALLOWED_CENTRAL_MAC`: the dedicated proxy's **Bluetooth** connection identity, as verified for your installation. Its Wi-Fi MAC may differ. An absent configuration fails compilation; an unrecognized peer remains rejected.
- `METER_TEST_CUTOFF_UTC`: explicit Unix UTC cutoff, matching the Home Assistant receiver. The historical bench used `1792869900`, October 24, 2026 at 19:25:00 UTC. That is a historical test deadline and becomes stale; choose and document your own deadline.
- `METER_DEFAULT_TEST_RUN`: a nonzero run identifier matching the receiver.
- S3 only, `METER_ARCHIVE_DEVICE_ID_OVERRIDE`: the exact logical 48-bit owner ID from **your own existing** SD archive `OWNER.txt`. The tested S3 port migrated an existing original-camera archive. It deliberately refuses missing/mismatched ownership and does not silently create or adopt a different archive.

The original ESP32 build creates/checks its own efuse-derived archive namespace; that identity is generated on your device, not hardcoded here. The S3 build is an existing-archive migration implementation, not a fresh-card provisioning workflow. A new S3-only installation needs a separately reviewed provisioning procedure before its mandatory SD ownership check can succeed. Neither build formats the card or bypasses a failed SD commit.

Changing source configuration is not a way to reset a persisted expired-test latch. Existing NVS policy can retain clock floors, run state and expiry. Do not erase unrelated NVS or relabel someone else's archive to force acceptance. The receiver and firmware policies must be commissioned together.

## Build environment

Historical builds used Espressif Arduino core **3.3.10**, bundled ESP-IDF **5.5.4**, and that core's BLE implementation. The S3 build requires the pinned target's NimBLE configuration and OPI PSRAM. It is not a substitute for a separate third-party BLE library.

Install `esp32:esp32@3.3.10` with Arduino CLI and the official Espressif board package index. Then, from the selected hardware directory:

```sh
./build.sh
```

`ARDUINO_CLI`, optional `ARDUINO_CONFIG_FILE`, and `BUILD_PATH` can override tool/config/output locations. Both scripts require private local settings, verify the installed core version, compile only, and copy only the application to ignored `bin/`. They never open a serial device or flash firmware.

Original target flags:

```text
esp32:esp32:esp32cam:FlashMode=dio,FlashFreq=40,PartitionScheme=min_spiffs
```

Its supplied `partitions.csv` is the pinned core's 4 MiB `min_spiffs` layout, with app slots at `0x10000` and `0x1f0000`, each `0x1e0000` bytes.

S3 target flags:

```text
esp32:esp32:XIAO_ESP32S3:CPUFreq=240,FlashMode=qio,FlashSize=8M,PSRAM=opi,PartitionScheme=default_8MB,USBMode=hwcdc,CDCOnBoot=default
build.partitions=s3_preserved
upload.maximum_size=1835008
```

The S3 `partitions.csv` intentionally preserves the historical board's custom layout: app0 `0x10000`, app1 `0x1d0000`, each `0x1c0000`; NVS begins at `0x390000`. `check_partition.py` verifies the source table, generated table and application fit. It does not establish what is installed on another board. Review that board's own backup, partition table and active OTA slot before choosing an application programming address. No blanket flash command is supplied because default-layout/merged flashing can overwrite existing board data.

## Standalone host checks

A C++17 `g++` compiler and Python 3 are sufficient:

```sh
python3 esp32-cam/run_host_tests.py
python3 xiao-esp32s3/run_host_tests.py
```

The four original and six S3 C++ suites use temporary host executables and synthetic settings, without a private `local_config.h`, network or hardware. They cover protocol/CRC, expiry and rollback policy, boot diagnostics and SD archive behavior; S3 also checks archive identity and security-state rejection. These are software fixtures, not camera, on-air encryption or reliability tests.

Publication validation: all four original and six S3 host suites passed. Both sanitized firmware targets also compiled successfully with the pinned installed core in an isolated copy using synthetic proxy/archive settings and the historical cutoff as a compilation fixture. The resulting application files were 1,385,632 bytes (original) and 815,424 bytes (S3); the S3 generated partition table/fit check passed. These builds were not flashed or tested on hardware, and their binaries are not distributed. Both public build scripts also rejected missing private configuration before invoking the compiler.

## Behavior and remaining limits

A requested photograph must commit to microSD and pass readback checks before READY. The receiver reads bounded 128-byte JPEG chunks, validates identity/offset/CRC and JPEG decoding, saves a durable copy, then acknowledges delivery. Pending SD recovery retains original capture time and expiry. Test copies retain the existing 168-hour policy; reconnect/retry does not extend expiry. Full storage/expiry behavior still needs validation on each deployment.

The original build retains optional bounded illumination; the S3 Sense expansion has no equivalent white capture LED and rejects nonzero capture-light duty. The S3 OV3660 orientation remains `vflip=1`, `hmirror=0`, with sensor PID checks and fail-closed security checks. Do not apply original ESP32 pin assumptions to the S3.

Successful bench pictures show the transport can work. Automatic original-camera cold start, long-running delivery, installed-meter readability, glare and display-cycle handling remain unresolved. OCR, numeric meter readings and billing processing are not implemented by these firmware builds.
