#include "BleMeterCamera/s3_security.h"
#include <cassert>
#include <cstdio>
int main(){
 ble_gap_conn_desc d{};d.peer_id_addr.type=0;d.peer_id_addr.val[0]=42;storedPeer=d.peer_id_addr;
 assert(!meter::s3SecureBond(nullptr));
 d.sec_state.encrypted=1;d.sec_state.bonded=1;d.sec_state.key_size=16;
 assert(!meter::s3SecureBond(&d));
 peerExists=true;peerValue={16,true,true};assert(!d.sec_state.authenticated);assert(meter::s3SecureBond(&d));
 d.sec_state.encrypted=0;assert(!meter::s3SecureBond(&d));d.sec_state.encrypted=1;
 d.sec_state.bonded=0;assert(!meter::s3SecureBond(&d));d.sec_state.bonded=1;
 d.sec_state.key_size=15;assert(!meter::s3SecureBond(&d));d.sec_state.key_size=16;
 peerValue.sc=false;assert(!meter::s3SecureBond(&d));peerValue.sc=true;
 peerValue.key_size=15;assert(!meter::s3SecureBond(&d));peerValue.key_size=16;
 peerValue.ltk_present=false;assert(!meter::s3SecureBond(&d));
 peerExists=false;ourExists=true;ourValue={16,true,true};assert(meter::s3SecureBond(&d));
 d.peer_id_addr.val[0]++;assert(!meter::s3SecureBond(&d));
 std::puts("PASS: actual SC metadata gate rejects plaintext, unbonded, short-key, legacy, missing-LTK, wrong-peer and missing-store states");
}
