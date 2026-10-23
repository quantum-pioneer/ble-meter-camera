#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace meter {
constexpr char SERVICE_UUID[] = "9a8e0001-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr char CONTROL_UUID[] = "9a8e0002-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr char METADATA_UUID[] = "9a8e0003-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr char DATA_UUID[] = "9a8e0004-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr char MANIFEST_UUID[] = "9a8e0005-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr char HEALTH_UUID[] = "9a8e0006-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr char BOOT_DIAGNOSTIC_UUID[] = "9a8e0007-55d5-4c52-a75b-9dfe0e5f5a01";
constexpr uint8_t VERSION = 2;
constexpr size_t CHUNK_BYTES = 128;
constexpr size_t DATA_VALUE_BYTES = 8 + CHUNK_BYTES;
constexpr uint16_t LOCAL_ATT_MTU = 247;
constexpr size_t MAX_IMAGE_BYTES = 128 * 1024;
constexpr uint32_t IDLE_TIMEOUT_MS = 45000;
constexpr uint32_t CONNECTION_LIMIT_MS = 660000;
constexpr uint32_t CAPTURE_INTERVAL_MS = 10000;
constexpr uint8_t MAX_FLASH_DUTY = 64;
constexpr uint32_t MAX_FLASH_MS = 2000;
inline bool captureFlashDuty(const uint8_t *bytes, size_t length, uint8_t &duty) {
  if (!bytes || (length != 7 && length != 8) || bytes[0] != 1) return false;
  duty = length == 8 ? bytes[7] : 0;
  return duty <= MAX_FLASH_DUTY;
}
enum State : uint8_t { IDLE, BUSY, READY, ERROR };
enum Error : uint16_t {
  NONE, BAD_COMMAND, ALREADY_BUSY, CAMERA_FAILED, TOO_LARGE, EXPIRED,
  WRONG_ID, BAD_OFFSET, UNSUPPORTED, RATE_LIMIT, NO_PENDING, SD_FAILED
};
enum Opcode : uint8_t { CAPTURE = 1, SELECT = 2, RELEASE = 3, TEST = 4, ACK = 5, RETRY = 6, CONFIG = 7 };
// Session limits are below half the 32-bit clock range. A slightly older
// sample must not turn a newer activity timestamp into billions of elapsed ms.
inline bool elapsedAtLeast(uint32_t now, uint32_t since, uint32_t limit) {
  uint32_t elapsed = now - since;
  return elapsed < 0x80000000U && elapsed >= limit;
}
inline uint32_t read32(const uint8_t *p) {
  return uint32_t(p[0]) | uint32_t(p[1]) << 8 | uint32_t(p[2]) << 16 | uint32_t(p[3]) << 24;
}
inline void write32(uint8_t *p, uint32_t v) {
  for (unsigned i = 0; i < 4; ++i) p[i] = uint8_t(v >> (8 * i));
}
inline size_t encodeDataPacket(uint8_t (&out)[DATA_VALUE_BYTES], uint32_t id,
                               uint32_t offset, const uint8_t *source, size_t length) {
  if (!source || offset >= length) return 0;
  size_t remaining = length - offset;
  size_t count = remaining < CHUNK_BYTES ? remaining : CHUNK_BYTES;
  write32(out, id); write32(out + 4, offset);
  memcpy(out + 8, source + offset, count);
  return count + 8;
}
inline uint32_t crc32(const uint8_t *p, size_t n) {
  uint32_t crc = 0xffffffffU;
  while (n--) {
    crc ^= *p++;
    for (unsigned i = 0; i < 8; ++i) crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1)));
  }
  return ~crc;
}
struct Metadata {
  State state = IDLE;
  Error error = NONE;
  uint32_t id = 0, length = 0, crc = 0, captureMs = 0;
  void encode(uint8_t (&out)[20]) const {
    out[0] = VERSION; out[1] = state;
    out[2] = uint8_t(error); out[3] = uint8_t(error >> 8);
    write32(out + 4, id); write32(out + 8, length);
    write32(out + 12, crc); write32(out + 16, captureMs);
  }
};
} // namespace meter
