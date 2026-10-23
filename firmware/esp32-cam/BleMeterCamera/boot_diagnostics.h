#pragma once
#include <stddef.h>
#include <stdint.h>
#include "protocol.h"

namespace meter {
enum BootStage : uint8_t {
  BOOT_UNKNOWN = 0, BOOT_APP_ENTERED = 1, BOOT_STATE_INIT = 2,
  BOOT_CAMERA_INIT = 3, BOOT_POLICY_NVS = 4, BOOT_SD_INIT = 5,
  BOOT_FLASH_INIT = 6, BOOT_BLE_INIT = 7, BOOT_BLE_SERVICE = 8,
  BOOT_ADVERTISING_REQUESTED = 9, BOOT_ADVERTISING_ACTIVE = 10,
  BOOT_ADVERTISING_EXHAUSTED = 11
};
constexpr uint32_t BOOT_RECORD_MAGIC = 0x31444d42U; // BMD1, little endian.
constexpr size_t BOOT_RECORD_BYTES = 24;
constexpr uint16_t BOOT_NVS_READY = 1, BOOT_PRIOR_VALID = 2,
                   BOOT_COUNTER_OVERFLOW = 4, BOOT_WRITE_ERROR = 8;

struct BootRecord {
  uint8_t stage = BOOT_UNKNOWN;
  uint16_t flags = 0; // Persisted bit0: counter saturated. Other bits reserved.
  uint32_t counter = 0, resetReason = 0, strap = 0;
  void encode(uint8_t *out) const {
    write32(out, BOOT_RECORD_MAGIC); out[4] = 1; out[5] = stage;
    out[6] = uint8_t(flags); out[7] = uint8_t(flags >> 8);
    write32(out + 8, counter); write32(out + 12, resetReason);
    write32(out + 16, strap); write32(out + 20, crc32(out, 20));
  }
  bool decode(const uint8_t *in, size_t length) {
    if (!in || length != BOOT_RECORD_BYTES || read32(in) != BOOT_RECORD_MAGIC ||
        in[4] != 1 || in[5] < BOOT_APP_ENTERED || in[5] > BOOT_ADVERTISING_EXHAUSTED ||
        in[6] > 1 || in[7] || !read32(in + 8) ||
        (in[6] && read32(in + 8) != UINT32_MAX) || read32(in + 20) != crc32(in, 20)) return false;
    stage = in[5]; flags = in[6]; counter = read32(in + 8);
    resetReason = read32(in + 12); strap = read32(in + 16); return true;
  }
};

// Only setup/main-loop code calls begin/checkpoint. BLE reads a published copy.
// Production writes one NVS blob key with one commit; host fixtures inject it.
class BootDiagnostics {
 public:
  BootRecord previous, current;
  uint16_t flags = 0;
  void begin(bool nvsReady, const uint8_t *prior, size_t length,
             uint32_t resetReason, uint32_t strap) {
    previous = BootRecord{}; current = BootRecord{};
    flags = nvsReady ? BOOT_NVS_READY : BOOT_WRITE_ERROR;
    if (nvsReady && previous.decode(prior, length)) flags |= BOOT_PRIOR_VALID;
    current.stage = BOOT_APP_ENTERED; current.resetReason = resetReason; current.strap = strap;
    if (!nvsReady) return; // Counter unknown; do not invent a persistent count.
    if (previous.counter == UINT32_MAX) {
      current.counter = UINT32_MAX; current.flags = 1; flags |= BOOT_COUNTER_OVERFLOW;
    } else current.counter = previous.counter + 1;
  }
  template <typename Writer> bool checkpoint(BootStage stage, Writer write) {
    current.stage = stage;
    if (!(flags & BOOT_NVS_READY)) { flags |= BOOT_WRITE_ERROR; return false; }
    uint8_t encoded[BOOT_RECORD_BYTES]; current.encode(encoded);
    if (!write(encoded, sizeof(encoded))) { flags |= BOOT_WRITE_ERROR; return false; }
    return true; // A previous failure remains visible throughout this boot.
  }
  void encodeWire(uint8_t *out) const {
    out[0] = 1; out[1] = previous.stage;
    out[2] = uint8_t(flags); out[3] = uint8_t(flags >> 8);
    write32(out + 4, current.counter); write32(out + 8, previous.counter);
    write32(out + 12, previous.resetReason); write32(out + 16, previous.strap);
  }
};
}
