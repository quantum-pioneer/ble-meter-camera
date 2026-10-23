# BLE JPEG wire v2 with mandatory SD and bench-context extensions

The current camera policy requires CONFIG, MANIFEST, HEALTH and a verified SD
commit before JPEG delivery. The original DATA framing remains wire v2; the
camera revision prefix `ble-v3` refers to these policy/storage extensions.
Capture uptime below is a relative counter; synchronized original UTC and
expiry are carried separately in MANIFEST. See the current extensions below.

Service: `9a8e0001-55d5-4c52-a75b-9dfe0e5f5a01`, name `MeterCameraBLE`.
Control/metadata/data UUIDs replace `0001` with `0002`/`0003`/`0004` respectively.
Control is encrypted write-with-response; metadata/data are encrypted reads.
Bond/pair before GATT access. Metadata/control remain at most 20 bytes. Version 2 DATA is at most 136 bytes
with 128 payload bytes. The camera offers ATT MTU 247; verify the actual
negotiated MTU from the camera log and the receiver public `mtu_size`. There are no
notifications. No Bluetooth Classic/SPP, camera Wi-Fi or USB image transport.

All integers are little endian. Metadata is exactly `<BBHIIII>`: version 2,
state, error, requested transfer ID, total bytes, CRC32 and capture uptime ms.
States: 0 idle, 1 busy, 2 ready, 3 error. CRC32 matches Python `zlib.crc32`.
Capture uptime is unsynchronized and wraps every 49.7 days. On SD retry from a
different boot it is `0xffffffff` unknown, never a substitute receipt timestamp.

| Opcode | Layout | Meaning |
|---|---|---|
| 1 CAPTURE | `<BIBB>` or `<BIBBB>` opcode, nonzero ID, frame code, quality, optional raw light duty | Capture one real JPEG; default light off, optional duty0–64/255. |
| 2 SELECT | `<BII>` opcode, ID, offset | Select requested byte offset for next data read. |
| 3 RELEASE | `<BI>` opcode, ID | Free RAM and disconnect; ID 0 cancels any work. Preserves undelivered SD capture. |
| 4 TEST | `<BIBB>` opcode, ID, ignored frame/quality | Produce `bytes(range(256))`, CRC32 `0x29058c73`; never archived. |
| 5 ACK | `<BI>` opcode, ID | After CRC, JPEG decode and durable HA publish, mark the committed current SD archive delivered. It retains the original JPEG until expiry. |
| 6 RETRY | `<BI>` opcode, new nonzero ID | Serve newest complete pending SD photograph under this new transfer ID. Error 10 if no pending file. |

Data is `<II>` requested transfer ID, selected offset, then 1–128 image bytes.
Reads do not advance offset. SELECT/reads can be repeated idempotently. Validate
ID, exact offset, length, total CRC32 and decoded JPEG before publication.
Invalid commands never replace a ready image's checksum, ID or capture time.

The optional eighth CAPTURE byte bounds white LED duty to64/255 (about25%).
An independent timer turns it off within a two-second scheduled pulse even if
frame acquisition stalls; cleanup switches it off before storage or upload.
The seven-byte command is unchanged and keeps the LED off. TEST stays exactly
seven bytes and RETRY exactly five; neither uses illumination. Values above64
return unsupported error8. Fresh-frame settling and exposure diagnostics do not
establish that the installed meter digits are readable.

ACK work runs on the main loop, outside Bluetooth callbacks. It removes only
the pending marker; ACK does not delete the archived JPEG. RELEASE and link loss
may follow immediately; queued ACK retains its archive sequence independently.
A lost/failed ACK can leave a duplicate pending retry, but cannot acknowledge an
unverified image. The receiver must avoid overlapping transfers and never ACK
on cancellation, checksum/decode/storage failure, or TEST.

Frame codes: 1=QQVGA 160×120, 5=QVGA 320×240, 8=VGA 640×480, 9=SVGA 800×600,
10=XGA 1024×768, 12=SXGA 1280×1024, 13=UXGA 1600×1200. Quality 10–40, lower
means more detail/larger bytes. Maximum JPEG 128 KiB. Start modest; negotiated
throughput and the 600s receiver deadline can reject a high-quality capture even
when it fits camera RAM. Higher settings remain available for later testing.

Errors: 1 bad command, 2 busy/not ready, 3 camera/saved-image failure, 4 oversize,
5 expired, 6 wrong ID, 7 bad offset, 8 unsupported, 9 rate limit, 10 no pending SD.
CAPTURE/TEST/RETRY starts are rate-limited to at least 10s across reconnects.
Another command needs idle/error, or RELEASE of the current ready image.
Only one central is accepted; advertising stops during a session.

Disconnect, RELEASE, 45s without valid activity, or 660s total connection lifetime
releases volatile bytes. Polling cannot extend the hard limit. The receiver uses
a shorter 600s total deadline and must RELEASE/disconnect in cleanup. Interrupted
RAM transfers can resume only while the same connection/frame remains active;
SD RETRY is explicit and starts at offset 0 in a new session.


Wire v1 used version byte 1, 12 payload bytes and 20-byte data values. A dual
receiver chooses the exact data bound from the metadata version, never from
observed short packets. Wire v2 uses version byte 2 with the same `<BBHIIII>`
metadata layout and no added reserved field. Legacy commands, error codes,
CRC and SD record formats are unchanged; CAPTURE additionally accepts the
optional eighth light-duty byte described above. The receiver refuses v2 SELECT/DATA
when its actual negotiated public MTU is below 137, then RELEASEs without
publishing. Expected MTU with this camera and C6 is 247; actual negotiation
must be recorded. The 256-byte TEST fixture is exactly two v2 payload chunks.

The transfer still captures one requested still, with no local burst. The user
reports both screen and digit blinking/changing; a future bounded local burst
needs the actual meter cycle measurement. No OCR or billing work is included.

## Required SD/clock context extension (ble-v3-sd-required-bench168h)

Existing wire-v2 metadata and DATA layout remain unchanged. Four-byte fields
below are unsigned little endian; these characteristics require encrypted
reads through the same allowlisted C6. They are exactly 20 bytes.

Before CAPTURE, TEST fixture or RETRY on **every** new connection, write CONFIG
opcode `7` to CONTROL as `<BBHIIII>`:

| Field | Value |
|---|---|
| opcode, mode, reserved | `7`, `1=TEST_PHOTO / 2=METER`, `0` |
| trusted UTC, immutable cutoff | current epoch seconds, `1792869900` |
| run ID, retention seconds | `20261021` for this trial, `604800` |

CONFIG queues main-loop NVS persistence and owned SD migration/cleanup. Poll
MANIFEST until CLOCK_SYNC is set, allowing ten seconds. A mounted SD alone is
insufficient; context/cleanup must succeed. The test cutoff cannot be extended
and is latched expired in NVS. Missing/untrusted UTC fails closed after boot.
CONFIG remains permitted after expiry for maintenance; it cannot authorize
expired captures/fixtures/retries/data. RELEASE with ID zero still disconnects.

MANIFEST characteristic `9a8e0005-55d5-4c52-a75b-9dfe0e5f5a01` is `<BBHIIII>`:
version `1`, flags byte, SD error uint16, capture UTC, expiry UTC, run ID,
archive sequence. Idle context returns zeros for capture/expiry/sequence and
its selected run. READY JPEGs require SD_COMMITTED, matching mode/run, valid
UTC/fallback and unexpired timestamps before DATA and before publication.

| Flag bit | Meaning |
|---|---|
| 1 | SD_READY |
| 2 | SD_COMMITTED with readback verified, current READY JPEG |
| 4 | CLOCK_SYNC and durable context/cleanup ready |
| 8 | TEST_EXPIRED, scoped to TEST PHOTO mode |
| 16 / 32 | TEST_PHOTO / METER mode |
| 64 | FROM_PENDING |
| 128 | TIME_FALLBACK (capture UTC zero; expiry is first trusted receipt +168h) |

A fresh real JPEG carries its sensor-frame capture UTC; TEST expiry is
capture+604800. A restored record preserves it. METER expiry is zero and test
cleanup never deletes METER records. The 256-byte transport fixture does not
claim SD_COMMITTED and is never a photograph. Camera error `11` means SD_FAILED.

SD error numbers: `0=OK,1=mount,2=ownership,3=context,4=capacity,5=card full,
6=write,7=readback/CRC,8=legacy migration,9=clock,10=purge`.

HEALTH characteristic `9a8e0006-55d5-4c52-a75b-9dfe0e5f5a01` is `<BBHIIII>`:
version `1`, ESP reset reason byte, SD error uint16, boot nonce, uptime ms,
SD JPEG count and pending count. A changed nonce and low uptime identify a new
boot while independently powered. Reset reason values are Espressif
`esp_reset_reason_t` (`1=power-on,3=software,7=watchdog,9=brownout`, etc.); the
actual SDK enumeration is authoritative. This reports firmware observations,
not supply voltage or proof of cable quality.

## Persistent boot diagnostic extension (ble-v3-sd-required-bench168h-bootdiag)

BOOT_DIAGNOSTIC `9a8e0007-55d5-4c52-a75b-9dfe0e5f5a01` is an encrypted,
read-only 20-byte `<BBHIIII>` value: version `1`, **previous** stage byte,
flags uint16, current boot counter, previous boot counter, previous ESP reset
reason, previous raw GPIO strap register. It is available before CONFIG succeeds,
so a missing SD/context can still be diagnosed. Current reset/uptime remain in
HEALTH. The prior snapshot is fixed for the current boot, including after new
stage commits. It never represents the current boot's stage.

| Stage | Name / checkpoint immediately before |
|---|---|
| 0 | UNKNOWN: no valid previous record |
| 1 | APP_ENTERED: first diagnostic commit, before Serial initialization |
| 2 | STATE_INIT: Wi-Fi stop/deinit and mutex/queue initialization |
| 3 | CAMERA_INIT: PSRAM check, sensor PWDN/settle and camera driver |
| 4 | POLICY_NVS: existing clock/retention policy namespace |
| 5 | SD_INIT: existing archive mount and scan |
| 6 | FLASH_INIT: existing bounded flash timer/PWM initialization |
| 7 | BLE_INIT: BLE controller and host initialization |
| 8 | BLE_SERVICE: server, encrypted characteristics and advertising setup |
| 9 | ADVERTISING_REQUESTED: initial main-loop advertising request |
| 10 | ADVERTISING_ACTIVE: successful GAP completion observed, committed once from main loop |
| 11 | ADVERTISING_EXHAUSTED: no initial successful completion, all three attempts exhausted and two seconds elapsed after the last attempt |

A delayed successful completion may replace stage 11 with stage 10. Stage 10
is written only once; subsequent ordinary connections/disconnections do not
wear NVS by updating diagnostic stages.

Flags are `1=NVS_READY`, `2=PRIOR_VALID`, `4=COUNTER_OVERFLOW`, `8=WRITE_ERROR`;
other bits are reserved zero. NVS_READY means namespace open succeeded, not
that every write succeeded. WRITE_ERROR is sticky for the current boot, including
namespace-open failure. Without NVS readiness current counter is zero; without
a valid prior record all prior fields/stage are zero and the first tentative
current counter is one. Valid prior counters increment once per app startup;
prior UINT32_MAX produces current UINT32_MAX with COUNTER_OVERFLOW, never wrap.
An available but malformed/CRC-invalid prior record has PRIOR_VALID clear; it
does not manufacture a prior phase or counter. A counter can be tentative if
WRITE_ERROR is set.

Persistence uses only key `state` in separate namespace `meter_boot_v1`: one
24-byte blob via `Preferences.putBytes`/NVS commit, not a set of independently
updated fields. Stored little-endian layout is magic `0x31444d42`, version byte
`1`, stage byte, flags uint16 (only bit0=saturated), counter, reset reason, strap,
then CRC32 over bytes 0–19. Decode requires the exact length/version, stage 1–11,
nonzero counter, valid flags and CRC. Only setup/main-loop code accesses this
namespace; BLE reads a mutex-protected published wire copy.

These checkpoints are diagnostic evidence, **not a cold-start repair**. A prior
phase means a later checkpoint did not persist; an unchanged counter does not
prove ROM failure, because early NVS open/write can fail too. The first upgraded
boot lacks a prior baseline. A valid ADVERTISING_ACTIVE baseline should be
recorded before controlled cold-power comparisons. Extra early NVS commits
change startup timing, and HEALTH POWERON after a manual EN/RST operation is
not independent cold-start proof. No reboot loop, eFuse change, brownout change,
capture/clock/retention change or SD archive identity change is introduced.

Background maintenance uses CONFIG, MANIFEST/HEALTH and RELEASE only. It can
continue after collection/test stop and after METER selection. No JPEG bytes
are stored on the C6. The camera's next-expiry cleanup also runs without a BLE
session while monotonic clock trust is valid.
