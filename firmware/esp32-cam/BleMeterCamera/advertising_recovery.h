#pragma once
#include <stdint.h>

namespace meter {
struct AdvertisingRecovery {
  static constexpr uint8_t MAX_ATTEMPTS = 3;
  static constexpr uint32_t RETRY_MS = 2000;
  bool requested = false, active = false;
  uint8_t attempts = 0;
  uint32_t nextAttempt = 0;
  int lastStatus = -1;
  void request(uint32_t now) {
    requested = true; active = false; attempts = 0; nextAttempt = now + 100;
  }
  void connected() { requested = false; active = false; attempts = 0; }
  void disconnected(bool ownedConnectionRemains, uint32_t now) {
    if (!ownedConnectionRemains) request(now);
  }
  bool takeAttempt(uint32_t now, bool ownedConnectionRemains) {
    if (ownedConnectionRemains || !requested || active || attempts >= MAX_ATTEMPTS || int32_t(now - nextAttempt) < 0) return false;
    ++attempts; nextAttempt = now + RETRY_MS; return true;
  }
  void completed(bool success, int status) {
    lastStatus = status; active = requested && success;
    if (active) requested = false;
  }
};
}
