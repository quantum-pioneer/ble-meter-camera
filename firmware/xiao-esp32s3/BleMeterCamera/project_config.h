#pragma once
// Host fixtures use synthetic identities; Arduino builds always require private configuration.
#if defined(METER_HOST_TEST) && !defined(ARDUINO)
#include "../tests/test_config.h"
#else
#if __has_include("local_config.h")
#include "local_config.h"
#else
#error "Copy local_config.example.h to local_config.h and configure this installation first"
#endif
#endif
#ifndef METER_ALLOWED_CENTRAL_MAC
#error "Set the dedicated proxy Bluetooth MAC in local_config.h"
#endif
#ifndef METER_TEST_CUTOFF_UTC
#error "Set an explicit test cutoff in local_config.h"
#endif
#ifndef METER_DEFAULT_TEST_RUN
#error "Set a nonzero test run ID in local_config.h"
#endif
static_assert(sizeof(METER_ALLOWED_CENTRAL_MAC) == 18, "Proxy MAC must use XX:XX:XX:XX:XX:XX notation");
static_assert(METER_TEST_CUTOFF_UTC >= 1792000000U && METER_TEST_CUTOFF_UTC <= 4102444800U, "Set an explicit supported UTC cutoff");
static_assert(METER_DEFAULT_TEST_RUN != 0, "Set a nonzero test run ID");
