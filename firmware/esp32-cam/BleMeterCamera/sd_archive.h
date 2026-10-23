#pragma once
#include <SD_MMC.h>
#include <esp_heap_caps.h>
#include <vector>
#include "archive_format.h"

// Only the sketch loop accesses FAT. BLE callbacks enqueue work.
class SDArchive {
 public:
  static constexpr uint32_t MAX_FILES = 1024;
  static constexpr uint32_t MAX_BYTES = 128U * 1024 * 1024;
  enum Failure : uint16_t { OK = 0, MOUNT = 1, OWNER = 2, CONTEXT = 3, CAPACITY = 4,
    CARD_FULL = 5, WRITE = 6, VERIFY = 7, MIGRATION = 8, CLOCK = 9, PURGE = 10 };
  bool ready = false;
  uint32_t count = 0, pending = 0, bytes = 0;
  uint32_t nextExpiry = 0;
  uint16_t lastError = MOUNT;
  void begin(uint32_t boot) {
    boot_ = boot;
    pinMode(13, INPUT_PULLUP); pinMode(15, INPUT_PULLUP); pinMode(2, INPUT_PULLUP);
    if (!SD_MMC.begin("/sdcard", true, false, 10000, 5) || SD_MMC.cardType() == CARD_NONE) {
      SD_MMC.end(); ready = false; lastError = MOUNT;
      Serial.println("SD_STATUS available=0 reason=mount_failed format=never; captures blocked"); return;
    }
    char directory[48], owner[80];
    snprintf(directory, sizeof(directory), "/ble-meter-camera-%012llx", (unsigned long long)(ESP.getEfuseMac() & 0xffffffffffffULL));
    snprintf(owner, sizeof(owner), "BLE_METER_CAMERA_ARCHIVE_V1 device=%012llx\n", (unsigned long long)(ESP.getEfuseMac() & 0xffffffffffffULL));
    dir_ = directory; owner_ = owner;
    if (!ensureDirectory(dir_, owner_)) {
      SD_MMC.end(); ready = false; lastError = OWNER;
      Serial.println("SD_STATUS available=0 reason=ownership_mismatch_or_write_failure; existing files preserved"); return;
    }
    ready = true; lastError = OK; scan(false, 0, 0);
    Serial.printf("SD_STATUS available=1 path=/sdcard%s mode=1bit frequency_khz=10000 format=never files=%u pending=%u bytes=%u cap=%u retention_seconds=%u\n", dir_.c_str(), count, pending, bytes, MAX_BYTES, meter::TEST_RETENTION_SECONDS);
  }
  // All owned TEST runs are pruned, even when the current mode is METER/expired.
  bool maintain(uint32_t utc, uint32_t legacyRun) {
    if (!utc) { lastError = CLOCK; return false; }
    if (!ready) begin(boot_);
    if (!ready) return false;
    if (!SD_MMC.exists((dir_ + "/OWNER.txt").c_str())) {
      ready = false; lastError = MOUNT; SD_MMC.end(); return false;
    }
    lastError = OK;
    scan(true, utc, legacyRun);
    return lastError == OK;
  }
  uint32_t store(const uint8_t *data, uint32_t length, uint32_t id, uint32_t captureMs,
                 uint32_t crc, uint32_t captureUtc, uint8_t mode, uint32_t run,
                 meter::ArchiveRecord &out) {
    if (!ready || !captureUtc || !run || length > meter::MAX_IMAGE_BYTES) { lastError = CONTEXT; return 0; }
    if (!maintain(captureUtc, run)) return 0;
    if (count >= MAX_FILES || uint64_t(bytes) + length + 64 > MAX_BYTES) {
      lastError = CAPACITY; Serial.println("SD_ARCHIVE blocked=capacity; unexpired records preserved"); return 0;
    }
    uint64_t total = SD_MMC.totalBytes(), used = SD_MMC.usedBytes();
    if (used >= total || total - used < length + 1024) { lastError = CARD_FULL; return 0; }
    String directory = runDirectory(mode, run);
    if (!ensureDirectory(directory, runOwner(mode, run)) || nextSequence_ == 0xffffffffU) { lastError = OWNER; return 0; }
    meter::ArchiveRecord rec;
    rec.sequence = ++nextSequence_; rec.boot = boot_; rec.captureMs = captureMs; rec.id = id;
    rec.length = length; rec.crc = crc; rec.capturedUtc = rec.retainedUtc = captureUtc; rec.run = run; rec.mode = mode;
    String base = path(directory, rec); uint8_t encoded[48]; rec.encode(encoded);
    if (!writeFile(base + ".meta.part", encoded, sizeof(encoded)) ||
        !writeFile(base + ".jpg.part", data, length) ||
        !writeFile(base + ".pending", reinterpret_cast<const uint8_t *>("pending\n"), 8) ||
        !SD_MMC.rename((base + ".meta.part").c_str(), (base + ".meta").c_str()) ||
        !SD_MMC.rename((base + ".jpg.part").c_str(), (base + ".jpg").c_str())) {
      for (const char *suffix : {".meta.part", ".jpg.part", ".pending", ".meta", ".jpg"}) SD_MMC.remove((base + suffix).c_str());
      lastError = WRITE; Serial.println("SD_ARCHIVE write_failed; capture blocked"); return 0;
    }
    meter::ArchiveRecord verified;
    if (!readRecord(directory, name(rec) + ".meta", verified) || verified.crc != crc || !verifyJpeg(base + ".jpg", rec)) {
      removeRecord(directory, rec); lastError = VERIFY;
      Serial.println("SD_ARCHIVE readback_failed; capture blocked"); return 0;
    }
    out = rec; scan(false, 0, 0); lastError = OK;
    Serial.printf("SD_ARCHIVE stored sequence=%u capture_utc=%u expires_utc=%u mode=%u run=%u bytes=%u crc32=%08x readback=verified pending=%u\n", rec.sequence, rec.capturedUtc, rec.expires(), mode, run, length, crc, pending);
    return rec.sequence;
  }
  bool restore(uint8_t *&data, meter::ArchiveRecord &rec, const meter::BenchPolicy &policy, uint64_t uptime) {
    uint32_t utc = policy.now(uptime);
    if (!maintain(utc, policy.run) || !policy.allowed(uptime)) return false;
    uint32_t upper = 0;
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
      String directory;
      if (!findRecord(true, rec, directory, policy.mode, policy.run, upper)) return false;
      upper = rec.sequence;
      if (rec.expiredUtc(utc) || !policy.canServe(rec.mode, rec.run, rec.expires(), uptime)) continue;
      File f = SD_MMC.open((path(directory, rec) + ".jpg").c_str(), FILE_READ);
      if (!f || f.size() != rec.length) { if (f) f.close(); lastError = VERIFY; continue; }
      uint8_t *buffer = static_cast<uint8_t *>(heap_caps_malloc(rec.length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
      if (!buffer) { f.close(); lastError = VERIFY; return false; }
      size_t got = f.read(buffer, rec.length); f.close();
      if (got != rec.length || meter::crc32(buffer, rec.length) != rec.crc ||
          buffer[0] != 0xff || buffer[1] != 0xd8 || buffer[rec.length - 2] != 0xff || buffer[rec.length - 1] != 0xd9) {
        heap_caps_free(buffer); lastError = VERIFY; continue;
      }
      data = buffer; lastError = OK; return true;
    }
    return false;
  }
  void acknowledge(uint32_t sequence) {
    if (!ready || !sequence) return;
    for (const String &directory : directories()) {
      File d = SD_MMC.open(directory.c_str()); File entry;
      while ((entry = d.openNextFile())) {
        String filename = entry.name(); entry.close(); meter::ArchiveRecord rec;
        if (readRecord(directory, filename, rec) && rec.sequence == sequence) {
          String marker = path(directory, rec) + ".pending";
          bool ok = !SD_MMC.exists(marker.c_str()) || SD_MMC.remove(marker.c_str());
          Serial.printf("SD_ACK sequence=%u delivered=%u capture_utc=%u expires_utc=%u\n", sequence, ok, rec.capturedUtc, rec.expires());
          d.close(); scan(false, 0, 0); return;
        }
      }
      d.close();
    }
  }
 private:
  String dir_, owner_;
  uint32_t boot_ = 0, nextSequence_ = 0;
  String leaf(String filename) { int slash = filename.lastIndexOf('/'); return slash >= 0 ? filename.substring(slash + 1) : filename; }
  String name(const meter::ArchiveRecord &rec) {
    char out[40]; snprintf(out, sizeof(out), "f%08lx-b%08lx-u%010lu", (unsigned long)rec.sequence, (unsigned long)rec.boot, (unsigned long)rec.captureMs); return out;
  }
  String path(const String &directory, const meter::ArchiveRecord &rec) { return directory + "/" + name(rec); }
  String runDirectory(uint8_t mode, uint32_t run) {
    char suffix[32]; snprintf(suffix, sizeof(suffix), "/%s-%08lx", mode == meter::MODE_TEST_PHOTO ? "test" : "meter", (unsigned long)run); return dir_ + suffix;
  }
  String runOwner(uint8_t mode, uint32_t run) {
    return owner_ + "BMC2 mode=" + String(mode) + " run=" + String(run) + "\n";
  }
  bool markerMatches(const String &directory, const String &expected) {
    File f = SD_MMC.open((directory + "/OWNER.txt").c_str(), FILE_READ);
    String contents = f && f.size() <= 160 ? f.readString() : String(); if (f) f.close();
    return contents == expected;
  }
  bool ensureDirectory(const String &directory, const String &expected) {
    if (SD_MMC.exists(directory.c_str())) return markerMatches(directory, expected);
    if (!SD_MMC.mkdir(directory.c_str())) return false;
    return writeFile(directory + "/OWNER.txt", reinterpret_cast<const uint8_t *>(expected.c_str()), expected.length());
  }
  std::vector<String> directories() {
    std::vector<String> out; out.push_back(dir_);
    File root = SD_MMC.open(dir_.c_str()); File entry;
    while ((entry = root.openNextFile())) {
      String filename = leaf(entry.name()); bool isDirectory = entry.isDirectory(); entry.close();
      if (!isDirectory) continue;
      uint8_t mode = filename.startsWith("test-") ? meter::MODE_TEST_PHOTO : filename.startsWith("meter-") ? meter::MODE_METER : 0;
      if (!mode) continue;
      unsigned prefix = mode == meter::MODE_TEST_PHOTO ? 5 : 6;
      if (filename.length() != prefix + 8) continue;
      char *end = nullptr; uint32_t run = strtoul(filename.substring(prefix).c_str(), &end, 16);
      // Verify canonical full path and an exact ownership marker before traversal.
      if (!run || runDirectory(mode, run) != dir_ + "/" + filename) continue;
      String candidate = dir_ + "/" + filename;
      if (markerMatches(candidate, runOwner(mode, run)) && out.size() < 64) out.push_back(candidate);
    }
    root.close(); return out;
  }
  bool ownedName(String filename, String &base, String &suffix) {
    filename = leaf(filename); if (filename.length() < 35) return false;
    base = filename.substring(0, 31); suffix = filename.substring(31);
    if (suffix != ".jpg" && suffix != ".meta" && suffix != ".pending" && suffix != ".jpg.part" && suffix != ".meta.part" && suffix != ".time" && suffix != ".time.part") return false;
    unsigned long sequence = 0, boot = 0, uptime = 0; int parsed = 0;
    if (sscanf(base.c_str(), "f%8lx-b%8lx-u%10lu%n", &sequence, &boot, &uptime, &parsed) != 3 || parsed != 31) return false;
    meter::ArchiveRecord rec; rec.sequence = sequence; rec.boot = boot; rec.captureMs = uptime;
    return sequence && base == name(rec);
  }
  bool writeFile(const String &filename, const uint8_t *data, size_t length) {
    File f = SD_MMC.open(filename.c_str(), FILE_WRITE); if (!f) return false;
    bool ok = f.write(data, length) == length; f.flush(); f.close(); return ok;
  }
  bool readRecord(const String &directory, String filename, meter::ArchiveRecord &rec) {
    filename = leaf(filename); if (filename.length() != 36 || !filename.endsWith(".meta")) return false;
    File f = SD_MMC.open((directory + "/" + filename).c_str(), FILE_READ); if (!f) return false;
    bool ok = false;
    if (f.size() == 48) { uint8_t encoded[48]; ok = f.read(encoded, sizeof(encoded)) == sizeof(encoded) && rec.decode(encoded); }
    else if (f.size() == 32 && directory == dir_) { uint8_t legacy[32]; ok = f.read(legacy, sizeof(legacy)) == sizeof(legacy) && rec.decodeLegacy(legacy); }
    f.close();
    if (!ok || filename != name(rec) + ".meta") return false;
    if (!rec.retainedUtc && directory == dir_ && SD_MMC.exists((path(directory, rec) + ".time").c_str())) {
      meter::ArchiveRecord adopted;
      if (!readTime(path(directory, rec) + ".time", rec, adopted)) { lastError = MIGRATION; return false; }
      rec = adopted;
    }
    return true;
  }
  bool sameCapture(const meter::ArchiveRecord &a, const meter::ArchiveRecord &b) {
    return a.sequence == b.sequence && a.boot == b.boot && a.captureMs == b.captureMs &&
           a.id == b.id && a.length == b.length && a.crc == b.crc;
  }
  bool readTime(const String &filename, const meter::ArchiveRecord &legacy, meter::ArchiveRecord &adopted) {
    File f = SD_MMC.open(filename.c_str(), FILE_READ); uint8_t encoded[48];
    bool ok = f && f.size() == sizeof(encoded) && f.read(encoded, sizeof(encoded)) == sizeof(encoded);
    if (f) f.close();
    return ok && adopted.decode(encoded) && sameCapture(legacy, adopted) &&
           adopted.mode == meter::MODE_TEST_PHOTO && adopted.fallback && !adopted.capturedUtc;
  }
  bool adoptLegacy(const String &directory, meter::ArchiveRecord &legacy, uint32_t utc, uint32_t run) {
    String base = path(directory, legacy), committed = base + ".time", staging = base + ".time.part";
    meter::ArchiveRecord adopted; bool haveBasis = false;
    // A failed old replace-style migration may have left a complete .meta.part.
    // Bind every common field and CRC before accepting its earlier fallback.
    for (const char *suffix : {".time.part", ".meta.part"}) {
      meter::ArchiveRecord candidate;
      if (readTime(base + suffix, legacy, candidate) && candidate.run == run &&
          uint64_t(candidate.retainedUtc) <= uint64_t(utc) + 5 &&
          (!haveBasis || candidate.retainedUtc < adopted.retainedUtc)) {
        adopted = candidate; haveBasis = true;
      }
    }
    if (!haveBasis) {
      adopted = legacy; adopted.retainedUtc = utc; adopted.run = run;
      adopted.mode = meter::MODE_TEST_PHOTO; adopted.fallback = true;
    }
    meter::ArchiveRecord staged;
    bool identicalStage = readTime(staging, legacy, staged) && staged.retainedUtc == adopted.retainedUtc && staged.run == adopted.run;
    if (!identicalStage) {
      if (SD_MMC.exists(staging.c_str()) && !SD_MMC.remove(staging.c_str())) return false;
      uint8_t encoded[48]; adopted.encode(encoded);
      if (!writeFile(staging, encoded, sizeof(encoded)) || !readTime(staging, legacy, staged)) return false;
    }
    // FatFs f_rename refuses an existing destination. Never replace/remove the
    // BMC1 original; commit an immutable sidecar only to an absent path.
    if (SD_MMC.exists(committed.c_str()) || !SD_MMC.rename(staging.c_str(), committed.c_str())) return false;
    meter::ArchiveRecord verified;
    if (!readTime(committed, legacy, verified) || verified.retainedUtc != adopted.retainedUtc) return false;
    legacy = verified;
    Serial.printf("SD_MIGRATE sequence=%u captured_utc=unknown retention_fallback_utc=%u run=%u legacy_meta=preserved sidecar=committed recovered_basis=%u\n", legacy.sequence, legacy.retainedUtc, legacy.run, haveBasis);
    return true;
  }
  bool verifyJpeg(const String &filename, const meter::ArchiveRecord &rec) {
    File f = SD_MMC.open(filename.c_str(), FILE_READ); if (!f || f.size() != rec.length) { if (f) f.close(); return false; }
    uint8_t block[512]; uint32_t crc = 0xffffffffU; size_t got = 0, total = 0;
    uint8_t first[2] = {}, last[2] = {};
    while ((got = f.read(block, sizeof(block)))) {
      for (size_t i = 0; i < got; ++i) {
        if (total < 2) first[total] = block[i];
        last[0] = last[1]; last[1] = block[i]; ++total;
        crc ^= block[i]; for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ ((crc & 1) ? 0xedb88320U : 0);
      }
      if (total > rec.length) break;
    }
    f.close(); return total == rec.length && (crc ^ 0xffffffffU) == rec.crc && first[0] == 0xff && first[1] == 0xd8 && last[0] == 0xff && last[1] == 0xd9;
  }
  bool removeRecord(const String &directory, const meter::ArchiveRecord &rec) {
    String base = path(directory, rec); bool ok = true;
    // Keep metadata until every photograph/staging/pending copy was removed.
    for (const char *suffix : {".jpg", ".jpg.part", ".pending", ".meta.part", ".time.part", ".time"})
      if (SD_MMC.exists((base + suffix).c_str()) && !SD_MMC.remove((base + suffix).c_str())) ok = false;
    if (ok && SD_MMC.exists((base + ".meta").c_str())) ok = SD_MMC.remove((base + ".meta").c_str());
    return ok;
  }
  bool findRecord(bool wantPending, meter::ArchiveRecord &out, String &outDir,
                  uint8_t mode, uint32_t run, uint32_t upper = 0) {
    bool found = false;
    for (const String &directory : directories()) {
      File d = SD_MMC.open(directory.c_str()); File entry;
      while ((entry = d.openNextFile())) {
        String filename = entry.name(); entry.close(); meter::ArchiveRecord rec;
        if (readRecord(directory, filename, rec) && rec.mode == mode && rec.run == run && rec.retainedUtc &&
            SD_MMC.exists((path(directory, rec) + ".jpg").c_str()) &&
            SD_MMC.exists((path(directory, rec) + ".pending").c_str()) == wantPending &&
            (!upper || rec.sequence < upper) && (!found || rec.sequence > out.sequence)) { out = rec; outDir = directory; found = true; }
      }
      d.close();
    }
    return found;
  }
  void scan(bool prune, uint32_t utc, uint32_t legacyRun) {
    count = pending = bytes = nextExpiry = 0;
    for (const String &directory : directories()) {
      bool testDirectory = directory == dir_ || directory.startsWith(dir_ + "/test-");
      // Recover time transactions before any .part pruning, independent of FAT
      // directory iteration order. The original 32-byte metadata stays intact.
      if (prune && directory == dir_ && utc && legacyRun) {
        File migration = SD_MMC.open(directory.c_str()); File candidate;
        while ((candidate = migration.openNextFile())) {
          String filename = leaf(candidate.name()); candidate.close();
          String base, suffix;
          if (!ownedName(filename, base, suffix) || suffix != ".meta") continue;
          meter::ArchiveRecord legacy;
          if (!readRecord(directory, filename, legacy)) { lastError = VERIFY; continue; }
          if (!legacy.retainedUtc && !adoptLegacy(directory, legacy, utc, legacyRun)) lastError = MIGRATION;
        }
        migration.close();
      }
      File d = SD_MMC.open(directory.c_str()); File entry;
      while ((entry = d.openNextFile())) {
        String filename = leaf(entry.name()); uint64_t size = entry.size(); bool isDirectory = entry.isDirectory(); entry.close();
        if (isDirectory || filename == "OWNER.txt") continue;
        String base, suffix; bool owned = ownedName(filename, base, suffix);
        meter::ArchiveRecord rec; bool recordRead = false;
        if (owned) {
          uint32_t sequence = strtoul(base.substring(1, 9).c_str(), nullptr, 16);
          if (sequence > nextSequence_) nextSequence_ = sequence;
          if (suffix == ".meta") {
            if (!readRecord(directory, filename, rec)) {
              lastError = VERIFY;
              bytes = uint32_t(std::min<uint64_t>(uint64_t(MAX_BYTES) + 1, uint64_t(bytes) + size));
              continue; // Unknown expiry: retain the original, report the gap.
            }
            recordRead = true;
          }
          bool preserveTimePart = false;
          if (directory == dir_ && (suffix == ".meta.part" || suffix == ".time.part")) {
            meter::ArchiveRecord original;
            preserveTimePart = SD_MMC.exists((directory + "/" + base + ".meta").c_str()) &&
              (!readRecord(directory, base + ".meta", original) || !original.retainedUtc);
          }
          // Only exact generated incomplete TEST files are removed. A staged
          // legacy fallback is preserved until its immutable .time is verified.
          if (prune && testDirectory && !preserveTimePart && (suffix.endsWith(".part") ||
              ((suffix == ".meta" || suffix == ".pending") && !SD_MMC.exists((directory + "/" + base + ".jpg").c_str())) ||
              (suffix == ".time" && !SD_MMC.exists((directory + "/" + base + ".meta").c_str())) ||
              (suffix == ".jpg" && !SD_MMC.exists((directory + "/" + base + ".meta").c_str())))) {
            if (!SD_MMC.remove((directory + "/" + filename).c_str())) lastError = PURGE;
            continue;
          }
          if (suffix == ".jpg") ++count;
          if (suffix == ".pending") ++pending;
        }
        bytes = uint32_t(std::min<uint64_t>(uint64_t(MAX_BYTES) + 1, uint64_t(bytes) + size));
        if (!recordRead && !readRecord(directory, filename, rec)) {
          if (owned && suffix == ".meta") lastError = VERIFY;
          continue;
        }
        if (testDirectory && rec.expires() && (!nextExpiry || rec.expires() < nextExpiry)) nextExpiry = rec.expires();
        if (prune && testDirectory && rec.expiredUtc(utc)) {
          bool removed = removeRecord(directory, rec); if (!removed) lastError = PURGE;
          Serial.printf("SD_PURGE sequence=%u mode=%u run=%u expires_utc=%u removed=%u\n", rec.sequence, rec.mode, rec.run, rec.expires(), removed);
        }
      }
      d.close();
    }
    if (prune) scan(false, 0, 0);
  }
};
