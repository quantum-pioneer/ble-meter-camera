#pragma once
#include "protocol.h"
#include "bench_policy.h"

namespace meter {
constexpr uint32_t ARCHIVE_MAGIC = 0x32434d42; // BMC2, little endian.
constexpr uint32_t LEGACY_ARCHIVE_MAGIC = 0x31434d42;
constexpr uint32_t UNKNOWN_CAPTURE_UPTIME = 0xffffffffU;
constexpr uint32_t ARCHIVE_RETENTION_MS = TEST_RETENTION_SECONDS * 1000U;
struct ArchiveRecord {
  uint32_t sequence = 0, boot = 0, captureMs = 0, id = 0, length = 0, crc = 0;
  uint32_t capturedUtc = 0, retainedUtc = 0, run = 0;
  uint8_t mode = MODE_TEST_PHOTO;
  bool fallback = false;
  void encode(uint8_t (&out)[48]) const {
    for (auto &byte : out) byte = 0;
    write32(out, ARCHIVE_MAGIC); write32(out + 4, sequence);
    write32(out + 8, boot); write32(out + 12, captureMs);
    write32(out + 16, id); write32(out + 20, length); write32(out + 24, crc);
    write32(out + 28, capturedUtc); write32(out + 32, retainedUtc); write32(out + 36, run);
    out[40] = mode; out[41] = fallback ? 1 : 0;
    write32(out + 44, crc32(out, 44));
  }
  bool decode(const uint8_t (&in)[48]) {
    if (read32(in) != ARCHIVE_MAGIC || crc32(in, 44) != read32(in + 44) ||
        in[42] || in[43] || in[41] > 1) return false;
    decodeCommon(in);
    capturedUtc = read32(in + 28); retainedUtc = read32(in + 32); run = read32(in + 36);
    mode = in[40]; fallback = in[41] == 1;
    return valid() && run && retainedUtc >= 1792000000U &&
           (mode == MODE_TEST_PHOTO || mode == MODE_METER) &&
           ((fallback && !capturedUtc) || (!fallback && capturedUtc == retainedUtc));
  }
  bool decodeLegacy(const uint8_t (&in)[32]) {
    if (read32(in) != LEGACY_ARCHIVE_MAGIC || crc32(in, 28) != read32(in + 28)) return false;
    decodeCommon(in); capturedUtc = retainedUtc = run = 0; mode = MODE_TEST_PHOTO; fallback = true;
    return valid();
  }
  uint32_t expires() const {
    return mode == MODE_TEST_PHOTO && retainedUtc && retainedUtc <= 0xffffffffU - TEST_RETENTION_SECONDS
             ? retainedUtc + TEST_RETENTION_SECONDS : 0;
  }
  bool expiredUtc(uint32_t utc) const { return utc && expires() && utc >= expires(); }
 private:
  bool valid() const { return sequence != 0 && length >= 4 && length <= MAX_IMAGE_BYTES; }
  void decodeCommon(const uint8_t *in) {
    sequence = read32(in + 4); boot = read32(in + 8); captureMs = read32(in + 12);
    id = read32(in + 16); length = read32(in + 20); crc = read32(in + 24);
  }
};
} // namespace meter
