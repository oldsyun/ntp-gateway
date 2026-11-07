import json
import tempfile
import unittest
from pathlib import Path

from config_manager import ConfigManager, OBSOLETE_PORT_KEYS


class ConfigMigrationTests(unittest.TestCase):
    def test_obsolete_split_sync_fields_are_removed_on_load(self):
        root = Path(__file__).resolve().parent.parent
        defaults = json.loads(
            (root / "default_config.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            default_path = temp / "default_config.json"
            config_path = temp / "config.json"
            default_path.write_text(
                json.dumps(defaults, ensure_ascii=False), encoding="utf-8"
            )
            for port in defaults["ports"]:
                for key in OBSOLETE_PORT_KEYS:
                    port[key] = "legacy"
            config_path.write_text(
                json.dumps(defaults, ensure_ascii=False), encoding="utf-8"
            )

            manager = ConfigManager(config_path, default_path)
            for port in manager.snapshot()["ports"]:
                self.assertTrue(OBSOLETE_PORT_KEYS.isdisjoint(port))

            persisted = json.loads(config_path.read_text(encoding="utf-8"))
            for port in persisted["ports"]:
                self.assertTrue(OBSOLETE_PORT_KEYS.isdisjoint(port))

    def test_schema_v1_enables_automatic_compensation(self):
        root = Path(__file__).resolve().parent.parent
        defaults = json.loads(
            (root / "default_config.json").read_text(encoding="utf-8")
        )
        defaults["schema_version"] = 1
        defaults["ntp"]["step_threshold_sec"] = 0.05
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            default_path = temp / "default_config.json"
            config_path = temp / "config.json"
            default_path.write_text(
                json.dumps(defaults, ensure_ascii=False), encoding="utf-8"
            )
            config_path.write_text(
                json.dumps(defaults, ensure_ascii=False), encoding="utf-8"
            )

            manager = ConfigManager(config_path, default_path)
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["schema_version"], 4)
            self.assertEqual(snapshot["ntp"]["step_threshold_sec"], 0.05)
            self.assertEqual(snapshot["ntp"]["slew_threshold_sec"], 0.001)
            self.assertTrue(snapshot["ntp"]["auto_compensate"])
            for port in snapshot["ports"]:
                self.assertEqual(port["schedule_mode"], "interval")
                self.assertEqual(port["daily_time"], "00:00:00")


if __name__ == "__main__":
    unittest.main()
