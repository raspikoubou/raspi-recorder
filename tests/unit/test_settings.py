import json
import unittest
from pathlib import Path

from raspi_recorder.domain.camera import CameraSettings
from raspi_recorder.domain.capacity import BYTES_PER_GB
from raspi_recorder.domain.settings import ApplicationSettings


class ApplicationSettingsTests(unittest.TestCase):
    def test_defaults_match_the_initial_product_candidate(self):
        settings = ApplicationSettings()
        self.assertFalse(settings.power_saving_enabled)
        self.assertEqual(settings.camera(1).width, 1280)
        self.assertEqual(settings.camera(1).fps, 15)
        self.assertFalse(settings.camera(1).audio_enabled)
        self.assertTrue(settings.camera(1).overlay_enabled)

    def test_duplicate_usb_port_assignment_is_rejected(self):
        cameras = (
            CameraSettings(usb_port_key="platform-usb-1-1"),
            CameraSettings(usb_port_key="platform-usb-1-1"),
        )
        with self.assertRaises(ValueError):
            ApplicationSettings(cameras=cameras)

    def test_audio_interface_is_bound_to_the_same_usb_parent_port(self):
        valid = CameraSettings(
            usb_port_key="1-1.2",
            audio_interface_key="1-1.2/1-1.2:1.2/capture/0",
        )
        self.assertEqual(valid.audio_interface_key, "1-1.2/1-1.2:1.2/capture/0")
        with self.assertRaises(ValueError):
            CameraSettings(
                usb_port_key="1-1.2",
                audio_interface_key="1-1.3/1-1.3:1.2/capture/0",
            )

    def test_capacity_threshold_must_exceed_the_stop_floor(self):
        with self.assertRaises(ValueError):
            ApplicationSettings(auto_delete_start_tenths_gb=20)

    def test_capacity_validation_uses_actual_media_size(self):
        settings = ApplicationSettings()
        result = settings.validate_capacity(16 * BYTES_PER_GB)
        self.assertEqual(result.start_bytes, 5 * BYTES_PER_GB)
        self.assertEqual(result.recover_bytes, 7 * BYTES_PER_GB)
        with self.assertRaises(ValueError):
            settings.validate_capacity(6 * BYTES_PER_GB)

    def test_default_json_matches_schema_inputs_and_has_no_secrets(self):
        root = Path(__file__).parents[2]
        example = json.loads((root / "config/default-settings.json").read_text())
        schema = json.loads((root / "config/settings.schema.json").read_text())

        self.assertEqual(example["schema_version"], 2)
        self.assertIs(example["power_saving_enabled"], False)
        self.assertEqual([camera["camera_id"] for camera in example["cameras"]], [1, 2])
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertNotIn("password", json.dumps(example).lower())

    def test_settings_payload_round_trips_and_unknown_keys_are_rejected(self):
        original = ApplicationSettings(
            cameras=(
                CameraSettings(usb_port_key="platform-usb-1-1"),
                CameraSettings(),
            )
        )
        self.assertEqual(
            ApplicationSettings.from_payload(original.to_payload()), original
        )
        invalid = original.to_payload()
        invalid["unexpected"] = True
        with self.assertRaises(ValueError):
            ApplicationSettings.from_payload(invalid)

    def test_power_saving_is_optional_backward_compatible_and_typed(self):
        enabled = ApplicationSettings(power_saving_enabled=True)
        self.assertTrue(
            ApplicationSettings.from_payload(enabled.to_payload()).power_saving_enabled
        )

        legacy_payload = ApplicationSettings().to_payload()
        del legacy_payload["power_saving_enabled"]
        self.assertFalse(
            ApplicationSettings.from_payload(legacy_payload).power_saving_enabled
        )

        invalid_payload = ApplicationSettings().to_payload()
        invalid_payload["power_saving_enabled"] = 1
        with self.assertRaises(ValueError):
            ApplicationSettings.from_payload(invalid_payload)

    def test_default_settings_file_deserializes(self):
        root = Path(__file__).parents[2]
        example = json.loads((root / "config/default-settings.json").read_text())
        settings = ApplicationSettings.from_payload(example)
        self.assertEqual(settings, ApplicationSettings())

    def test_saved_v1_settings_keep_camera_storage_and_revision_values(self):
        legacy = json.loads(
            (Path(__file__).parents[1] / "fixtures/settings-v1.json").read_text()
        )
        legacy["revision"] = 23
        legacy["mode"] = "event"
        legacy["cameras"][0]["gpio_bcm"] = 17
        legacy["cameras"][0]["overlay_corner"] = "top_right"
        before = json.dumps(legacy, sort_keys=True)
        migrated = ApplicationSettings.from_saved_payload(legacy)
        payload = migrated.to_payload()
        self.assertEqual(migrated.revision, 23)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["cameras"][0]["overlay_corner"], "top_right")
        self.assertEqual(payload["volume"], legacy["volume"])
        self.assertNotIn("mode", payload)
        self.assertNotIn("gpio_bcm", payload["cameras"][0])
        self.assertEqual(json.dumps(legacy, sort_keys=True), before)

    def test_current_api_rejects_retired_settings_instead_of_enabling_them(self):
        payload = ApplicationSettings().to_payload()
        for key, value in (("mode", "event"), ("pre_record_seconds", 30)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                ApplicationSettings.from_payload({**payload, key: value})
        legacy = json.loads(
            (Path(__file__).parents[1] / "fixtures/settings-v1.json").read_text()
        )
        with self.assertRaises(ValueError):
            ApplicationSettings.from_payload(legacy)

    def test_saved_v1_rejects_unknown_fields_and_invalid_camera_arrays(self):
        payload = json.loads(
            (Path(__file__).parents[1] / "fixtures/settings-v1.json").read_text()
        )
        with self.assertRaises(ValueError):
            ApplicationSettings.from_saved_payload({**payload, "unexpected": True})
        with self.assertRaises(ValueError):
            ApplicationSettings.from_saved_payload({**payload, "cameras": []})


if __name__ == "__main__":
    unittest.main()
