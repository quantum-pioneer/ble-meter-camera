# Work remaining

- [ ] Diagnose original ESP32-CAM cold-start silence with measured power/reset/startup evidence.
- [ ] Reproduce original running transfer stalls with phase-level timing and reset/boot evidence.
- [ ] Fix S3 maintenance/capture scheduling collision and count every missed slot explicitly.
- [ ] Measure SD scan and clock/policy synchronization phases; explain the remaining timeouts.
- [ ] Repeat independent-power and proxy-interruption tests across consecutive scheduled slots.
- [ ] Complete an unattended endurance run with expected-vs-received slot accounting.
- [ ] Make historical bench policy/new-trial configuration easier to reproduce without weakening expiry or clock protections.
- [ ] Validate the actual meter position: BLE/proxy network route, focus, glare, light, display-page cycle and all required digits.
- [ ] Only after reliable legible photographs, design and validate meter-reading extraction and usage calculations.

Success means a new valid persisted photograph for every required slot, reliable independent cold start and bounded recovery, with honest accounting for interruptions and missed slots. An advertisement, a successful compile, or the last old picture remaining on a dashboard is not sufficient.
