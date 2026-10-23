#pragma once
#include "SPI.h"
#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <string>
#include <type_traits>
#include <vector>

class String {
 public:
  std::string text;
  String() = default;
  String(const char *s) : text(s ? s : "") {}
  String(std::string s) : text(std::move(s)) {}
  template<class T, typename = std::enable_if_t<std::is_integral_v<T>>> String(T value) : text(std::to_string(value)) {}
  const char *c_str() const { return text.c_str(); }
  unsigned length() const { return unsigned(text.length()); }
  int lastIndexOf(char c) const { auto pos = text.rfind(c); return pos == std::string::npos ? -1 : int(pos); }
  String substring(unsigned start) const { return text.substr(start); }
  String substring(unsigned start, unsigned end) const { return text.substr(start, end - start); }
  bool startsWith(const String &prefix) const { return text.rfind(prefix.text, 0) == 0; }
  bool endsWith(const String &suffix) const { return text.length() >= suffix.text.length() && text.compare(text.length() - suffix.text.length(), suffix.text.length(), suffix.text) == 0; }
  friend String operator+(const String &a, const String &b) { return a.text + b.text; }
  friend bool operator==(const String &a, const String &b) { return a.text == b.text; }
  friend bool operator!=(const String &a, const String &b) { return !(a == b); }
};
inline constexpr int INPUT_PULLUP = 1, CARD_NONE = 0, FILE_READ = 0, FILE_WRITE = 1;
inline void pinMode(int, int) {}
struct FakeESP { uint64_t getEfuseMac() const { return 0xaabbccddeeffULL; } };
inline FakeESP ESP;
struct FakeSerial { template<class... T> void printf(const char *, T...) {} void println(const char *) {} };
inline FakeSerial Serial;
inline bool mockMountFailure = false, mockWriteFailure = false, mockReadCorruption = false;

class File {
  std::filesystem::path path_;
  std::shared_ptr<std::fstream> stream_;
  std::vector<std::filesystem::path> children_;
  unsigned next_ = 0;
  bool directory_ = false, valid_ = false;
 public:
  File() = default;
  explicit File(std::filesystem::path p, int mode = FILE_READ) : path_(std::move(p)) {
    directory_ = std::filesystem::is_directory(path_);
    if (directory_) {
      for (const auto &child : std::filesystem::directory_iterator(path_)) children_.push_back(child.path());
      std::sort(children_.begin(), children_.end()); valid_ = true;
    } else {
      auto flags = std::ios::binary | std::ios::in;
      if (mode == FILE_WRITE) flags |= std::ios::out | std::ios::trunc;
      stream_ = std::make_shared<std::fstream>(path_, flags); valid_ = stream_->is_open();
    }
  }
  explicit operator bool() const { return valid_; }
  File openNextFile() {
    while (next_ < children_.size()) {
      auto candidate = children_[next_++]; if (std::filesystem::exists(candidate)) return File(candidate);
    }
    return File();
  }
  String name() const { return path_.filename().string(); }
  bool isDirectory() const { return directory_; }
  uint64_t size() const { return valid_ && !directory_ ? std::filesystem::file_size(path_) : 0; }
  String readString() { return std::string(std::istreambuf_iterator<char>(*stream_), std::istreambuf_iterator<char>()); }
  size_t read(uint8_t *data, size_t n) {
    if (!valid_ || directory_) return 0;
    stream_->read(reinterpret_cast<char *>(data), std::streamsize(n)); size_t got = size_t(stream_->gcount());
    if (mockReadCorruption && got && path_.extension() == ".jpg") { data[0] ^= 1; mockReadCorruption = false; }
    return got;
  }
  size_t write(const uint8_t *data, size_t n) {
    if (!valid_ || directory_ || mockWriteFailure) return 0;
    stream_->write(reinterpret_cast<const char *>(data), std::streamsize(n)); return *stream_ ? n : 0;
  }
  void flush() { if (stream_) stream_->flush(); }
  void close() { if (stream_) stream_->close(); valid_ = false; }
};
class FakeSD {
 public:
  std::filesystem::path root;
  bool mounted = false;
  std::filesystem::path resolve(const char *p) const { return root / std::filesystem::path(p).relative_path(); }
  bool begin(uint8_t cs, SPIClass &spi, uint32_t hz, const char *mount, uint8_t files, bool format) { if (format || cs!=21 || hz!=4000000 || files!=5 || std::string(mount)!="/sdcard" || spi.sck!=7 || spi.miso!=8 || spi.mosi!=9 || spi.cs!=21) std::abort(); mounted = !mockMountFailure; if (mounted) std::filesystem::create_directories(root); return mounted; }
  int cardType() const { return mounted ? 1 : CARD_NONE; }
  void end() { mounted = false; }
  bool exists(const char *p) const { return mounted && std::filesystem::exists(resolve(p)); }
  bool mkdir(const char *p) { return mounted && std::filesystem::create_directories(resolve(p)); }
  File open(const char *p, int mode = FILE_READ) { return mounted ? File(resolve(p), mode) : File(); }
  bool rename(const char *a, const char *b) {
    // ESP-IDF VFS forwards to FatFs f_rename, which returns FR_EXIST instead of
    // the POSIX replace-existing behavior of std::filesystem::rename.
    if (!mounted || std::filesystem::exists(resolve(b))) return false;
    std::error_code ec; std::filesystem::rename(resolve(a), resolve(b), ec); return !ec;
  }
  bool remove(const char *p) { if (!mounted) return false; std::error_code ec; return std::filesystem::remove(resolve(p), ec) && !ec; }
  uint64_t totalBytes() const { return 1024ULL * 1024 * 1024; }
  uint64_t usedBytes() const { uint64_t total = 0; for (const auto &p : std::filesystem::recursive_directory_iterator(root)) if (p.is_regular_file()) total += p.file_size(); return total; }
};
inline FakeSD SD;
