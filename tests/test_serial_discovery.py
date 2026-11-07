import json
import tempfile
import unittest
from pathlib import Path

from config_manager import ConfigManager
from serial_discovery import build_port_configs, detect_serial_ports, discover_serial_ports


ROOT = Path(__file__).resolve().parent.parent


class SerialDiscoveryTests(unittest.TestCase):
    def test_proc_uart_discovery_excludes_active_console(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            dev = root / "dev"
            sysfs = root / "sys"
            dev.mkdir()
            (sysfs / "class" / "tty" / "console").mkdir(parents=True)
            (sysfs / "class" / "tty" / "console" / "active").write_text(
                "ttyS0\n", encoding="utf-8"
            )
            for name in ("ttyS0", "ttyS1", "ttyS2"):
                (dev / name).touch()
            proc = root / "serial"
            proc.write_text(
                "\n".join(
                    [
                        "serinfo:1.0 driver revision:",
                        "0: uart:TEST port:1000 irq:1",
                        "1: uart:TEST port:2000 irq:2",
                        "2: uart:TEST port:3000 irq:3",
                    ]
                ),
                encoding="utf-8",
            )

            ports = detect_serial_ports(
                dev_root=dev, sysfs_root=sysfs, proc_serial=proc
            )

            self.assertEqual([item["device"] for item in ports], [str(dev / "ttyS1"), str(dev / "ttyS2")])
            self.assertEqual([item["name"] for item in ports], ["COM1", "COM2"])

    def test_hardware_profile_overrides_auto_detection(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app_dir = Path(temp_dir)
            (app_dir / "hardware_profile.json").write_text(
                json.dumps(
                    {
                        "serial_ports": [
                            {"device": "/dev/ttyAMA2", "name": "RS485-A"},
                            {"device": "/dev/ttyAMA3", "name": "RS485-B"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            ports = discover_serial_ports(app_dir)

            self.assertEqual(
                [item["device"] for item in ports],
                ["/dev/ttyAMA2", "/dev/ttyAMA3"],
            )
            self.assertEqual([item["name"] for item in ports], ["RS485-A", "RS485-B"])

    def test_build_port_configs_preserves_existing_device_settings(self):
        defaults = json.loads(
            (ROOT / "default_config.json").read_text(encoding="utf-8")
        )
        template = defaults["ports"][0]
        existing = dict(template, device="/dev/ttyS2", baudrate=19200)
        discovered = [
            {"device": "/dev/ttyS2", "name": "COM1"},
            {"device": "/dev/ttyS1", "name": "COM2"},
        ]

        ports = build_port_configs(discovered, template, [existing])

        self.assertEqual([item["id"] for item in ports], [1, 2])
        self.assertEqual([item["device"] for item in ports], ["/dev/ttyS2", "/dev/ttyS1"])
        self.assertEqual(ports[0]["baudrate"], 19200)
        self.assertEqual(ports[1]["baudrate"], 9600)

    def test_config_accepts_variable_port_count(self):
        defaults = json.loads(
            (ROOT / "default_config.json").read_text(encoding="utf-8")
        )
        defaults["ports"] = defaults["ports"][:2]
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            default_path = temp / "default_config.json"
            config_path = temp / "config.json"
            default_path.write_text(json.dumps(defaults), encoding="utf-8")
            config_path.write_text(json.dumps(defaults), encoding="utf-8")

            manager = ConfigManager(config_path, default_path)

            self.assertEqual(len(manager.snapshot()["ports"]), 2)

    def test_fresh_config_uses_hardware_profile(self):
        defaults = json.loads(
            (ROOT / "default_config.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "default_config.json").write_text(
                json.dumps(defaults), encoding="utf-8"
            )
            (temp / "hardware_profile.json").write_text(
                json.dumps(
                    {
                        "serial_ports": [
                            {"device": "/dev/ttyAMA0", "name": "COM-A"},
                            {"device": "/dev/ttyAMA1", "name": "COM-B"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            manager = ConfigManager(
                temp / "config.json", temp / "default_config.json"
            )

            ports = manager.snapshot()["ports"]
            self.assertEqual([item["device"] for item in ports], ["/dev/ttyAMA0", "/dev/ttyAMA1"])
            self.assertEqual([item["name"] for item in ports], ["COM-A", "COM-B"])

    def test_reset_ports_from_hardware_preserves_matching_device_settings(self):
        defaults = json.loads(
            (ROOT / "default_config.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            (temp / "default_config.json").write_text(
                json.dumps(defaults), encoding="utf-8"
            )
            saved = json.loads(json.dumps(defaults))
            saved["ports"][0]["baudrate"] = 19200
            (temp / "config.json").write_text(json.dumps(saved), encoding="utf-8")
            (temp / "hardware_profile.json").write_text(
                json.dumps(
                    {
                        "serial_ports": [
                            {"device": "/dev/ttyS1", "name": "COM1"},
                            {"device": "/dev/ttyS2", "name": "COM2"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            manager = ConfigManager(
                temp / "config.json", temp / "default_config.json"
            )
            ports = manager.reset_ports_from_hardware()

            self.assertEqual(len(ports), 2)
            self.assertEqual(ports[0]["baudrate"], 19200)
            self.assertEqual(ports[0]["device"], "/dev/ttyS1")


if __name__ == "__main__":
    unittest.main()
