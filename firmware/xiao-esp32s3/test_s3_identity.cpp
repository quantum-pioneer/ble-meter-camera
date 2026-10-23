#include "BleMeterCamera/sd_archive.h"
#include <cassert>
#include <unistd.h>
namespace fs=std::filesystem;
int main(){
 char t[]="/tmp/s3-archive-identity-XXXXXX";assert(mkdtemp(t));SD.root=t;
 auto owned=SD.root/"ble-meter-camera-123456789abc";
 {std::ofstream f(SD.root/"unrelated.txt");f<<"preserve";}
 SDArchive missing;missing.begin(1);assert(!missing.ready && missing.lastError==SDArchive::OWNER);assert(!fs::exists(owned));
 fs::create_directories(owned);
 {std::ofstream f(owned/"OWNER.txt");f<<"BLE_METER_CAMERA_ARCHIVE_V1 device=aabbccddeeff\n";}
 {std::ofstream f(owned/"preserve.jpg");f<<"preserve";}
 SDArchive wrong;wrong.begin(2);assert(!wrong.ready && wrong.lastError==SDArchive::OWNER);assert(fs::exists(owned/"preserve.jpg"));
 {std::ofstream f(owned/"OWNER.txt");f<<"BLE_METER_CAMERA_ARCHIVE_V1 device=123456789abc\n";}
 SDArchive correct;correct.begin(3);assert(correct.ready);assert(correct.maintain(meter::TEST_CUTOFF_UTC-1,meter::DEFAULT_TEST_RUN));
 assert(!fs::exists(SD.root/"ble-meter-camera-aabbccddeeff"));
 {std::ofstream f(owned/"OWNER.txt");f<<"tampered";}
 assert(!correct.maintain(meter::TEST_CUTOFF_UTC,meter::DEFAULT_TEST_RUN));assert(correct.lastError==SDArchive::OWNER);assert(fs::exists(owned/"preserve.jpg") && fs::exists(SD.root/"unrelated.txt"));
 fs::remove_all(SD.root);std::puts("PASS: exact old archive preexistence/OWNER and maintenance revalidation, no silent adoption or unrelated deletion");
}
