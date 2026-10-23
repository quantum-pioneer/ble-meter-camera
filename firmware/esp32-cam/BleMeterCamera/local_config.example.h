#pragma once
// Private installation settings: this file is a template, not a usable configuration.
// Copy to local_config.h, replace ALL placeholders, then remove the #error below.
#error "Configure your local proxy, cutoff and run ID before building"
#define METER_ALLOWED_CENTRAL_MAC "REPLACE_WITH_PROXY_BLUETOOTH_MAC"
// Unix UTC seconds. Match the Home Assistant receiver configuration.
// Historical bench cutoff was 1792869900 (2026-10-24 19:25:00 UTC); do not reuse it.
#define METER_TEST_CUTOFF_UTC 0U
#define METER_DEFAULT_TEST_RUN 0U
