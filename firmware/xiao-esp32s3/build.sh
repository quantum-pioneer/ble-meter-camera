#!/usr/bin/env bash
set -euo pipefail
firmware_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${ARDUINO_CLI:=arduino-cli}"
: "${BUILD_PATH:=$firmware_dir/build}"
cli_args=()
if [[ -n "${ARDUINO_CONFIG_FILE:-}" ]]; then cli_args+=(--config-file "$ARDUINO_CONFIG_FILE"); fi
if [[ ! -f "$firmware_dir/BleMeterCamera/local_config.h" ]]; then
  echo 'Create private BleMeterCamera/local_config.h from the example first.' >&2
  exit 2
fi
"$ARDUINO_CLI" "${cli_args[@]}" core list | awk '$1 == "esp32:esp32" && $2 == "3.3.10" {found=1} END {exit !found}' || {
  echo 'Install pinned esp32:esp32@3.3.10 before building.' >&2; exit 1;
}
mkdir -p "$BUILD_PATH" "$firmware_dir/bin"
"$ARDUINO_CLI" "${cli_args[@]}" compile \
  --fqbn 'esp32:esp32:XIAO_ESP32S3:CPUFreq=240,FlashMode=qio,FlashSize=8M,PSRAM=opi,PartitionScheme=default_8MB,USBMode=hwcdc,CDCOnBoot=default' \
  --build-property 'build.partitions=s3_preserved' \
  --build-property 'upload.maximum_size=1835008' \
  --build-path "$BUILD_PATH" "$firmware_dir/BleMeterCamera"
python3 "$firmware_dir/check_partition.py" "$BUILD_PATH"
cp -- "$BUILD_PATH/BleMeterCamera.ino.bin" "$firmware_dir/bin/BleMeterCamera.ino.bin"
(
  cd -- "$firmware_dir/bin"
  sha256sum BleMeterCamera.ino.bin > APPLICATION_SHA256SUMS
)
echo 'Built application only. This script never flashes a device.'
