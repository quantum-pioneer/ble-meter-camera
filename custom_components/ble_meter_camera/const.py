"""Integration constants for the dedicated XIAO ESP32-C6 receiver."""
DOMAIN = "ble_meter_camera"
DEFAULT_CAMERA_MODEL = "ESP32-CAM OV2640"
S3_CAMERA_MODEL = "XIAO ESP32-S3 Sense OV3660"
CAMERA_MODELS = (DEFAULT_CAMERA_MODEL, S3_CAMERA_MODEL)
PLATFORMS = ["camera", "sensor", "switch", "button"]
LOCAL_TIMEZONE = "America/Chicago"
BENCH_CUTOFF = "2026-10-24T19:25:00+00:00"
TEST_RUN_ID = "calendar-20261021"
TEST_RUN_CODE = 20261021
TEST_RETENTION_SECONDS = 168 * 3600
BENCH_SECONDS = 3 * 86400  # Legacy import compatibility; never used to extend this run.
DEFAULTS = {"interval": 900, "frame_size": 5, "quality": 20, "capture_light_percent": 0,
            "max_bytes": 131072, "chunk_pause": 0.02,
            "history_days": 7, "history_max_bytes": 128 * 1024 * 1024,
            "mode": "TEST_PHOTO"}
