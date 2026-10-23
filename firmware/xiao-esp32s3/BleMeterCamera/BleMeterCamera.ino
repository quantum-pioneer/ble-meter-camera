// Isolated XIAO ESP32-S3 Sense + OV3660 port. Arduino ESP32 3.3.10.
// Historical bench-tested port; sustained reliability remains unproved.
// No Wi-Fi credentials, TCP services, SPP, notifications, or automatic captures.
// Mandatory SD commit/readback precedes BLE READY. Never formats a card.
#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLESecurity.h>
#include <esp_camera.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <esp32-hal-bt.h>
#include <esp_system.h>
#include <Preferences.h>
#include <soc/gpio_reg.h>
#include <soc/soc.h>
#include <driver/ledc.h>
#include <atomic>
#include "protocol.h"
#include "sd_archive.h"
#include "advertising_recovery.h"
#include "boot_diagnostics.h"
#include "s3_security.h"

#if !defined(CONFIG_IDF_TARGET_ESP32S3) || !defined(CONFIG_NIMBLE_ENABLED)
#error "This isolated port requires the pinned ESP32-S3 NimBLE SDK"
#endif
#ifndef BOARD_HAS_PSRAM
#error "XIAO ESP32-S3 Sense camera requires OPI PSRAM enabled"
#endif

using namespace meter;
static BLEServer *server = nullptr;
static SemaphoreHandle_t stateLock;
static QueueHandle_t captureQueue;
static Metadata metadata;
static camera_fb_t *image = nullptr;
static uint8_t *retryImage = nullptr;
static SDArchive archive;
static BenchPolicy policy;
static Preferences policyStore;
static bool policyStoreReady = false, contextReady = false;
static ArchiveRecord activeRecord;
static uint32_t archiveBoot = 0;
static uint8_t bootResetReason = 0;
static Preferences bootDiagnosticStore;
static BootDiagnostics bootDiagnostics;
static uint8_t bootDiagnosticWire[20] = {};
static bool bootDiagnosticWireDirty = false;
static std::atomic<bool> startupAdvertisingSucceeded{false};
static bool bootAdvertisingRecorded = false, bootAdvertisingExhaustedRecorded = false;
static uint32_t activeArchiveSequence = 0;
static uint8_t testImage[256];
static bool isTest = false, cameraOK = false, connected = false;
static std::atomic<bool> peerSecurityVerified{false}; // Revoked immediately on any authentication transition.
static bool activeFromPending = false;
static bool disconnectPending = false, disconnectIssued = false;
static AdvertisingRecovery advertisingRecovery;
static uint16_t connectionId = 0xffff;
static uint32_t generation = 0, selectedOffset = 0, sessionStart = 0, lastActivity = 0;
static uint32_t lastCaptureRequest = 0, disconnectAt = 0;
static bool capturedBefore = false;
struct CaptureRequest {
  uint32_t generation, id; uint8_t opcode, frameCode, quality, flashDuty;
  uint32_t utc = 0, cutoff = 0, run = 0, retention = 0;
};
// The dedicated proxy Bluetooth identity is required in private local_config.h.

static uint32_t uptimeMs() { return uint32_t(esp_timer_get_time() / 1000); }
static uint64_t uptime64() { return uint64_t(esp_timer_get_time() / 1000); }
static bool lockState() { return xSemaphoreTake(stateLock, pdMS_TO_TICKS(20)) == pdTRUE; }
static void unlockState() { xSemaphoreGive(stateLock); }
static void publishBootDiagnostic() {
  if (!bootDiagnosticWireDirty) return;
  uint8_t value[20]; bootDiagnostics.encodeWire(value);
  if (stateLock && !lockState()) return;
  memcpy(bootDiagnosticWire, value, sizeof(value)); bootDiagnosticWireDirty = false;
  if (stateLock) unlockState();
}
static void bootCheckpoint(BootStage stage) {
  // Setup/main loop only. Never hold stateLock while committing NVS.
  bootDiagnostics.checkpoint(stage, [](const uint8_t *bytes, size_t length) {
    return bootDiagnosticStore.putBytes("state", bytes, length) == length;
  });
  bootDiagnosticWireDirty = true; publishBootDiagnostic();
}
static void initBootDiagnostic(uint32_t resetReason, uint32_t strap) {
  uint8_t prior[BOOT_RECORD_BYTES] = {}; size_t length = 0;
  bool ready = bootDiagnosticStore.begin("meter_boot_v1", false);
  if (ready && bootDiagnosticStore.getBytesLength("state") == sizeof(prior))
    length = bootDiagnosticStore.getBytes("state", prior, sizeof(prior));
  bootDiagnostics.begin(ready, prior, length, resetReason, strap);
  bootCheckpoint(BOOT_APP_ENTERED);
}
static bool persistPolicy() {
  if (!policyStoreReady) return false;
  bool ok = policyStore.putUInt("cutoff", TEST_CUTOFF_UTC) == sizeof(uint32_t) &&
            policyStore.putUInt("floor", policy.lastTrustedUtc) == sizeof(uint32_t) &&
            policyStore.putUInt("run", policy.run) == sizeof(uint32_t) &&
            policyStore.putUChar("mode", policy.mode) == sizeof(uint8_t) &&
            policyStore.putBool("expired", policy.testExpired) == sizeof(bool);
  if (!ok) Serial.println("POLICY_STORAGE failed; capture context blocked");
  return ok;
}
static bool operationsAllowed() { return contextReady && policy.allowed(uptime64()); }
static bool imageAllowed() {
  return operationsAllowed() && (isTest ||
         policy.canServe(activeRecord.mode, activeRecord.run, activeRecord.expires(), uptime64()));
}
// The Sense expansion has no equivalent white capture LED. Never drive the
// original ESP32-CAM GPIO4/GPIO33 or SD-CS/user-LED GPIO21 as a light.
static void flashOff() {}
static void initFlash() {
  Serial.println("CAMERA_FLASH supported=0 reason=no_white_capture_LED; nonzero duty rejected");
}
// Caller holds stateLock. This is never called while another task reads image bytes.
static void clearImage() {
  flashOff();
  if (image) { esp_camera_fb_return(image); image = nullptr; }
  if (retryImage) { heap_caps_free(retryImage); retryImage = nullptr; }
  activeArchiveSequence = 0;
  activeRecord = ArchiveRecord{};
  activeFromPending = false;
  isTest = false; selectedOffset = 0;
}
static void resetState() {
  ++generation; clearImage(); metadata = Metadata{};
}
static void setError(Error error) {
  metadata.error = error;
  // Keep a ready frame's identity, checksum, timestamp, and data for retry/release.
  if (metadata.state != READY && metadata.state != meter::BUSY) metadata.state = ERROR;
}
static bool frameSize(uint8_t code, framesize_t &out) {
  // Protocol numbers deliberately do not depend on the Arduino sensor enum.
  switch (code) {
    case 1: out = FRAMESIZE_QQVGA; return true;
    case 5: out = FRAMESIZE_QVGA; return true;
    case 8: out = FRAMESIZE_VGA; return true;
    case 9: out = FRAMESIZE_SVGA; return true;
    case 10: out = FRAMESIZE_XGA; return true;
    case 12: out = FRAMESIZE_SXGA; return true;
    case 13: out = FRAMESIZE_UXGA; return true;
    default: return false;
  }
}

class MetadataCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t value[20] = {};
    if (!lockState()) { characteristic->setValue(value, 0); return; }
    metadata.encode(value); lastActivity = uptimeMs(); unlockState();
    characteristic->setValue(value, sizeof(value));
  }
};
class ManifestCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t value[20] = {};
    if (!lockState()) { characteristic->setValue(value, 0); return; }
    bool hasRecord = metadata.state == READY && !isTest && activeRecord.sequence;
    uint32_t utc = policy.now(uptime64());
    bool expired = policy.mode == MODE_TEST_PHOTO && (policy.testExpired || (utc && utc >= TEST_CUTOFF_UTC));
    value[0] = 1;
    value[1] = (archive.ready ? 1 : 0) | (hasRecord ? 2 : 0) |
      (contextReady && policy.synchronized ? 4 : 0) | (expired ? 8 : 0) |
      (policy.mode == MODE_TEST_PHOTO ? 16 : 32) |
      (hasRecord && activeFromPending ? 64 : 0) |
      (hasRecord && activeRecord.fallback ? 128 : 0);
    value[2] = uint8_t(archive.lastError); value[3] = uint8_t(archive.lastError >> 8);
    write32(value + 4, hasRecord ? activeRecord.capturedUtc : 0);
    write32(value + 8, hasRecord ? activeRecord.expires() : 0);
    write32(value + 12, hasRecord ? activeRecord.run : policy.run);
    write32(value + 16, hasRecord ? activeRecord.sequence : 0);
    lastActivity = uptimeMs(); unlockState();
    characteristic->setValue(value, sizeof(value));
  }
};
class DataCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t value[DATA_VALUE_BYTES]; size_t length = 0;
    if (!lockState()) { characteristic->setValue(value, 0); return; }
    if (metadata.state == READY && imageAllowed() && selectedOffset < metadata.length) {
      const uint8_t *source = isTest ? testImage : (retryImage ? retryImage : (image ? image->buf : nullptr));
      length = encodeDataPacket(value, metadata.id, selectedOffset, source, metadata.length);
      lastActivity = uptimeMs();
    }
    unlockState(); characteristic->setValue(value, length);
  }
};
class HealthCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t value[20] = {};
    if (!lockState()) { characteristic->setValue(value, 0); return; }
    value[0] = 1; value[1] = bootResetReason;
    value[2] = uint8_t(archive.lastError); value[3] = uint8_t(archive.lastError >> 8);
    write32(value + 4, archiveBoot); write32(value + 8, uptimeMs());
    write32(value + 12, archive.count); write32(value + 16, archive.pending);
    lastActivity = uptimeMs(); unlockState();
    characteristic->setValue(value, sizeof(value));
  }
};
class BootDiagnosticCallbacks : public BLECharacteristicCallbacks {
  void onRead(BLECharacteristic *characteristic) override {
    uint8_t value[20];
    if (!lockState()) { characteristic->setValue(value, 0); return; }
    memcpy(value, bootDiagnosticWire, sizeof(value));
    lastActivity = uptimeMs(); unlockState();
    characteristic->setValue(value, sizeof(value));
  }
};
class ControlCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *characteristic) override {
    String value = characteristic->getValue();
    const uint8_t *bytes = reinterpret_cast<const uint8_t *>(value.c_str());
    size_t length = value.length();
    if (!lockState()) return;
    if (!connected || !length || length > 20) { setError(BAD_COMMAND); unlockState(); return; }
    uint8_t opcode = bytes[0]; uint32_t now = uptimeMs();
    if (opcode == CONFIG && length == 20) {
      if (metadata.state == meter::BUSY || metadata.state == READY || bytes[2] || bytes[3]) setError(ALREADY_BUSY);
      else {
        CaptureRequest request{generation, 0, CONFIG, bytes[1], 0, 0};
        request.utc = read32(bytes + 4); request.cutoff = read32(bytes + 8);
        request.run = read32(bytes + 12); request.retention = read32(bytes + 16);
        contextReady = false;
        if (xQueueSend(captureQueue, &request, 0) != pdTRUE) setError(ALREADY_BUSY);
        else { metadata.error = NONE; lastActivity = now; }
      }
    } else if ((opcode == CAPTURE && (length == 7 || length == 8)) || (opcode == TEST && length == 7) || (opcode == RETRY && length == 5)) {
      uint32_t id = read32(bytes + 1); framesize_t fs;
      uint8_t flashDuty = 0;
      if (!operationsAllowed()) setError(EXPIRED);
      else if (metadata.state == meter::BUSY || metadata.state == READY) setError(ALREADY_BUSY);
      else if (id == 0 || (opcode == CAPTURE && (!frameSize(bytes[5], fs) || bytes[6] < 10 || bytes[6] > 40 || !captureFlashDuty(bytes, length, flashDuty) || flashDuty != 0))) setError(UNSUPPORTED);
      else if (capturedBefore && uint32_t(now - lastCaptureRequest) < CAPTURE_INTERVAL_MS) setError(RATE_LIMIT);
      else {
        resetState(); metadata.state = meter::BUSY; metadata.id = id;
        CaptureRequest request{generation, id, opcode, uint8_t(length >= 7 ? bytes[5] : 0), uint8_t(length >= 7 ? bytes[6] : 0), flashDuty};
        if (xQueueSend(captureQueue, &request, 0) != pdTRUE) { metadata.state = ERROR; setError(ALREADY_BUSY); }
        else { lastCaptureRequest = now; capturedBefore = true; lastActivity = now; }
      }
    } else if (opcode == SELECT && length == 9) {
      if (!imageAllowed()) setError(EXPIRED);
      else if (read32(bytes + 1) != metadata.id) setError(WRONG_ID);
      else if (metadata.state != READY) setError(ALREADY_BUSY);
      else if (read32(bytes + 5) >= metadata.length) setError(BAD_OFFSET);
      else { selectedOffset = read32(bytes + 5); metadata.error = NONE; lastActivity = now; }
    } else if (opcode == ACK && length == 5) {
      if (!imageAllowed()) setError(EXPIRED);
      else if (read32(bytes + 1) != metadata.id) setError(WRONG_ID);
      else if (metadata.state != READY || isTest) setError(ALREADY_BUSY);
      else {
        // Never access SD from a Bluetooth callback. Preserve the archive sequence
        // in the queued work, so RELEASE/disconnect cannot discard a durable ACK.
        CaptureRequest ack{generation, activeArchiveSequence, ACK, 0, 0, 0};
        if (activeArchiveSequence && xQueueSend(captureQueue, &ack, 0) != pdTRUE) setError(ALREADY_BUSY);
        else { metadata.error = NONE; lastActivity = now; }
      }
    } else if (opcode == RELEASE && length == 5) {
      if (read32(bytes + 1) != metadata.id && read32(bytes + 1) != 0) setError(WRONG_ID);
      else {
        resetState(); lastActivity = now;
        disconnectPending = true; disconnectAt = now + 100;
      }
    } else setError(BAD_COMMAND);
    unlockState();
  }
};
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *bleServer, ble_gap_conn_desc *desc) override {
    xSemaphoreTake(stateLock, portMAX_DELAY);
    advertisingRecovery.connected();
    unlockState();
    String peer = BLEAddress(desc->peer_id_addr).toString();
    if (!peer.equalsIgnoreCase(METER_ALLOWED_CENTRAL_MAC)) {
      Serial.printf("BLE_PEER rejected=%s reason=central_allowlist\n", peer.c_str());
      bleServer->disconnect(desc->conn_handle); return;
    }
    if (!lockState()) { bleServer->disconnect(desc->conn_handle); return; }
    if (connected) { unlockState(); bleServer->disconnect(desc->conn_handle); return; }
    resetState(); contextReady = false; peerSecurityVerified = false;
    connected = true; connectionId = desc->conn_handle;
    advertisingRecovery.connected();
    sessionStart = lastActivity = uptimeMs(); disconnectPending = false; disconnectIssued = false;
    startupAdvertisingSucceeded.store(true); // A fresh connection also proves advertising reached a peer.
    unlockState();
    BLEDevice::getAdvertising()->stop();
    Serial.printf("BLE connected peer=%s; encrypted SC bonded 16-byte authorized GATT required\n", peer.c_str());
  }
  void onDisconnect(BLEServer *, ble_gap_conn_desc *desc) override {
    xSemaphoreTake(stateLock, portMAX_DELAY);
    if (connected && desc->conn_handle == connectionId) {
      connected = false; peerSecurityVerified = false; connectionId = 0xffff; resetState();
      disconnectPending = false; disconnectIssued = false;
    }
    advertisingRecovery.disconnected(connected, uptimeMs());
    bool ownedRemains = connected;
    unlockState();
    Serial.printf("BLE disconnected id=%u owned_connection_remains=%u advertise_requested=%u\n", desc->conn_handle, ownedRemains, !ownedRemains);
  }
  void onMtuChanged(BLEServer *, ble_gap_conn_desc *, uint16_t mtu) override {
    Serial.printf("BLE_MTU negotiated=%u data_value_max=%u wire_version=%u\n", mtu, unsigned(DATA_VALUE_BYTES), VERSION);
  }
};
class SecurityCallbacks : public BLESecurityCallbacks {
  bool onSecurityRequest() override { return true; }
  bool onAuthorizationRequest(uint16_t handle, uint16_t, bool) override {
    if (!lockState()) return false;
    bool allowed = connected && connectionId == handle && peerSecurityVerified;
    unlockState(); return allowed;
  }
  void onAuthenticationComplete(ble_gap_conn_desc *desc) override {
    peerSecurityVerified.store(false); // Never retain prior authorization through a failed callback/lock.
    if (!desc) return;
    // Only SDK metadata is read: no application bond writes or key-byte logs.
    bool secure = s3SecureBond(desc);
    if (!lockState()) return; // Authorization remains false on a lock failure.
    bool current = connected && connectionId == desc->conn_handle;
    if (current) {
      peerSecurityVerified = secure;
      if (!secure) { disconnectPending = true; disconnectAt = uptimeMs(); }
    }
    unlockState();
    Serial.printf("BLE_AUTH accepted=%u encrypted=%u bonded=%u key_size=%u sc_required=1 method=secure_connections_just_works no_mitm=1\n",
      current && secure, desc->sec_state.encrypted, desc->sec_state.bonded, desc->sec_state.key_size);
  }
};

static bool initCamera() {
  if (!psramFound()) { Serial.println("ERROR PSRAM required; camera disabled"); return false; }
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0; c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = 15; c.pin_d1 = 17; c.pin_d2 = 18; c.pin_d3 = 16;
  c.pin_d4 = 14; c.pin_d5 = 12; c.pin_d6 = 11; c.pin_d7 = 48;
  c.pin_xclk = 10; c.pin_pclk = 13; c.pin_vsync = 38; c.pin_href = 47;
  c.pin_sccb_sda = 40; c.pin_sccb_scl = 39; c.pin_pwdn = -1; c.pin_reset = -1;
  c.xclk_freq_hz = 20000000; c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_UXGA; c.jpeg_quality = 12; c.fb_count = 1;
  c.fb_location = CAMERA_FB_IN_PSRAM; c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  // Sense has no camera PWDN/reset GPIO. Do not touch nonexistent control pins.
  Serial.println("CAMERA startup pins=XIAO_ESP32S3_SENSE PWDN=none reset=none init_attempts=1");
  esp_err_t result = esp_camera_init(&c);
  if (result != ESP_OK) { Serial.printf("ERROR camera init 0x%x\n", result); return false; }
  sensor_t *sensor = esp_camera_sensor_get();
  if (!sensor || sensor->id.PID != OV3660_PID) {
    Serial.printf("ERROR expected OV3660; PID=0x%x\n", sensor ? sensor->id.PID : 0);
    esp_camera_deinit(); return false;
  }
  // Actual cold-start JPEG verified upright with vflip1, but mirrored: use hmirror0.
  // Preserve originals on SD; only newly captured sensor frames are affected.
  if (!sensor->set_vflip || !sensor->set_hmirror ||
      sensor->set_vflip(sensor, 1) != 0 || sensor->set_hmirror(sensor, 0) != 0) {
    Serial.println("ERROR camera orientation settings failed; capture blocked");
    esp_camera_deinit(); return false;
  }
  Serial.println("CAMERA_ORIENTATION vertical_flip=1 horizontal_mirror=0 basis=actual_calendar_orientation");
  Serial.printf("CAMERA OV3660 PID=0x%x PSRAM=%u flash=%u pins=XIAO_ESP32S3_SENSE\n", sensor->id.PID, ESP.getPsramSize(), ESP.getFlashChipSize());
  return true;
}

static void sensorDiagnostic(sensor_t *sensor, const char *phase) {
  sensor->init_status(sensor); // Read actual SCCB exposure/gain registers, not old cached controls.
  Serial.printf("CAMERA_SENSOR phase=%s uptime_ms=%u framesize=%u quality=%u aec=%u agc=%u aec_register=%u agc_gain_index=%u gain_ceiling_enum=%u dsp_aec_select_bit=%u xclk_hz=%d\n",
    phase, uptimeMs(), sensor->status.framesize, sensor->status.quality,
    sensor->status.aec, sensor->status.agc, sensor->status.aec_value,
    sensor->status.agc_gain, sensor->status.gainceiling, sensor->status.aec2, sensor->xclk_freq_hz);
}

static camera_fb_t *settledFrame(const char *phase, uint32_t settleMs, uint32_t budgetMs, uint8_t minimumDiscard) {
  const int64_t startedUs = esp_timer_get_time();
  uint8_t discarded = 0;
  // fb_get itself has the driver's 4s timeout. A NULL or over-budget result
  // aborts immediately, so a stalled read is never retried twenty times.
  while (discarded < 20 && uint64_t(esp_timer_get_time() - startedUs) < uint64_t(budgetMs) * 1000) {
    camera_fb_t *frame = esp_camera_fb_get();
    int64_t nowUs = esp_timer_get_time();
    if (!frame) { Serial.printf("CAMERA_SETTLE phase=%s result=no_frame discarded=%u\n", phase, discarded); return nullptr; }
    int64_t frameUs = int64_t(frame->timestamp.tv_sec) * 1000000 + frame->timestamp.tv_usec;
    if (nowUs - startedUs <= int64_t(budgetMs) * 1000 && discarded >= minimumDiscard && frameUs >= startedUs + int64_t(settleMs) * 1000) {
      Serial.printf("CAMERA_SETTLE phase=%s result=fresh discarded=%u elapsed_ms=%lld frame_age_ms=%lld frame_uptime_ms=%lld\n", phase, discarded, (nowUs - startedUs) / 1000, (nowUs - frameUs) / 1000, frameUs / 1000);
      return frame;
    }
    esp_camera_fb_return(frame); ++discarded;
    delay(75);
  }
  Serial.printf("CAMERA_SETTLE phase=%s result=budget_exceeded discarded=%u\n", phase, discarded);
  return nullptr;
}

static void capture(const CaptureRequest &request) {
  if (request.opcode == CONFIG) {
    if (!lockState()) return;
    bool current = connected && generation == request.generation;
    bool accepted = current && policy.configure(request.frameCode, request.utc, request.cutoff, request.run, request.retention, uptime64());
    unlockState();
    bool durable = accepted && persistPolicy();
    bool maintained = durable && archive.maintain(policy.now(uptime64()), DEFAULT_TEST_RUN);
    if (!lockState()) return;
    current = connected && generation == request.generation;
    contextReady = current && accepted && durable && maintained;
    if (!contextReady) { metadata.state = ERROR; metadata.error = accepted ? SD_FAILED : BAD_COMMAND; }
    else { metadata = Metadata{}; lastActivity = uptimeMs(); }
    unlockState();
    Serial.printf("BENCH_CONTEXT accepted=%u durable=%u cleanup=%u ready=%u utc=%u cutoff=%u expired=%u mode=%u run=%u clock=authenticated_ble\n", accepted, durable, maintained, contextReady, policy.now(uptime64()), TEST_CUTOFF_UTC, policy.testExpired, policy.mode, policy.run);
    return;
  }
  if (request.opcode == ACK) { archive.acknowledge(request.id); return; }
  if (!lockState()) return;
  bool current = connected && operationsAllowed() && metadata.state == meter::BUSY && generation == request.generation;
  unlockState(); if (!current) return;
  camera_fb_t *captured = nullptr; uint8_t *restored = nullptr; Error error = NONE;
  uint32_t archiveSequence = 0;
  ArchiveRecord record;
  uint32_t length = sizeof(testImage), crc = 0, capturedAt = uptimeMs();
  if (request.opcode == TEST) crc = crc32(testImage, sizeof(testImage));
  else if (request.opcode == RETRY) {
    if (!archive.restore(restored, record, policy, uptime64())) error = archive.pending ? SD_FAILED : NO_PENDING;
    else { length = record.length; crc = record.crc; capturedAt = record.boot == archiveBoot ? record.captureMs : UNKNOWN_CAPTURE_UPTIME; archiveSequence = record.sequence; }
  }
  else if (!archive.ready) error = SD_FAILED;
  else if (!cameraOK) error = CAMERA_FAILED;
  else {
    sensor_t *sensor = esp_camera_sensor_get(); framesize_t fs;
    sensorDiagnostic(sensor, "before_settings");
    if (!frameSize(request.frameCode, fs)
        || (sensor->status.framesize != fs && sensor->set_framesize(sensor, fs))
        || (sensor->status.quality != request.quality && sensor->set_quality(sensor, request.quality))) error = UNSUPPORTED;
    else {
      captured = settledFrame("ambient", 1000, 2200, 3);
      sensorDiagnostic(sensor, "after_ambient");
      flashOff(); capturedAt = uptimeMs();
      if (!captured || captured->format != PIXFORMAT_JPEG || captured->len < 4 ||
          captured->buf[0] != 0xff || captured->buf[1] != 0xd8 ||
          captured->buf[captured->len - 2] != 0xff || captured->buf[captured->len - 1] != 0xd9) error = CAMERA_FAILED;
      else if (captured->len > MAX_IMAGE_BYTES) error = TOO_LARGE;
      else {
        uint64_t frameMs = uint64_t(captured->timestamp.tv_sec) * 1000 + captured->timestamp.tv_usec / 1000;
        capturedAt = uint32_t(frameMs);
        length = captured->len; crc = crc32(captured->buf, length);
        uint32_t captureUtc = policy.now(frameMs);
        if (!operationsAllowed() || !captureUtc) error = EXPIRED;
        else {
          archiveSequence = archive.store(captured->buf, length, request.id, capturedAt, crc, captureUtc, policy.mode, policy.run, record);
          if (!archiveSequence) error = SD_FAILED;
        }
      }
    }
  }
  flashOff();
  if (!lockState()) { if (captured) esp_camera_fb_return(captured); if (restored) heap_caps_free(restored); return; }
  current = connected && metadata.state == meter::BUSY && generation == request.generation;
  if (current && !operationsAllowed()) error = EXPIRED;
  // First boot must confirm the actual OV3660 before ANY fixture/photo/retry READY.
  if (current && error == NONE && !cameraOK) error = CAMERA_FAILED;
  bool becameReady = false;
  if (current) {
    if (error != NONE) { metadata.state = ERROR; metadata.error = error; }
    else {
      image = captured; captured = nullptr; isTest = request.opcode == TEST;
      retryImage = restored; restored = nullptr; activeArchiveSequence = archiveSequence;
      activeRecord = record; activeFromPending = request.opcode == RETRY;
      metadata.state = READY; metadata.error = NONE; metadata.length = length;
      metadata.crc = crc; metadata.captureMs = capturedAt; lastActivity = uptimeMs();
      becameReady = true;
    }
  }
  unlockState();
  if (becameReady) Serial.printf("READY id=%u type=%s length=%u crc32=%08x capture_uptime_ms=%u sd_sequence=%u from_pending=%u\n", request.id, request.opcode == TEST ? "test" : "jpeg", length, crc, capturedAt, archiveSequence, request.opcode == RETRY);
  if (captured) esp_camera_fb_return(captured); if (restored) heap_caps_free(restored);
}

static void reportWiFi() {
  wifi_mode_t mode; esp_err_t status = esp_wifi_get_mode(&mode);
  Serial.printf("WIFI_PROOF initialized=%u mode=%d status=%s\n", status != ESP_ERR_WIFI_NOT_INIT, status == ESP_OK ? int(mode) : -1, esp_err_to_name(status));
  if (status == ESP_OK && mode != WIFI_MODE_NULL) { esp_wifi_stop(); esp_wifi_deinit(); Serial.println("ERROR unexpected Wi-Fi enabled; stopped"); }
}
void setup() {
  // No capture-light GPIO on Sense. In particular, GPIO21 is reserved for SD CS.
  bootResetReason = uint8_t(esp_reset_reason());
  uint32_t bootStrap = REG_READ(GPIO_STRAP_REG);
  initBootDiagnostic(bootResetReason, bootStrap);
  Serial.begin(115200); delay(200);
  Serial.println("BLE meter camera v2 ble-v3-s3-ov3660-sd-spi-bench168h; no automatic captures; BLE only; 660s hard / 45s idle");
  Serial.printf("BOOT_DIAGNOSTIC reset_reason=%u strap_register=0x%08lx serial_wait=none brownout_protection=unchanged current_counter=%u previous_counter=%u previous_stage=%u flags=%u\n", unsigned(bootResetReason), (unsigned long)bootStrap, bootDiagnostics.current.counter, bootDiagnostics.previous.counter, bootDiagnostics.previous.stage, bootDiagnostics.flags);
  Serial.printf("BLE_PEER allowed_central=%s\n", METER_ALLOWED_CENTRAL_MAC);
  bootCheckpoint(BOOT_STATE_INIT);
  // This firmware never initializes Wi-Fi. Stop/deinit also covers inherited SDK state.
  esp_wifi_stop(); esp_wifi_deinit(); reportWiFi();
  stateLock = xSemaphoreCreateMutex(); captureQueue = xQueueCreate(2, sizeof(CaptureRequest));
  if (!stateLock || !captureQueue) { Serial.println("FATAL state allocation failed"); return; }
  for (size_t i = 0; i < sizeof(testImage); ++i) testImage[i] = uint8_t(i);
  bootCheckpoint(BOOT_CAMERA_INIT); cameraOK = initCamera();
  bootCheckpoint(BOOT_POLICY_NVS);
  policyStoreReady = policyStore.begin("meter_bench_v2", false);
  if (policyStoreReady) {
    policy.lastTrustedUtc = policyStore.getUInt("floor", 0);
    policy.testExpired = policyStore.getBool("expired", false) || policy.lastTrustedUtc >= TEST_CUTOFF_UTC;
    policy.mode = policyStore.getUChar("mode", MODE_TEST_PHOTO);
    policy.run = policyStore.getUInt("run", DEFAULT_TEST_RUN);
    // An unexpected persisted policy blocks the test rather than extending it.
    if (policyStore.getUInt("cutoff", TEST_CUTOFF_UTC) != TEST_CUTOFF_UTC) policy.testExpired = true;
    if (!persistPolicy()) policyStoreReady = false;
  }
  Serial.printf("BENCH_POLICY durable=%u cutoff_utc=%u expired=%u mode=%u run=%u clock=untrusted_until_ble_sync retention_seconds=%u\n", policyStoreReady, TEST_CUTOFF_UTC, policy.testExpired, policy.mode, policy.run, TEST_RETENTION_SECONDS);
  bootCheckpoint(BOOT_SD_INIT); archiveBoot = esp_random(); archive.begin(archiveBoot);
  bootCheckpoint(BOOT_FLASH_INIT); initFlash();
  // NimBLE's BLEDevice::init owns controller + host initialization. Do not
  // prestart the controller: nimble_port_init requires the initial IDLE state.
  bootCheckpoint(BOOT_BLE_INIT);
  if (!BLEDevice::init("MeterCameraS3BLE")) {
    Serial.println("FATAL BLE init failed"); return;
  }
  bootCheckpoint(BOOT_BLE_SERVICE); BLEDevice::setMTU(LOCAL_ATT_MTU);
  BLEDevice::setSecurityCallbacks(new SecurityCallbacks());
  BLESecurity security;
  security.setCapability(ESP_IO_CAP_NONE);
  security.setKeySize(16);
  security.setAuthenticationMode(true, false, true); // Bonding + LE SC Just Works; no MITM.
  // In this SDK sm_sc_only also forces MITM level-4 ATT access, incompatible
  // with SC Just Works. Require verified SC bond + 16-byte encryption in the
  // AUTHOR callback instead; legacy/unverified links never access project GATT.
  ble_hs_cfg.sm_sc_only = 0;
  BLESecurity::setForceAuthentication(true);
  server = BLEDevice::createServer(); server->setCallbacks(new ServerCallbacks());
  BLEService *service = server->createService(SERVICE_UUID);
  auto *control = service->createCharacteristic(CONTROL_UUID, BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_ENC | BLECharacteristic::PROPERTY_WRITE_AUTHOR);
  auto *meta = service->createCharacteristic(METADATA_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_READ_ENC | BLECharacteristic::PROPERTY_READ_AUTHOR);
  auto *data = service->createCharacteristic(DATA_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_READ_ENC | BLECharacteristic::PROPERTY_READ_AUTHOR);
  auto *manifest = service->createCharacteristic(MANIFEST_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_READ_ENC | BLECharacteristic::PROPERTY_READ_AUTHOR);
  auto *health = service->createCharacteristic(HEALTH_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_READ_ENC | BLECharacteristic::PROPERTY_READ_AUTHOR);
  auto *bootDiagnostic = service->createCharacteristic(BOOT_DIAGNOSTIC_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_READ_ENC | BLECharacteristic::PROPERTY_READ_AUTHOR);
  control->setCallbacks(new ControlCallbacks()); meta->setCallbacks(new MetadataCallbacks()); data->setCallbacks(new DataCallbacks());
  manifest->setCallbacks(new ManifestCallbacks());
  health->setCallbacks(new HealthCallbacks());
  bootDiagnostic->setCallbacks(new BootDiagnosticCallbacks());
  service->start();
  BLEAdvertising *advertising = BLEDevice::getAdvertising();
  advertising->addServiceUUID(SERVICE_UUID); advertising->setScanResponse(true);
  advertising->setMinInterval(800); advertising->setMaxInterval(1600); // 0.5–1.0 s.
  advertising->setMinPreferred(24); advertising->setMaxPreferred(48); // 30–60 ms requested; central decides.
  bootCheckpoint(BOOT_ADVERTISING_REQUESTED); advertisingRecovery.request(uptimeMs());
  Serial.printf("BLE_READY address=%s offered_mtu=%u wire_version=%u chunk_bytes=%u max_frame=%u heap=%u psram_free=%u\n", BLEDevice::getAddress().toString().c_str(), LOCAL_ATT_MTU, VERSION, unsigned(CHUNK_BYTES), unsigned(MAX_IMAGE_BYTES), ESP.getFreeHeap(), ESP.getFreePsram());
}
void loop() {
  if (!server) { delay(1000); return; }
  // The pinned NimBLE advertising start returns status synchronously. Persist
  // the first observed active/connected startup from this loop only.
  if (!bootAdvertisingRecorded && startupAdvertisingSucceeded.load()) {
    bootCheckpoint(BOOT_ADVERTISING_ACTIVE); bootAdvertisingRecorded = true;
    Serial.printf("BOOT_STAGE advertising_active counter=%u flags=%u\n", bootDiagnostics.current.counter, bootDiagnostics.flags);
  }
  if (!bootAdvertisingRecorded && !bootAdvertisingExhaustedRecorded && lockState()) {
    bool exhausted = !connected && advertisingRecovery.requested && !advertisingRecovery.active &&
      advertisingRecovery.attempts >= AdvertisingRecovery::MAX_ATTEMPTS &&
      int32_t(uptimeMs() - advertisingRecovery.nextAttempt) >= 0;
    unlockState();
    if (exhausted) { bootCheckpoint(BOOT_ADVERTISING_EXHAUSTED); bootAdvertisingExhaustedRecorded = true; }
  }
  publishBootDiagnostic();
  bool newlyExpired = false;
  if (lockState()) {
    newlyExpired = policy.observe(uptime64());
    if (newlyExpired && policy.mode == MODE_TEST_PHOTO) {
      ++generation; clearImage(); metadata.state = ERROR; metadata.error = EXPIRED;
      disconnectPending = connected; disconnectAt = uptimeMs();
    }
    unlockState();
  }
  if (newlyExpired) {
    bool durable = persistPolicy();
    if (!durable) contextReady = false;
    Serial.printf("BENCH_ENDED cutoff_utc=%u observed_utc=%u durable=%u; awaiting_meter_installation\n", TEST_CUTOFF_UTC, policy.now(uptime64()), durable);
  }
  static uint32_t lastClockPersist = 0, lastMaintenance = 0, lastExpiryCheck = 0;
  uint32_t maintenanceNow = uptimeMs(), utc = policy.now(uptime64());
  if (utc && uint32_t(maintenanceNow - lastExpiryCheck) >= 1000) {
    lastExpiryCheck = maintenanceNow;
    if (uint32_t(maintenanceNow - lastMaintenance) >= 60000 ||
        (archive.nextExpiry && utc >= archive.nextExpiry)) {
      lastMaintenance = maintenanceNow;
      archive.maintain(utc, DEFAULT_TEST_RUN);
    }
  }
  if (utc && uint32_t(maintenanceNow - lastClockPersist) >= 3600000) {
    lastClockPersist = maintenanceNow;
    if (!persistPolicy()) contextReady = false;
  }
  CaptureRequest request;
  if (xQueueReceive(captureQueue, &request, 0) == pdTRUE) capture(request);
  uint16_t disconnectId = 0xffff; bool advertise = false;
  const char *disconnectReason = "release";
  uint32_t disconnectNow = 0, disconnectIdle = 0, disconnectSession = 0;
  uint32_t disconnectLastActivity = 0, disconnectSessionStart = 0;
  if (lockState()) {
    // Sample after acquiring the same lock used to update activity/connect
    // timestamps. An earlier sample could underflow after a concurrent callback.
    const uint32_t now = uptimeMs();
    bool hardLimit = elapsedAtLeast(now, sessionStart, CONNECTION_LIMIT_MS);
    bool idleLimit = elapsedAtLeast(now, lastActivity, IDLE_TIMEOUT_MS);
    if (connected && !disconnectIssued && (idleLimit || hardLimit)) {
      metadata.state = ERROR; metadata.error = EXPIRED; ++generation; clearImage();
      disconnectPending = true; disconnectAt = now;
      disconnectReason = hardLimit ? "hard_limit" : "idle";
    }
    if (connected && !disconnectIssued && disconnectPending && int32_t(now - disconnectAt) >= 0) {
      disconnectId = connectionId; disconnectPending = false; disconnectIssued = true;
      disconnectNow = now; disconnectIdle = now - lastActivity; disconnectSession = now - sessionStart;
      disconnectLastActivity = lastActivity; disconnectSessionStart = sessionStart;
    }
    advertise = advertisingRecovery.takeAttempt(now, connected);
    unlockState();
  }
  if (disconnectId != 0xffff) {
    Serial.printf("BLE session disconnect reason=%s uptime_ms=%u idle_ms=%u connected_ms=%u last_activity_ms=%u session_start_ms=%u\n", disconnectReason, disconnectNow, disconnectIdle, disconnectSession, disconnectLastActivity, disconnectSessionStart);
    server->disconnect(disconnectId);
  }
  if (advertise) {
    bool started = BLEDevice::getAdvertising()->start();
    bool active = BLEDevice::getAdvertising()->isAdvertising();
    xSemaphoreTake(stateLock, portMAX_DELAY);
    bool observed = active || connected;
    advertisingRecovery.completed(started && observed, started && observed ? 0 : -1);
    unlockState();
    if (started && observed) startupAdvertisingSucceeded.store(true);
    Serial.printf("BLE_ADVERTISING start_return=%u active=%u stack=NimBLE\n", started, active);
  }
  // Recover a stopped advertiser through the same bounded retry policy.
  bool actualAdvertising = BLEDevice::getAdvertising()->isAdvertising();
  if (lockState()) {
    if (!connected && advertisingRecovery.active && !actualAdvertising) advertisingRecovery.request(uptimeMs());
    unlockState();
  }
  uint32_t now = uptimeMs();
  static uint32_t lastDiagnostic = 0;
  if (uint32_t(now - lastDiagnostic) >= 60000) {
    lastDiagnostic = now; reportWiFi();
    xSemaphoreTake(stateLock, portMAX_DELAY);
    AdvertisingRecovery advSnapshot = advertisingRecovery;
    unlockState();
    Serial.printf("DIAGNOSTIC uptime_ms=%u heap=%u heap_min=%u psram_free=%u sd=%u sd_error=%u sd_files=%u sd_pending=%u sd_bytes=%u utc=%u cutoff=%u test_expired=%u context=%u next_sd_expiry=%u adv_active=%u adv_requested=%u adv_attempts=%u adv_status=%d flash_duty=%u\n", now, ESP.getFreeHeap(), ESP.getMinFreeHeap(), ESP.getFreePsram(), archive.ready, archive.lastError, archive.count, archive.pending, archive.bytes, policy.now(uptime64()), TEST_CUTOFF_UTC, policy.testExpired, contextReady, archive.nextExpiry, advSnapshot.active, advSnapshot.requested, advSnapshot.attempts, advSnapshot.lastStatus, 0U);
  }
  delay(10);
}
