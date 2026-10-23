#pragma once
#include <cstdint>
struct ble_addr_t { uint8_t type; uint8_t val[6]; };
struct ble_gap_sec_state { unsigned encrypted:1; unsigned authenticated:1; unsigned bonded:1; unsigned key_size:5; };
struct ble_gap_conn_desc { ble_addr_t peer_id_addr; ble_gap_sec_state sec_state; };
