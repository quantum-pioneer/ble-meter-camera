# Recorded tests and interpretation

This is a redacted summary of saved bench reports, transfer metadata, serial diagnostics, full-JPEG checks and source reviews through October 23, 2026. Times below are UTC unless explicitly marked Chicago/CDT. Raw household records and images remain private. These are historical observations, not new hardware tests performed for this publication.

## Original ESP32-CAM / OV2640

The original board used the AI-Thinker ESP32-CAM pin mapping, original ESP32 silicon, 4 MiB flash, PSRAM and microSD. It sent encrypted GATT traffic through a dedicated XIAO ESP32-C6 proxy to the custom HA receiver.

| Checkpoint | Observation | What it establishes |
|---|---|---|
| October 20 transport window, 19:14–20:31 Chicago | 22 real-JPEG attempts: 15 succeeded, 7 did not. Three separate fixtures succeeded. The normal v1 baseline was 10/11; three intentional interruption overlaps failed. | Partial operation across mixed revisions and deliberate faults. These counts are not a final-version reliability rate. |
| Initial v1 | 12 JPEG payload bytes per ATT read; successful 320×240 images took roughly 55–90 seconds. | Small real images could traverse the route. |
| v2, 128-byte payload / negotiated MTU 247 | Successful 1600×1200 JPEGs; a measured 53–54 kB configuration took about 90–93 seconds. | Higher resolution transfer worked. Resolution alone does not establish legible meter digits. |
| October 20 repair checks | SD recovery image 78,394 B / 156.089 s; 15% capture-light repeat 105,656 B / 177.688 s; scheduled image 117,422 B / 191.158 s. Zero reported retries in these checks. | Short successful bench sequence following repairs, not sustained acceptance. |
| October 21 independent power after manual RST | Two scheduled calendar images fully decoded at 1600×1200: 80,043 and 80,699 B, roughly 141 and 143 s. A third slot had metadata confirmation. | Reset-assisted independent operation; not untouched cold-start success. Large calendar numerals were readable; fine print remained soft. |
| October 21 19:00–October 22 08:45 Chicago, receiver 0.5.5 | 56 expected quarter-hour slots, 29 receipts, 41 transfer attempts (29 success / 12 failure), and 15 additional slots skipped under retry backoff. Last capture 02:30 / receipt 02:32:48 Chicago. | 29/56 slot coverage. Using 29/41 alone hides the skipped schedule slots. |
| Overnight error split | Eight metadata-read timeouts, three connection failures, one clock/SD-policy synchronization timeout. Camera remained advertising with the same boot in available health records; eight SD photos were pending. | Running transfer failures are distinct from cold-start silence. No reset or power-loss cause was proved for this run. |
| October 22 17:16 Chicago, receiver 0.5.6 | CAPTURE write 102.285 ms; 32 BUSY metadata reads returned in 48.868–184.280 ms (median 56.553 ms). Outer READY wait expired at 10,000.975 ms. | That attempt exceeded a 10-second readiness deadline; individual five-second reads were responding. |
| Reset-only and proxy antenna controls | Captures still hit the old 10-second READY limit. Reported median BLE RSSI changed from about −90 to −77 dBm around combined reset/proxy changes. | Reset/antenna alone did not restore those attempts; interventions confound attribution of signal improvement. |
| Final original SD-dedup firmware + receiver 0.5.7 | October 22 17:45 and 18:00 Chicago scheduled captures succeeded, 77,562 / 82,949 B, full 1600×1200 decode, zero chunk/metadata retries. READY took 13.927 / 14.542 s with a 60-second budget. | The earlier 10-second budget was too short for these successful captures. Two passes after combined changes do not prove sustained reliability. |

### Separate cold-start problem

Fresh power-on repeatedly produced no fresh camera advertisements. Comparisons included SD inserted/removed, independent power, computer USB, an alternate cable, a diagnostic build and the final SD-dedup build. Other nearby devices were still visible to scanners. Manual RST repeatedly restored advertising and warm capture.

After a silent cold attempt, the durable boot diagnostic still showed the previous recorded boot; the count advanced after RST. This is evidence that no new durable application-start marker was recorded. It does not establish exactly where startup stopped. There were no rail/ramp measurements proving a supply fault, strap fault, PSRAM failure or ROM-only execution. Prepared no-PSRAM and sleep-disabled probes were not flashed and are not tested fixes.

### Other development findings

SD mount/format problems, stale GATT cache, advertising recovery, capture exposure/old sensor frames, light PWM initialization, and a FatFs rename migration defect were encountered. Capture-light/exposure work improved dark but otherwise valid indoor JPEGs. The SD dedup change reused a record already read during scanning. None of these observations proves one explanation for all failures.

JPEGs are written/read back on SD before READY; Home Assistant validates and persists the full image before acknowledgment. Interrupted transfers keep the previous verified HA image, and retries retain the original capture/expiry basis.

## XIAO ESP32-S3 Sense / OV3660

See [S3 test record](S3_TEST_RECORD.md), including the later missed 18:00 slot. Final successful S3 results must not be mistaken for a completed endurance test.

## Limits shared by both boards

Tests used indoor bench subjects, including a calendar. The actual meter position, end-to-end range, outdoor exposure, display cycling, focus and all required digits remain unvalidated. The configured future cutoff and seven-day expiry had not yet been observed physically at the time of these reports. Simulation checks are separate from hardware results. Lifetime dashboard totals span boards, builds and deliberate interruptions and cannot be used as a per-board success rate.
