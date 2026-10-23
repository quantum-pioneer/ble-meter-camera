#include "BleMeterCamera/protocol.h"
#include "BleMeterCamera/archive_format.h"
#include "BleMeterCamera/advertising_recovery.h"
#include <cassert>
#include <cstdio>
#include <cstring>

int main() {
  using namespace meter;
  const auto *standard = reinterpret_cast<const uint8_t *>("123456789");
  assert(crc32(standard, 9) == 0xcbf43926U);
  assert(crc32(standard, 0) == 0);
  uint8_t test[256];
  for (unsigned i = 0; i < sizeof(test); ++i) test[i] = uint8_t(i);
  assert(crc32(test, sizeof(test)) == 0x29058c73U);
  Metadata meta;
  meta.state = READY; meta.error = NONE; meta.id = 0x12345678;
  meta.length = 256; meta.crc = 0x29058c73; meta.captureMs = 0xffffffffU;
  uint8_t packet[20]; meta.encode(packet);
  const uint8_t expected[20] = {
    2, 2, 0, 0, 0x78, 0x56, 0x34, 0x12, 0, 1, 0, 0,
    0x73, 0x8c, 0x05, 0x29, 0xff, 0xff, 0xff, 0xff
  };
  assert(memcmp(packet, expected, 20) == 0);
  assert(read32(packet + 4) == meta.id);
  assert(read32(packet + 16) == 0xffffffffU);
  static_assert(VERSION == 2 && CHUNK_BYTES == 128 && DATA_VALUE_BYTES == 136);
  static_assert(LOCAL_ATT_MTU == 247 && DATA_VALUE_BYTES < LOCAL_ATT_MTU - 1);
  uint8_t chunk[DATA_VALUE_BYTES], assembled[sizeof(test)] = {};
  const uint8_t expectedHeader[8] = {0x78, 0x56, 0x34, 0x12, 0, 0, 0, 0};
  assert(encodeDataPacket(chunk, meta.id, 0, test, sizeof(test)) == 136);
  assert(memcmp(chunk, expectedHeader, 8) == 0 && memcmp(chunk + 8, test, 128) == 0);
  memcpy(assembled, chunk + 8, 128);
  uint8_t repeated[DATA_VALUE_BYTES];
  assert(encodeDataPacket(repeated, meta.id, 0, test, sizeof(test)) == 136);
  assert(memcmp(repeated, chunk, 136) == 0); // SELECT/READ retry remains idempotent.
  assert(encodeDataPacket(chunk, meta.id, 128, test, sizeof(test)) == 136);
  assert(read32(chunk) == meta.id && read32(chunk + 4) == 128 && chunk[8] == 128 && chunk[135] == 255);
  memcpy(assembled + 128, chunk + 8, 128);
  assert(memcmp(assembled, test, sizeof(test)) == 0 && crc32(assembled, sizeof(assembled)) == 0x29058c73U);
  assert(encodeDataPacket(chunk, meta.id, 128, test, 129) == 9 && chunk[8] == 128);
  assert(encodeDataPacket(chunk, meta.id, 129, test, 129) == 0);
  assert(encodeDataPacket(chunk, meta.id, 0xffffffffU, test, 129) == 0);
  assert(encodeDataPacket(chunk, meta.id, 0, nullptr, sizeof(test)) == 0);
  ArchiveRecord archive;
  archive.sequence = 42; archive.boot = 77; archive.captureMs = 0xfffffff0U;
  archive.id = 123; archive.length = 4096; archive.crc = 0x12345678;
  archive.capturedUtc = archive.retainedUtc = TEST_CUTOFF_UTC - 1000;
  archive.mode = MODE_TEST_PHOTO; archive.run = DEFAULT_TEST_RUN;
  uint8_t record[48]; archive.encode(record); ArchiveRecord decoded;
  assert(decoded.decode(record));
  assert(decoded.sequence == 42 && decoded.boot == 77 && decoded.captureMs == 0xfffffff0U);
  assert(decoded.id == 123 && decoded.length == 4096 && decoded.crc == 0x12345678);
  assert(decoded.capturedUtc == archive.capturedUtc && decoded.retainedUtc == archive.retainedUtc);
  assert(decoded.run == DEFAULT_TEST_RUN && decoded.mode == MODE_TEST_PHOTO && !decoded.fallback);
  assert(!decoded.expiredUtc(archive.retainedUtc + TEST_RETENTION_SECONDS - 1));
  assert(decoded.expiredUtc(archive.retainedUtc + TEST_RETENTION_SECONDS));
  assert(!decoded.expiredUtc(0)); // Unknown clock never invents an age.
  decoded.boot = 999; // Restart cannot extend absolute expiry.
  assert(decoded.expiredUtc(archive.retainedUtc + TEST_RETENTION_SECONDS));
  for (unsigned i = 0; i < sizeof(record); ++i) {
    record[i] ^= 0x80; assert(!decoded.decode(record)); record[i] ^= 0x80;
  }
  archive.length = MAX_IMAGE_BYTES + 1; archive.encode(record); assert(!decoded.decode(record));
  assert(IDLE_TIMEOUT_MS >= 30000 && CONNECTION_LIMIT_MS == 660000);
  assert(!elapsedAtLeast(1000, 1001, IDLE_TIMEOUT_MS)); // Stale pre-lock sample: no false expiry.
  assert(!elapsedAtLeast(1000, 1001, CONNECTION_LIMIT_MS));
  assert(!elapsedAtLeast(1000 + IDLE_TIMEOUT_MS - 1, 1000, IDLE_TIMEOUT_MS));
  assert(elapsedAtLeast(1000 + IDLE_TIMEOUT_MS, 1000, IDLE_TIMEOUT_MS));
  assert(!elapsedAtLeast(1000 + CONNECTION_LIMIT_MS - 1, 1000, CONNECTION_LIMIT_MS));
  assert(elapsedAtLeast(1000 + CONNECTION_LIMIT_MS, 1000, CONNECTION_LIMIT_MS));
  const uint32_t beforeWrap = 0xfffffff0U;
  assert(!elapsedAtLeast(0x10, beforeWrap, IDLE_TIMEOUT_MS));
  assert(elapsedAtLeast(uint32_t(beforeWrap + IDLE_TIMEOUT_MS), beforeWrap, IDLE_TIMEOUT_MS));
  assert(!elapsedAtLeast(uint32_t(beforeWrap + CONNECTION_LIMIT_MS - 1), beforeWrap, CONNECTION_LIMIT_MS));
  assert(elapsedAtLeast(uint32_t(beforeWrap + CONNECTION_LIMIT_MS), beforeWrap, CONNECTION_LIMIT_MS));
  uint8_t captureCommand[9] = {CAPTURE, 1, 0, 0, 0, 13, 12, 0, 0};
  uint8_t duty = 255;
  assert(captureFlashDuty(captureCommand, 7, duty) && duty == 0);
  captureCommand[7] = MAX_FLASH_DUTY;
  assert(captureFlashDuty(captureCommand, 8, duty) && duty == 64);
  captureCommand[7] = 0;
  assert(captureFlashDuty(captureCommand, 8, duty) && duty == 0);
  captureCommand[7] = MAX_FLASH_DUTY + 1;
  assert(!captureFlashDuty(captureCommand, 8, duty));
  assert(!captureFlashDuty(captureCommand, 6, duty));
  assert(!captureFlashDuty(captureCommand, 9, duty));
  assert(!captureFlashDuty(nullptr, 7, duty));
  captureCommand[0] = TEST;
  assert(!captureFlashDuty(captureCommand, 8, duty));
  static_assert(MAX_FLASH_MS == 2000 && MAX_FLASH_DUTY == 64);
  AdvertisingRecovery advertising;
  advertising.request(1000);
  assert(!advertising.takeAttempt(1099, false));
  assert(advertising.takeAttempt(1100, false) && advertising.attempts == 1);
  advertising.completed(false, 1);
  assert(!advertising.active && advertising.requested);
  assert(!advertising.takeAttempt(3099, false));
  assert(advertising.takeAttempt(3100, false) && advertising.attempts == 2);
  assert(advertising.takeAttempt(5100, false) && advertising.attempts == 3);
  assert(!advertising.takeAttempt(7100, false)); // Three failures stay bounded.
  advertising.connected(); // Includes a rejected connection: radio advertising stopped.
  assert(!advertising.active && !advertising.requested);
  advertising.disconnected(false, 8000); // Rejected peer must restart advertising.
  assert(advertising.takeAttempt(8100, false));
  advertising.completed(true, 0);
  assert(advertising.active && !advertising.requested);
  assert(!advertising.takeAttempt(10100, false));
  advertising.connected();
  advertising.disconnected(true, 12000); // Another accepted session must survive.
  assert(!advertising.requested && !advertising.takeAttempt(12100, true));
  advertising.disconnected(false, beforeWrap);
  assert(!advertising.takeAttempt(uint32_t(beforeWrap + 99), false));
  assert(advertising.takeAttempt(uint32_t(beforeWrap + 100), false));
  advertising.connected();
  advertising.completed(true, 0); // A late start event cannot advertise over an owner.
  assert(!advertising.active && !advertising.requested);
  std::puts("PASS:168h UTC archive;  v2 wire/CRC/archive; stale-clock and wrap limits; CAPTURE7/8 flash bounds; rejected-peer advertising and bounded retries");
}
