#include "BleMeterCamera/bench_policy.h"
#include "BleMeterCamera/archive_format.h"
#include <cassert>
#include <cstdio>
#include <cstring>
using namespace meter;

int main() {
  static_assert(TEST_CUTOFF_UTC == 1792869900U && TEST_RETENTION_SECONDS == 604800U);
  BenchPolicy p;
  assert(!p.allowed(1000)); // Cold boot without trusted UTC fails closed.
  assert(!p.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 10, TEST_CUTOFF_UTC + 1, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 1000));
  assert(!p.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 10, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, 3 * 86400, 1000));
  assert(!p.configure(MODE_TEST_PHOTO, 0, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 1000));
  assert(!p.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 10, TEST_CUTOFF_UTC, 0, TEST_RETENTION_SECONDS, 1000));
  assert(p.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 10, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 1000));
  assert(p.allowed(10999) && !p.allowed(11000)); // Exact cutoff, no grace period.
  assert(p.observe(11000) && p.testExpired);
  assert(!p.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 100, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 11000));
  // Persisted expiry survives restart, rollback and a fresh test run ID.
  BenchPolicy rebooted;
  rebooted.testExpired = p.testExpired; rebooted.lastTrustedUtc = p.lastTrustedUtc;
  assert(!rebooted.allowed(0));
  assert(rebooted.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN + 1, TEST_RETENTION_SECONDS, 500));
  assert(!rebooted.allowed(500));
  // A mode switch is explicit. It neither clears the test latch nor permits old test retry.
  assert(rebooted.configure(MODE_METER, TEST_CUTOFF_UTC + 1, TEST_CUTOFF_UTC, 22, TEST_RETENTION_SECONDS, 1500));
  assert(rebooted.allowed(1500) && rebooted.testExpired);
  assert(rebooted.canServe(MODE_METER, 22, 0, 1500));
  assert(!rebooted.canServe(MODE_TEST_PHOTO, DEFAULT_TEST_RUN, TEST_CUTOFF_UTC + TEST_RETENTION_SECONDS, 1500));
  assert(!rebooted.canServe(MODE_METER, 23, 0, 1500));
  // Trusted monotonic time spans the 32-bit millis rollover.
  BenchPolicy wrap;
  assert(wrap.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 20, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 0xfffffff0ULL));
  assert(wrap.now(0x1000003d8ULL) == TEST_CUTOFF_UTC - 19);
  assert(!wrap.allowed(0xfffffff0ULL - 1));
  assert(!wrap.configure(MODE_TEST_PHOTO, TEST_CUTOFF_UTC - 100, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 0x1000003d8ULL));
  // Legacy first-receipt fallback is explicit, persisted and unchanged by retry.
  uint8_t old[32] = {}; write32(old, LEGACY_ARCHIVE_MAGIC); write32(old + 4, 1);
  write32(old + 8, 77); write32(old + 12, 1000); write32(old + 16, 100);
  write32(old + 20, 4000); write32(old + 24, 0x12345678); write32(old + 28, crc32(old, 28));
  ArchiveRecord legacy; assert(legacy.decodeLegacy(old));
  assert(legacy.fallback && !legacy.capturedUtc && !legacy.retainedUtc);
  legacy.retainedUtc = TEST_CUTOFF_UTC - 1; legacy.run = DEFAULT_TEST_RUN;
  uint8_t migrated[48]; legacy.encode(migrated); ArchiveRecord resumed;
  assert(resumed.decode(migrated));
  assert(resumed.fallback && !resumed.capturedUtc && resumed.retainedUtc == legacy.retainedUtc);
  uint32_t expiry = resumed.expires();
  resumed.boot = 99; resumed.id = 101; // A retry may change transfer identity only.
  resumed.encode(migrated); assert(resumed.decode(migrated) && resumed.expires() == expiry);
  assert(!resumed.expiredUtc(expiry - 1) && resumed.expiredUtc(expiry));
  resumed.mode = MODE_METER; assert(!resumed.expires() && !resumed.expiredUtc(0xffffffffU));
  std::puts("PASS: fixed cutoff, fail-closed clock, persistent expiry, rollback, explicit mode/run, 168h and immutable legacy fallback");
}
