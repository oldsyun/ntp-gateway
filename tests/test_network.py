import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import network_manager
from network_manager import NetworkManager, netmask_to_prefix, prefix_to_netmask


class NetworkHelperTests(unittest.TestCase):
    def test_netmask_roundtrip(self):
        self.assertEqual(netmask_to_prefix("255.255.255.0"), 24)
        self.assertEqual(prefix_to_netmask(24), "255.255.255.0")
        self.assertEqual(netmask_to_prefix("255.255.0.0"), 16)

    def test_baseline_persists_live_prefix_without_losing_netmask(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            conf = {
                "eth0": root / "net.conf",
                "eth1": root / "net2.conf",
            }
            live = {
                "eth0": {
                    "ip": "192.168.50.138",
                    "prefix": 24,
                    "gateway": "192.168.50.1",
                },
                "eth1": {
                    "ip": "192.168.0.230",
                    "prefix": 24,
                    "gateway": "",
                },
            }
            with mock.patch.object(network_manager, "NET_CONF", conf), mock.patch.object(
                NetworkManager, "live", side_effect=lambda name: dict(live[name])
            ), mock.patch.object(
                NetworkManager, "dns_servers", return_value=["114.114.114.114"]
            ):
                NetworkManager(root, logging.getLogger("test-network"))

            eth0 = conf["eth0"].read_text(encoding="utf-8")
            self.assertIn("IPADDR=192.168.50.138", eth0)
            self.assertIn("NETMASK=255.255.255.0", eth0)
            self.assertIn("GATEWAY=192.168.50.1", eth0)
            self.assertTrue((root / ".network-baseline-v2").exists())

    def test_same_gateway_does_not_add_temporary_route(self):
        manager = object.__new__(NetworkManager)
        manager._ip = "ip"
        old = {"gateway": "192.168.50.1"}
        new = {"gateway": "192.168.50.1"}
        with mock.patch.object(manager, "_run_ignore") as run_ignore, mock.patch.object(
            manager, "_run"
        ) as run:
            manager._commit_static_route("eth0", old, new)
        run_ignore.assert_not_called()
        run.assert_not_called()

    def test_new_gateway_commit_replaces_route_and_removes_temporary_route(self):
        manager = object.__new__(NetworkManager)
        manager._ip = "ip"
        old = {"gateway": "192.168.50.1"}
        new = {"gateway": "10.0.0.1"}
        with mock.patch.object(manager, "_run_ignore") as run_ignore, mock.patch.object(
            manager, "_run"
        ) as run:
            manager._commit_static_route("eth0", old, new)
        self.assertEqual(run_ignore.call_count, 1)
        self.assertIn("metric", run_ignore.call_args.args[0])
        self.assertEqual(
            run_ignore.call_args.args[0][-1],
            "0",
        )
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][-1], "50")

    def test_busybox_link_flags_are_parsed_correctly(self):
        manager = object.__new__(NetworkManager)
        manager._ip = "ip"

        def fake_run(args, timeout=10):
            command = " ".join(args)
            if "addr show" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout="2: eth0    inet 192.168.50.138/24 scope global eth0\n",
                )
            if "route show" in command:
                return SimpleNamespace(
                    returncode=0, stdout="default via 192.168.50.1 dev eth0\n"
                )
            if "link show" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout="2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500\n",
                )
            return SimpleNamespace(returncode=0, stdout="")

        with mock.patch.object(manager, "_run", side_effect=fake_run):
            status = manager.live("eth0")
        self.assertTrue(status["up"])
        self.assertTrue(status["carrier"])
        self.assertEqual(status["ip"], "192.168.50.138")
        self.assertEqual(status["addresses"][0]["prefix"], 24)


if __name__ == "__main__":
    unittest.main()
