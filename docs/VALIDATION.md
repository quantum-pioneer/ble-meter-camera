# Publication validation — October 23, 2026

Checks on the sanitized repository:

| Check | Result | Limit |
|---|---|---|
| Receiver 0.5.12 simulated suite | 180 tests passed | Fake transport and HA scaffolding; no radios contacted |
| Historical receiver 0.5.7 simulated suite | 144 tests passed | Separate process from the current suite; no hardware |
| Firmware C++ host suites | Four original + six S3 suites passed | Host stubs and synthetic settings |
| Original firmware build, Arduino ESP32 3.3.10 | Passed; application 1,385,632 B | Temporary synthetic local configuration; no flash |
| S3 firmware build, Arduino ESP32 3.3.10 | Passed; application 815,424 B | Temporary synthetic local configuration; no flash |
| S3 generated partition layout and application fit | Passed | Does not identify another board's actual installed layout |
| Missing firmware local configuration | Both build scripts reject it | Deliberately requires private local settings |

An omitted historical native test reference was replaced by a minimal attributed fixture containing the relevant unchanged Home Assistant 2026.10.2 methods. Its own license is included. The camera-reconfiguration module also passed independently (11 tests, included within the 180-test suite).

Publication checks do not fix or reproduce every historical failure. In particular, receiver 0.5.12 still has the documented maintenance/scheduler collision. No hardware was flashed, no live Home Assistant settings were changed, and no new physical reliability claim is made by these software checks. Physical results and their limits are recorded in the separate test history.
