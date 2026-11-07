#!/usr/bin/env python3
"""Standalone NTP-to-RS485 gateway service."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config_manager import ConfigError, ConfigManager
from network_manager import NetworkManager
from ntp_client import NtpClient
from port_worker import PortWorker
from serial_discovery import discover_serial_ports
from web_server import APP_VERSION, GatewayApplication, GatewayHTTPServer


def build_logger(app_dir: Path, config: ConfigManager) -> logging.Logger:
    log_dir = app_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    system = config.snapshot()["system"]
    logger = logging.getLogger("ntp-gateway")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_dir / "gateway.log",
        maxBytes=int(system.get("log_max_bytes", 1048576)),
        backupCount=int(system.get("log_backup_count", 5)),
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    logger.addHandler(console)
    return logger


def apply_timezone(config: ConfigManager) -> str:
    timezone = str(config.snapshot()["system"].get("timezone", "Asia/Shanghai"))
    zoneinfo = Path("/usr/share/zoneinfo") / timezone
    if zoneinfo.exists():
        os.environ["TZ"] = timezone
        source = f"zoneinfo:{timezone}"
    else:
        os.environ.pop("TZ", None)
        source = "system:/etc/localtime"
    if hasattr(time, "tzset"):
        time.tzset()
    return source


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone NTP to RS485 gateway")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="configuration file path",
    )
    parser.add_argument("--check", action="store_true", help="validate and exit")
    parser.add_argument(
        "--list-serial",
        action="store_true",
        help="print serial ports detected from the hardware and exit",
    )
    parser.add_argument(
        "--reset-ports",
        action="store_true",
        help="replace the serial port list with detected hardware ports and exit",
    )
    parser.add_argument(
        "--reset-password",
        action="store_true",
        help="reset the admin password from default-password.txt/env and exit",
    )
    parser.add_argument("--version", action="store_true", help="print version")
    args = parser.parse_args()
    if args.version:
        print(APP_VERSION)
        return 0

    app_dir = Path(__file__).resolve().parent
    if args.list_serial:
        print(json.dumps(discover_serial_ports(app_dir), ensure_ascii=False, indent=2))
        return 0
    config = ConfigManager(Path(args.config), app_dir / "default_config.json")
    if args.reset_ports:
        ports = config.reset_ports_from_hardware()
        print("ports:", ", ".join(str(item["id"]) for item in ports))
        for item in ports:
            print(f"{item['name']}: {item['device']}")
        return 0
    if args.reset_password:
        config.reset_admin_password()
        print("admin password reset; see initial-password.txt")
        return 0
    timezone_source = apply_timezone(config)
    logger = build_logger(app_dir, config)
    logger.info(
        "Timezone configured=%s source=%s local=%s",
        config.snapshot()["system"].get("timezone"),
        timezone_source,
        time.strftime("%Y-%m-%d %H:%M:%S %z %Z"),
    )
    if args.check:
        print("configuration valid")
        print("ports:", ", ".join(str(item["id"]) for item in config.snapshot()["ports"]))
        return 0

    logger.info("Starting NTP gateway version %s", APP_VERSION)
    ntp = NtpClient(config, logger)
    network = NetworkManager(app_dir, logger)
    port_ids = [int(item["id"]) for item in config.snapshot()["ports"]]
    workers = {
        port_id: PortWorker(port_id, config, ntp, logger)
        for port_id in port_ids
    }
    application = GatewayApplication(app_dir, config, ntp, network, workers, logger)
    system = config.snapshot()["system"]
    web = GatewayHTTPServer(
        application,
        str(system.get("listen_host", "0.0.0.0")),
        int(system.get("http_port", 80)),
    )

    stop_event = threading.Event()

    def request_stop(signum=None, _frame=None):
        logger.info("Stop requested signal=%s", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        ntp.start()
        for worker in workers.values():
            worker.start()
        web.start()
        while not stop_event.wait(1.0):
            pass
    except Exception:
        logger.exception("Gateway failed")
        return 1
    finally:
        logger.info("Stopping NTP gateway")
        web.stop()
        for worker in workers.values():
            worker.stop()
        ntp.stop()
        logging.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
