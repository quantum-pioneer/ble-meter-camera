#include "BleMeterCamera/sd_archive.h"
#include <cassert>
#include <unistd.h>
using namespace meter;
namespace fs = std::filesystem;
static unsigned fileCount(const char *suffix) {
  unsigned count = 0;
  for (const auto &p : fs::recursive_directory_iterator(SD_MMC.root))
    if (p.is_regular_file() && String(p.path().string()).endsWith(suffix)) ++count;
  return count;
}
static void put(const fs::path &p, const uint8_t *bytes, unsigned n) {
  std::ofstream f(p, std::ios::binary); f.write(reinterpret_cast<const char *>(bytes), n);
}
int main() {
  char temporary[] = "/tmp/meter-sd-fixture-XXXXXX";
  assert(mkdtemp(temporary)); SD_MMC.root = temporary;
  const fs::path owned = SD_MMC.root / "ble-meter-camera-aabbccddeeff";
  const uint32_t captured = TEST_CUTOFF_UTC - 100;
  const uint8_t jpeg[] = {0xff, 0xd8, 1, 2, 3, 4, 0xff, 0xd9};
  SDArchive archive; archive.begin(77); assert(archive.ready);
  ArchiveRecord test, meter;
  assert(archive.store(jpeg, sizeof(jpeg), 1, 1000, crc32(jpeg, sizeof(jpeg)), captured, MODE_TEST_PHOTO, DEFAULT_TEST_RUN, test));
  assert(test.capturedUtc == captured && !test.fallback && test.expires() == captured + TEST_RETENTION_SECONDS);
  assert(archive.count == 1 && archive.pending == 1);
  BenchPolicy policy;
  assert(policy.configure(MODE_TEST_PHOTO, captured + 1, TEST_CUTOFF_UTC, DEFAULT_TEST_RUN, TEST_RETENTION_SECONDS, 1000));
  uint8_t *restored = nullptr; ArchiveRecord retried;
  assert(archive.restore(restored, retried, policy, 1000));
  assert(retried.sequence == test.sequence && retried.capturedUtc == test.capturedUtc && retried.expires() == test.expires());
  assert(!memcmp(restored, jpeg, sizeof(jpeg))); heap_caps_free(restored);
  SDArchive retryAfterBoot; retryAfterBoot.begin(88); restored = nullptr;
  assert(retryAfterBoot.restore(restored, retried, policy, 1000));
  assert(retried.boot == 77 && retried.captureMs == 1000 && retried.capturedUtc == captured && retried.expires() == test.expires());
  assert(!memcmp(restored, jpeg, sizeof(jpeg))); heap_caps_free(restored);
  archive.acknowledge(test.sequence); assert(archive.pending == 0 && fileCount(".jpg") == 1);
  assert(archive.store(jpeg, sizeof(jpeg), 2, 2000, crc32(jpeg, sizeof(jpeg)), captured, MODE_METER, 33, meter));
  assert(!meter.expires());
  // Unrelated content and deliberately incomplete REAL METER records survive all test cleanup.
  fs::path meterDir = owned / "meter-00000021";
  fs::path orphan = meterDir / "f000000ff-b0000004d-u0000000999.jpg";
  fs::path staging = meterDir / "f000000ff-b0000004d-u0000000999.jpg.part";
  put(orphan, jpeg, sizeof(jpeg)); put(staging, jpeg, sizeof(jpeg));
  put(SD_MMC.root / "unrelated.jpg", jpeg, sizeof(jpeg));
  put(owned / "keep.txt", jpeg, sizeof(jpeg));
  assert(archive.maintain(test.expires() - 1, DEFAULT_TEST_RUN));
  assert(fileCount(".jpg") == 4); // Test, meter, orphan meter, unrelated.
  assert(archive.maintain(test.expires(), DEFAULT_TEST_RUN));
  assert(fileCount(".jpg") == 3 && fs::exists(orphan) && fs::exists(staging));
  assert(fs::exists(SD_MMC.root / "unrelated.jpg") && fs::exists(owned / "keep.txt"));
  // BMC1 root is migrated exactly once with unknown capture UTC and receipt fallback.
  uint8_t legacy[32] = {}; write32(legacy, LEGACY_ARCHIVE_MAGIC); write32(legacy + 4, 100);
  write32(legacy + 8, 66); write32(legacy + 12, 3000); write32(legacy + 16, 99);
  write32(legacy + 20, sizeof(jpeg)); write32(legacy + 24, crc32(jpeg, sizeof(jpeg))); write32(legacy + 28, crc32(legacy, 28));
  fs::path base = owned / "f00000064-b00000042-u0000003000";
  put(base.string() + ".jpg", jpeg, sizeof(jpeg)); put(base.string() + ".meta", legacy, sizeof(legacy));
  put(base.string() + ".pending", reinterpret_cast<const uint8_t *>("pending\n"), 8);
  uint32_t firstReceipt = test.expires(); assert(archive.maintain(firstReceipt, DEFAULT_TEST_RUN));
  uint8_t encoded[48]; std::ifstream f(base.string() + ".time", std::ios::binary); f.read(reinterpret_cast<char *>(encoded), sizeof(encoded)); f.close();
  ArchiveRecord migrated; assert(migrated.decode(encoded) && migrated.fallback && !migrated.capturedUtc && migrated.retainedUtc == firstReceipt);
  SDArchive restarted; restarted.begin(88); assert(restarted.maintain(firstReceipt + 20, DEFAULT_TEST_RUN));
  std::ifstream f2(base.string() + ".time", std::ios::binary); f2.read(reinterpret_cast<char *>(encoded), sizeof(encoded)); f2.close();
  assert(migrated.decode(encoded) && migrated.retainedUtc == firstReceipt);
  assert(restarted.maintain(firstReceipt + TEST_RETENTION_SECONDS, DEFAULT_TEST_RUN));
  assert(!fs::exists(base.string() + ".jpg") && !fs::exists(base.string() + ".meta") && !fs::exists(base.string() + ".pending") && !fs::exists(base.string() + ".time"));
  // FAT rename rejects replacing existing metadata, unlike the previous mock.
  put(owned / "rename-source", jpeg, sizeof(jpeg)); put(owned / "rename-dest", jpeg, sizeof(jpeg));
  assert(!SD_MMC.rename("/ble-meter-camera-aabbccddeeff/rename-source", "/ble-meter-camera-aabbccddeeff/rename-dest"));
  assert(fs::exists(owned / "rename-source"));
  // Interrupted sidecar and failed old .meta.part migrations recover their
  // already durable first fallback instead of extending it to today's sync.
  for (unsigned kind = 0; kind < 3; ++kind) {
    uint32_t sequence = 110 + kind;
    write32(legacy + 4, sequence); write32(legacy + 28, crc32(legacy, 28));
    char filename[80]; snprintf(filename, sizeof(filename), "f%08x-b00000042-u0000003000", sequence);
    fs::path recoverBase = owned / filename;
    put(recoverBase.string() + ".jpg", jpeg, sizeof(jpeg)); put(recoverBase.string() + ".meta", legacy, sizeof(legacy));
    ArchiveRecord recovery; assert(recovery.decodeLegacy(legacy)); recovery.run = DEFAULT_TEST_RUN;
    recovery.retainedUtc = firstReceipt + 10;
    uint8_t fallback[48]; recovery.encode(fallback);
    put(recoverBase.string() + (kind == 0 ? ".time.part" : ".meta.part"), fallback, sizeof(fallback));
    if (kind == 2) { // A foreign capture's earlier staged time must never bind.
      recovery.id += 1; recovery.retainedUtc = firstReceipt - 500; recovery.encode(fallback);
      put(recoverBase.string() + ".time.part", fallback, sizeof(fallback));
    }
    SDArchive recoveryBoot; recoveryBoot.begin(100 + kind);
    assert(recoveryBoot.maintain(firstReceipt + 100, DEFAULT_TEST_RUN));
    std::ifstream adoptedTime(recoverBase.string() + ".time", std::ios::binary);
    adoptedTime.read(reinterpret_cast<char *>(encoded), sizeof(encoded)); adoptedTime.close();
    ArchiveRecord adopted; assert(adopted.decode(encoded));
    assert(adopted.retainedUtc == firstReceipt + 10 && adopted.sequence == sequence && adopted.id == 99);
    assert(fs::file_size(recoverBase.string() + ".meta") == 32);
    assert(!fs::exists(recoverBase.string() + ".time.part") && !fs::exists(recoverBase.string() + ".meta.part"));
    assert(recoveryBoot.maintain(firstReceipt + 10 + TEST_RETENTION_SECONDS - 1, DEFAULT_TEST_RUN));
    assert(fs::exists(recoverBase.string() + ".jpg"));
    assert(recoveryBoot.maintain(firstReceipt + 10 + TEST_RETENTION_SECONDS, DEFAULT_TEST_RUN));
    assert(!fs::exists(recoverBase.string() + ".jpg") && !fs::exists(recoverBase.string() + ".meta") && !fs::exists(recoverBase.string() + ".time"));
  }
  // Corrupt owned metadata remains intact and visible as unknown-expiry failure;
  // other known expired test files may still be pruned.
  fs::path corruptMeta = owned / "f00000100-b00000042-u0000003000.meta";
  legacy[0] ^= 1; put(corruptMeta, legacy, sizeof(legacy));
  assert(!restarted.maintain(firstReceipt + 20, DEFAULT_TEST_RUN));
  assert(restarted.lastError == SDArchive::VERIFY && fs::exists(corruptMeta));
  fs::remove(corruptMeta);
  assert(fs::exists(orphan) && fs::exists(staging));
  // Mandatory write failure returns no commit; readback corruption is not a deliverable.
  ArchiveRecord failed; mockWriteFailure = true;
  assert(!restarted.store(jpeg, sizeof(jpeg), 200, 4000, crc32(jpeg, sizeof(jpeg)), firstReceipt, MODE_TEST_PHOTO, DEFAULT_TEST_RUN, failed));
  mockWriteFailure = false; assert(!failed.sequence);
  mockReadCorruption = true;
  assert(!restarted.store(jpeg, sizeof(jpeg), 201, 5000, crc32(jpeg, sizeof(jpeg)), firstReceipt, MODE_TEST_PHOTO, DEFAULT_TEST_RUN, failed));
  assert(!failed.sequence);
  // Missing card fails closed, never formats, then remount/cleanup is allowed.
  mockMountFailure = true; SDArchive missing; missing.begin(99); assert(!missing.ready);
  assert(!missing.maintain(firstReceipt, DEFAULT_TEST_RUN));
  mockMountFailure = false; assert(missing.maintain(firstReceipt, DEFAULT_TEST_RUN));
  assert(fs::exists(orphan) && fs::exists(staging) && fs::exists(SD_MMC.root / "unrelated.jpg"));
  fs::remove_all(SD_MMC.root);
  std::puts("PASS: actual SDArchive 168h boundary, ACK retention, test-only deletion, meter/orphan/unrelated preservation, one-time legacy fallback, restart, mandatory write/readback failure, remount");
}
