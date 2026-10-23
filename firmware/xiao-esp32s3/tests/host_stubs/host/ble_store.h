#pragma once
#include "ble_gap.h"
#include <cstring>
struct ble_store_key_sec { ble_addr_t peer_addr; uint8_t idx; };
struct ble_store_value_sec { uint8_t key_size; bool ltk_present; bool sc; };
inline bool peerExists=false,ourExists=false;
inline ble_addr_t storedPeer{};
inline ble_store_value_sec peerValue{},ourValue{};
inline int ble_store_read_peer_sec(const ble_store_key_sec *k,ble_store_value_sec *v){ if(!peerExists || std::memcmp(&k->peer_addr,&storedPeer,sizeof(storedPeer)))return 1;*v=peerValue;return 0; }
inline int ble_store_read_our_sec(const ble_store_key_sec *k,ble_store_value_sec *v){ if(!ourExists || std::memcmp(&k->peer_addr,&storedPeer,sizeof(storedPeer)))return 1;*v=ourValue;return 0; }
