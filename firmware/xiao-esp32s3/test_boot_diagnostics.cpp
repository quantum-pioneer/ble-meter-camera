#include "BleMeterCamera/boot_diagnostics.h"
#include <cassert>
#include <cstdio>
#include <vector>
using namespace meter;

struct AtomicStore {
  std::vector<uint8_t> bytes;
  bool fail = false;
  unsigned writes = 0;
  bool write(const uint8_t *data, size_t length) {
    ++writes;
    if (fail) return false; // NVS leaves the previous committed value intact.
    bytes.assign(data, data + length); return true;
  }
};
static BootDiagnostics boot(AtomicStore &store, uint32_t reset, uint32_t strap) {
  BootDiagnostics d;
  d.begin(true, store.bytes.data(), store.bytes.size(), reset, strap);
  d.checkpoint(BOOT_APP_ENTERED, [&](const uint8_t *p, size_t n) { return store.write(p, n); });
  return d;
}
static void checkpoint(BootDiagnostics &d, AtomicStore &store, BootStage stage) {
  d.checkpoint(stage, [&](const uint8_t *p, size_t n) { return store.write(p, n); });
}
int main() {
  AtomicStore store;
  auto first = boot(store, 1, 0x13);
  assert(first.flags == BOOT_NVS_READY && first.current.counter == 1);
  assert(first.previous.counter == 0 && first.previous.stage == BOOT_UNKNOWN);
  checkpoint(first, store, BOOT_ADVERTISING_ACTIVE);
  auto second = boot(store, 3, 0x17);
  assert(second.flags == (BOOT_NVS_READY | BOOT_PRIOR_VALID));
  assert(second.current.counter == 2 && second.previous.counter == 1);
  assert(second.previous.stage == BOOT_ADVERTISING_ACTIVE && second.previous.resetReason == 1 && second.previous.strap == 0x13);
  uint8_t frozen[20], after[20]; second.encodeWire(frozen);
  checkpoint(second, store, BOOT_CAMERA_INIT);
  checkpoint(second, store, BOOT_ADVERTISING_EXHAUSTED);
  checkpoint(second, store, BOOT_ADVERTISING_ACTIVE);
  second.encodeWire(after);
  for (size_t i = 0; i < sizeof(after); ++i) assert(frozen[i] == after[i]);
  assert(after[0] == 1 && after[1] == BOOT_ADVERTISING_ACTIVE && after[2] == 3 && after[3] == 0);
  assert(read32(after + 4) == 2 && read32(after + 8) == 1 && read32(after + 12) == 1 && read32(after + 16) == 0x13);

  // A failure/stall after every recorded phase is observable on the next boot.
  for (uint8_t stage = BOOT_APP_ENTERED; stage <= BOOT_ADVERTISING_EXHAUSTED; ++stage) {
    auto attempt = boot(store, 9, 0x1b);
    checkpoint(attempt, store, BootStage(stage));
    uint32_t count = attempt.current.counter;
    auto recovery = boot(store, 1, 0x13);
    assert(recovery.previous.stage == stage && recovery.previous.counter == count);
    assert(recovery.previous.resetReason == 9 && recovery.previous.strap == 0x1b);
  }
  // Failed writes remain visible and cannot manufacture a later durable stage.
  auto failing = boot(store, 1, 0x13);
  checkpoint(failing, store, BOOT_CAMERA_INIT);
  auto durable = store.bytes;
  store.fail = true; checkpoint(failing, store, BOOT_SD_INIT);
  assert(store.bytes == durable && (failing.flags & BOOT_WRITE_ERROR));
  store.fail = false;
  auto recovered = boot(store, 1, 0x13);
  assert(recovered.previous.stage == BOOT_CAMERA_INIT);
  store.fail = true;
  auto initialWriteFailed = boot(store, 1, 0x13);
  assert(initialWriteFailed.flags & BOOT_WRITE_ERROR);
  store.fail = false;
  auto retry = boot(store, 1, 0x13);
  assert(retry.previous.counter == recovered.current.counter);
  assert(retry.current.counter == initialWriteFailed.current.counter); // No persisted app-start, not proof of ROM failure.

  BootRecord record; assert(record.decode(store.bytes.data(), store.bytes.size()));
  uint8_t encoded[BOOT_RECORD_BYTES]; record.encode(encoded);
  for (size_t i = 0; i < sizeof(encoded); ++i) {
    encoded[i] ^= 1; BootRecord invalid; assert(!invalid.decode(encoded, sizeof(encoded))); encoded[i] ^= 1;
  }
  for (size_t n = 0; n < sizeof(encoded); ++n) { BootRecord invalid; assert(!invalid.decode(encoded, n)); }
  for (unsigned field : {4U, 5U, 6U, 7U}) {
    uint8_t invalid[BOOT_RECORD_BYTES]; record.encode(invalid); invalid[field] = 0xff;
    write32(invalid + 20, crc32(invalid, 20)); BootRecord bad; assert(!bad.decode(invalid, sizeof(invalid)));
  }
  store.bytes.assign(encoded, encoded + sizeof(encoded)); store.bytes[0] ^= 1;
  auto corrupt = boot(store, 1, 0x13);
  assert(corrupt.flags == BOOT_NVS_READY && corrupt.current.counter == 1 && corrupt.previous.counter == 0);

  BootRecord maximum; maximum.stage = BOOT_ADVERTISING_ACTIVE; maximum.counter = UINT32_MAX;
  maximum.resetReason = 1; maximum.strap = 0x13; maximum.encode(encoded);
  store.bytes.assign(encoded, encoded + sizeof(encoded));
  auto saturated = boot(store, 1, 0x13);
  assert(saturated.current.counter == UINT32_MAX && (saturated.flags & BOOT_COUNTER_OVERFLOW));
  auto stillSaturated = boot(store, 1, 0x13);
  assert(stillSaturated.current.counter == UINT32_MAX && (stillSaturated.flags & BOOT_COUNTER_OVERFLOW));
  BootDiagnostics unavailable; unavailable.begin(false, encoded, sizeof(encoded), 1, 0x13);
  bool called = false;
  assert(!unavailable.checkpoint(BOOT_BLE_INIT, [&](const uint8_t *, size_t) { called = true; return true; }));
  assert(!called && unavailable.current.counter == 0 && unavailable.previous.counter == 0);
  assert(unavailable.flags == BOOT_WRITE_ERROR);
  std::puts("PASS: boot-record CRC/version/flags, atomic stage lifecycle, frozen prior snapshot, failed-stage/write recovery, unavailable NVS, counter saturation and wire layout");
}
