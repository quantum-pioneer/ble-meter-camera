#pragma once
#include <stdint.h>
#include "project_config.h"

namespace meter {
constexpr uint32_t TEST_CUTOFF_UTC = METER_TEST_CUTOFF_UTC;
constexpr uint32_t TEST_RETENTION_SECONDS = 168U * 60 * 60;
constexpr uint32_t DEFAULT_TEST_RUN = METER_DEFAULT_TEST_RUN;
constexpr uint8_t MODE_TEST_PHOTO = 1, MODE_METER = 2;

// UTC is supplied only through the encrypted, allowlisted BLE control link.
// A reboot intentionally loses clock trust; a persisted floor rejects rollback.
struct BenchPolicy {
  uint32_t anchorUtc = 0, lastTrustedUtc = 0, run = DEFAULT_TEST_RUN;
  uint64_t anchorMs = 0;
  uint8_t mode = MODE_TEST_PHOTO;
  bool synchronized = false, configured = false, testExpired = false;
  uint32_t now(uint64_t uptime) const {
    if (!synchronized || uptime < anchorMs) return 0;
    uint64_t current = uint64_t(anchorUtc) + (uptime - anchorMs) / 1000;
    return current <= 0xffffffffULL ? uint32_t(current) : 0;
  }
  bool configure(uint8_t selectedMode, uint32_t utc, uint32_t cutoff,
                 uint32_t selectedRun, uint32_t retention, uint64_t uptime) {
    uint32_t current = now(uptime);
    if ((selectedMode != MODE_TEST_PHOTO && selectedMode != MODE_METER) || !selectedRun ||
        cutoff != TEST_CUTOFF_UTC || retention != TEST_RETENTION_SECONDS ||
        utc < 1792000000U || utc > 4102444800U ||
        (lastTrustedUtc && uint64_t(utc) + 5 < lastTrustedUtc) ||
        (current && uint64_t(utc) + 5 < current)) return false;
    if (current > utc) utc = current;
    if (lastTrustedUtc > utc) utc = lastTrustedUtc;
    anchorUtc = lastTrustedUtc = utc; anchorMs = uptime;
    mode = selectedMode; run = selectedRun; synchronized = configured = true;
    if (utc >= TEST_CUTOFF_UTC) testExpired = true;
    return true;
  }
  bool observe(uint64_t uptime) {
    uint32_t utc = now(uptime);
    if (utc > lastTrustedUtc) lastTrustedUtc = utc;
    bool newlyExpired = !testExpired && utc && utc >= TEST_CUTOFF_UTC;
    if (newlyExpired) testExpired = true;
    return newlyExpired;
  }
  bool allowed(uint64_t uptime) const {
    uint32_t utc = now(uptime);
    return configured && utc && (mode == MODE_METER ||
           (mode == MODE_TEST_PHOTO && !testExpired && utc < TEST_CUTOFF_UTC));
  }
  bool canServe(uint8_t recordMode, uint32_t recordRun, uint32_t expires,
                uint64_t uptime) const {
    uint32_t utc = now(uptime);
    return allowed(uptime) && mode == recordMode && run == recordRun &&
           (recordMode == MODE_METER || (expires && utc < expires));
  }
};
} // namespace meter
