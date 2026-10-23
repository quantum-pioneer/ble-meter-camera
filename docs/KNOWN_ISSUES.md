# Known issues and open questions

| Issue | Observed evidence | Status / next useful test |
|---|---|---|
| Original ESP32 cold power-on without advertising | Repeated cold controls silent; manual RST restores warm operation | Open. Measure power/reset/strap signals and capture early boot output while preserving a true cold-start control. No single cause proved. |
| Original running transfers eventually stall | Overnight 29/56 slot coverage; metadata, connection and policy-sync errors, same recorded boot | Open. Identify exact phase per attempt and correlate radio, SD and task timing. |
| READY deadline too short | Responsive BUSY reads exceeded old 10 s; later successes needed 13.9–14.5 s | Budget raised for tested snapshots. This resolves that particular deadline mismatch, not every timeout. |
| S3 SD maintenance overlaps scheduled capture | Receiver 0.5.12 maintenance occupied slot; scheduler advanced without a new image or failure increment | Confirmed code path and matching recorded event; unfixed in this snapshot. Track missed slots, reserve capture headroom or serialize/defer maintenance deliberately. |
| S3 maintenance/policy timeout | CONFIG/SD sync and later background cleanup timeouts | Open. Instrument phase durations and disconnect/worker completion. The underlying slow phase is not proved by a bare TimeoutError. |
| Authentication/configuration during S3 development | ATT error 5 before CAPTURE with initial SC-only configuration | Corrected final build retained encryption/SC/bond checks. Preserve security while testing changes. |
| S3 orientation | Upside-down then mirrored intermediate successes | Final tested vflip=1 / hmirror=0 was visually verified. Recheck against a real asymmetric target on other camera modules. |
| Proxy interruption and stale routes | Deliberate proxy restart interrupted an image; old image remained and a later new capture succeeded | Short recovery test passed. Endurance and repeated fault behavior remain open. |
| Historical bench cutoff | TEST_PHOTO stops at 2026-10-24 19:25 UTC | Intentional snapshot policy. New trials must explicitly configure matching firmware/receiver policy. |
| Installed meter performance | No complete installed-meter acceptance record | Open. Test actual placement, radio path, cold start, LCD pages, focus, glare and unattended schedule. |

For receiver 0.5.12, inspect `runtime.py` (`capture`, `_run_periodic`, `_monitor`) and `transfer.py` for maintenance ownership and the separate operation/context/READY budgets. A status of idle, `last_error=null`, or unchanged failure count does not by itself establish that every scheduled photo arrived. Compare persisted capture/receipt records against expected slots.

Do not report a confirmed crash without reset reason, boot counter or equivalent evidence. Keep connection failures, responsive BUSY/deadline expiry, an individual GATT timeout, SD work, and deliberate interruption separate.
