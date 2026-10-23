# Contributing

This project needs reproducible tests, diagnosis and practical tips. Anyone with a GitHub account can open an issue, comment on an existing issue, or propose a pull request through a fork. Direct write access is not needed.

- **Test result:** report successes as well as failures, including expected schedule slots and actual complete-photo receipts.
- **Problem:** identify the exact phase, firmware/receiver versions, power/reset conditions, and whether the boot changed.
- **Tip or proposal:** explain the mechanism, supporting measurements, tradeoffs and how to verify it. Clearly label an untested idea.
- **Code:** describe the trigger and resulting behavior, run relevant simulated tests, and state whether hardware testing was actually performed.

Use the issue templates. Before posting, remove names, addresses, Wi-Fi SSIDs/passwords, API keys, HA URLs/tokens, Bluetooth MACs, serial numbers, archive-owner IDs, location details and identifying images. Share small redacted excerpts instead of a full home-system backup. Synthetic examples should be labeled.

Helpful test reports include camera/sensor model, proxy model and antenna setup, power rating/cable and cold-vs-reset start, software versions, negotiated MTU, JPEG dimensions/bytes/checksum result, SD status, phase timings, expected/received slots, cleanup proof and whether the last visible photo is actually new. Use UTC or state the timezone explicitly.

Keep the original ESP32 cold-start issue separate from running transfer timeouts and S3 maintenance collisions. Do not describe a timeout as a crash without evidence. Do not disable encryption, widen allowed peers, autoformat the SD card or move expiry on retry as an unexplained workaround.

Project source is MIT licensed; the attributed native Home Assistant test fixture under `tests/fixtures` retains its separate Apache-2.0 license. By contributing original project code, you agree that it can be distributed under MIT. Respect upstream licenses. Keep discussion practical and respectful; a failed test is useful evidence.
