#pragma once
#include <host/ble_gap.h>
#include <host/ble_hs.h>
#include <host/ble_store.h>

namespace meter {
// Metadata gate only. Never log/export key material or write/delete bond data.
inline bool s3SecureBond(const ble_gap_conn_desc *desc) {
  if (!desc || !desc->sec_state.encrypted || !desc->sec_state.bonded || desc->sec_state.key_size != 16) return false;
  ble_store_key_sec key = {}; key.peer_addr = desc->peer_id_addr;
  ble_store_value_sec stored = {};
  if (ble_store_read_peer_sec(&key, &stored) == 0 && stored.ltk_present && stored.sc && stored.key_size == 16) return true;
  stored = {};
  return ble_store_read_our_sec(&key, &stored) == 0 && stored.ltk_present && stored.sc && stored.key_size == 16;
}
}
