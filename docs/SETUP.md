# Bench setup and limitations

This is an experimental source snapshot, not a turnkey or production-ready integration. Use a separate bench installation and keep an SD backup before changing firmware. No meter disassembly is needed for this design.

**S3 prerequisite:** this preserved port requires your own pre-existing, correctly owned original-camera SD archive. It refuses fresh or mismatched cards; fresh-card provisioning for a new S3-only installation is not implemented. Do not copy another installation's owner ID to force adoption.

1. Choose **one** camera target: original ESP32-CAM / OV2640 or XIAO ESP32-S3 Sense / OV3660. Their pin maps and build options differ. Follow [firmware/README.md](../firmware/README.md), supply private local configuration, and build the matching target. Published sources are sanitized derivatives of the tested sources; no prebuilt image is supplied.
2. Configure the dedicated **XIAO ESP32-C6** using `esphome/meter-ble-receiver.yaml` and a private `secrets.yaml`. The recorded proxy used ESPHome 2026.9.1 and ESP-IDF 5.5.5. Its template enables the onboard antenna using GPIO3/GPIO14 LOW and one active BLE connection slot. Do not copy these pins to unrelated proxy hardware. The camera uses BLE; the proxy uses its network connection to HA.
3. Add the proxy through Home Assistant's ESPHome integration with its encrypted API. Obtain the camera BLE address and the dedicated proxy scanner-source address locally. They are different roles. Never post the actual addresses in an issue.
4. Install **either** the root `custom_components/ble_meter_camera` (receiver 0.5.12, S3 checkpoint) **or** `historical/receiver-0.5.7/custom_components/ble_meter_camera` (original-camera checkpoint) into your HA configuration `custom_components` directory. Do not install both copies under the same domain. Restart HA and add **BLE Meter Camera** in Devices & services. Collection starts disabled.
5. Enter your camera address and dedicated C6 source address. The implementation pins and verifies this proxy route. For S3, while both collection controls are OFF and all clients/maintenance have stopped, use Reconfigure to select **XIAO ESP32-S3 Sense OV3660** with the same verified camera address and capture light **0**. The initial setup flow defaults to original-camera model, so this S3 step matters.
6. Review image options. The tested final photos were 1600×1200 (frame code 13), within a 131072-byte maximum; initial defaults are lower resolution. Test optical quality and JPEG size rather than assuming every scene fits. Original-camera optional illumination was bounded; S3 has no configured capture flash and shares SD CS with a board LED, so keep its light at zero.
7. Review the fixed historical cutoff in receiver `const.py` and the matching firmware trial configuration. After October 24, 2026 at 19:25 UTC, the published receiver's TEST_PHOTO run will not start. A new run requires a reviewed code/configuration change across both ends; no automatic extension is provided. The clock policy rejects implausible or discontinuous host time. Do not bypass it to make an expired run appear active.
8. Enable collection and periodic collection only for a deliberately configured trial. Verify a **new complete JPEG**, its capture and receipt times, SD commitment and HA persistence. Then test true independent cold power, a proxy interruption, repeated schedule slots, and finally the real meter position. Check missed slots even if the failure counter is unchanged.

The test policy retains photos for 168 hours from original trusted capture time, with an immutable fallback for legacy unknown-time records. Retrying must not reset expiry. Neither future expiry nor the fixed cutoff was established as a completed physical test by this historical record. These policies and the SD owner/run controls need review before reusing a card from another run; never format an existing card as an automatic recovery step.

The original receiver checkpoint used a 60-second READY budget. S3 receiver 0.5.12 uses S3-specific CONFIG 120 s, READY 120 s, maintenance 170 s; individual GATT operations remain 5 s, overall collection 600 s and the camera hard session 660 s. These deadlines cover different phases. Increasing all of them would hide evidence and is not an established fix.

## Local checks

Run existing simulated tests in an environment containing `voluptuous` and Pillow:

```bash
python3 -m unittest discover -s tests -v
```

Run the original receiver suite independently from `historical/receiver-0.5.7` with the same command. Firmware host tests and target build instructions are in the firmware guide. Tests use fakes/stubs and cannot establish BLE range, cold-start behavior, actual SD latency or meter readability. See [validation](VALIDATION.md) for checks performed on this publication.
