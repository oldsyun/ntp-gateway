import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from config_manager import (
    DEFAULT_PASSWORD_ENV,
    DEFAULT_PASSWORD_FILE,
    ConfigManager,
    ConfigError,
    resolve_initial_password,
)


ROOT = Path(__file__).resolve().parent.parent


class InitialPasswordTests(unittest.TestCase):
    def test_password_file_is_used(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_PASSWORD_FILE
            path.write_text("FilePass123\n", encoding="utf-8")

            self.assertEqual(resolve_initial_password(temp_dir), "FilePass123")

    def test_environment_overrides_password_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_PASSWORD_FILE
            path.write_text("FilePass123\n", encoding="utf-8")
            environment_password = "Env" + "Pass" + "123"
            with mock.patch.dict(
                os.environ, {DEFAULT_PASSWORD_ENV: environment_password}, clear=False
            ):
                self.assertEqual(resolve_initial_password(temp_dir), environment_password)

    def test_invalid_password_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_PASSWORD_FILE
            path.write_text("short\n", encoding="utf-8")

            with self.assertRaises(ConfigError):
                resolve_initial_password(temp_dir)

    def test_random_password_is_generated_without_configuration(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with mock.patch.dict(os.environ, {}, clear=True):
                password = resolve_initial_password(temp_dir)

            self.assertEqual(len(password), 16)

    def test_reset_admin_password_updates_hash_and_password_file(self):
        defaults = json.loads(
            (ROOT / "default_config.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "default_config.json").write_text(
                json.dumps(defaults), encoding="utf-8"
            )
            (temp / "config.json").write_text(json.dumps(defaults), encoding="utf-8")
            manager = ConfigManager(
                temp / "config.json", temp / "default_config.json"
            )
            new_password = "Reset" + "Pass" + "123"

            manager.reset_admin_password(new_password)

            self.assertTrue(manager.verify_admin("admin", new_password))
            self.assertFalse(
                manager.snapshot()["system"]["must_change_password"]
            )
            self.assertIn(
                new_password,
                (temp / "initial-password.txt").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
