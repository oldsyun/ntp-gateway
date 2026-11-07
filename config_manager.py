"""Persistent configuration for the standalone NTP gateway."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from schedule_utils import ScheduleError, normalize_daily_time
from serial_discovery import build_port_configs, discover_serial_ports


class ConfigError(ValueError):
    pass


PASSWORD_ITERATIONS = 200_000
CURRENT_SCHEMA_VERSION = 4
DEFAULT_PASSWORD_ENV = "NTP_GATEWAY_DEFAULT_PASSWORD"
DEFAULT_PASSWORD_FILE = "default-password.txt"
ALLOWED_BAUDS = {1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200}
OBSOLETE_PORT_KEYS = {
    "align_minute",
    "millisecond_encoding",
    "minute_function",
    "minute_register",
    "minute_value_order",
    "sync_mode",
}


def hash_password(password: str, *, iterations: int = PASSWORD_ITERATIONS) -> str:
    if not 8 <= len(password) <= 128:
        raise ConfigError("password length must be 8..128")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    salt_text = base64.b64encode(salt).decode("ascii")
    digest_text = base64.b64encode(digest).decode("ascii")
    algorithm = "pbkdf2" + "_sha256"
    return algorithm + "$" + str(iterations) + "$" + salt_text + "$" + digest_text


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64, validate=True)
        expected = base64.b64decode(digest_b64, validate=True)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def generate_initial_password(length: int = 16) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#%+-_"
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(ch.isupper() for ch in value)
            and any(ch.islower() for ch in value)
            and any(ch.isdigit() for ch in value)
            and any(ch in "!@#%+-_" for ch in value)
        ):
            return value


def resolve_initial_password(config_dir: Path | str) -> str:
    configured = os.environ.get(DEFAULT_PASSWORD_ENV, "").strip()
    if configured:
        return configured
    path = Path(config_dir) / DEFAULT_PASSWORD_FILE
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if not 8 <= len(value) <= 128:
            raise ConfigError(f"{DEFAULT_PASSWORD_FILE} must contain 8..128 characters")
        return value
    return generate_initial_password()


def _deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _deep_update(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = _deep_copy(value)
    return base


class ConfigManager:
    def __init__(self, config_path: Path, default_path: Path):
        self.config_path = Path(config_path)
        self.default_path = Path(default_path)
        self._lock = threading.RLock()
        self._revision = 0
        self._config: Dict[str, Any] = {}
        self._load()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def _load(self) -> None:
        is_new_config = not self.config_path.exists()
        defaults = json.loads(self.default_path.read_text(encoding="utf-8"))
        if self.config_path.exists():
            user = json.loads(self.config_path.read_text(encoding="utf-8"))
            config = _deep_update(defaults, user)
        else:
            config = defaults
        if is_new_config:
            discovered = discover_serial_ports(self.config_path.parent)
            template = (config.get("ports") or [{}])[0]
            config["ports"] = build_port_configs(discovered, template)
        migrated = self._migrate(config)
        self._validate(config)
        self._config = config
        if not config["system"].get("password_hash"):
            password = resolve_initial_password(self.config_path.parent)
            config["system"]["password_hash"] = hash_password(password)
            config["system"]["must_change_password"] = True
            self._write_password_file(password)
            self._save_locked(config)
        elif migrated:
            self._save_locked(config)
        else:
            self.config_path.chmod(0o600)

    @staticmethod
    def _migrate(config: Dict[str, Any]) -> bool:
        changed = False
        try:
            schema_version = int(config.get("schema_version", 1))
        except (TypeError, ValueError):
            schema_version = 1
        if schema_version < 2:
            ntp = config.setdefault("ntp", {})
            ntp.setdefault("auto_compensate", True)
            changed = True
        if schema_version < 3:
            ntp = config.setdefault("ntp", {})
            ntp["auto_compensate"] = True
            ntp["slew_threshold_sec"] = 0.001
            ntp["step_threshold_sec"] = 0.05
            changed = True
        if schema_version < 4:
            for port in config.get("ports", []):
                if not isinstance(port, dict):
                    continue
                port.setdefault("schedule_mode", "interval")
                port.setdefault("daily_time", "00:00:00")
            changed = True
        if schema_version < CURRENT_SCHEMA_VERSION:
            config["schema_version"] = CURRENT_SCHEMA_VERSION
            changed = True
        ports = config.get("ports", [])
        if isinstance(ports, list):
            for port in ports:
                if not isinstance(port, dict):
                    continue
                for key in OBSOLETE_PORT_KEYS:
                    if key in port:
                        port.pop(key, None)
                        changed = True
        return changed

    def _write_password_file(self, password: str) -> None:
        path = self.config_path.parent / "initial-password.txt"
        path.write_text(
            "首次登录账号：{}\n首次登录密码：{}\n登录后请立即修改密码。\n".format(
                self._config.get("system", {}).get("admin_user", "admin"), password
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _save_locked(self, config: Dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config, ensure_ascii=False, indent=2)
        fd, temp_name = tempfile.mkstemp(
            prefix=".config-", suffix=".json", dir=str(self.config_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.config_path)
            self.config_path.chmod(0o600)
            try:
                dir_fd = os.open(self.config_path.parent, os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except (AttributeError, OSError):
                pass
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _validate(self, config: Dict[str, Any]) -> None:
        if not isinstance(config, dict):
            raise ConfigError("configuration root must be an object")

        system = config.get("system", {})
        port = int(system.get("http_port", 80))
        if not 1 <= port <= 65535:
            raise ConfigError("http_port must be 1..65535")
        if not str(system.get("admin_user", "")).strip():
            raise ConfigError("admin_user is required")

        ntp = config.get("ntp", {})
        servers = ntp.get("servers", [])
        if not isinstance(servers, list) or not 1 <= len(servers) <= 10:
            raise ConfigError("NTP servers must contain 1..10 entries")
        if any(not str(item).strip() for item in servers):
            raise ConfigError("NTP server entries cannot be empty")
        self._require_range(ntp, "sync_interval_sec", 5, 86400)
        self._require_range(ntp, "timeout_ms", 100, 10000)
        self._require_range(ntp, "max_age_sec", 30, 86400)
        self._require_range(ntp, "holdover_max_sec", 0, 86400)
        self._require_range(ntp, "max_step_sec", 1, 86400)
        try:
            slew_threshold = float(ntp.get("slew_threshold_sec", 0.001))
            step_threshold = float(ntp.get("step_threshold_sec", 0.05))
        except (TypeError, ValueError) as exc:
            raise ConfigError("NTP compensation thresholds must be numbers") from exc
        if not 0.0 <= slew_threshold <= 10.0:
            raise ConfigError("slew_threshold_sec must be 0..10")
        if not 0.0 <= step_threshold <= 10.0:
            raise ConfigError("step_threshold_sec must be 0..10")
        if slew_threshold > step_threshold:
            raise ConfigError("slew_threshold_sec must not exceed step_threshold_sec")

        schedule = config.get("schedule", {})
        self._require_range(schedule, "retry_count", 0, 10)
        self._require_range(schedule, "retry_interval_sec", 0, 300)

        ports = config.get("ports", [])
        if not isinstance(ports, list) or len(ports) > 32:
            raise ConfigError("serial ports must be a list of at most 32 entries")
        ids = [int(item.get("id", 0)) for item in ports]
        if sorted(ids) != list(range(1, len(ports) + 1)):
            raise ConfigError("serial port ids must be sequential starting at 1")
        devices = [str(item.get("device", "")) for item in ports]
        if len(set(devices)) != len(devices):
            raise ConfigError("serial devices must be unique")
        for item in ports:
            device = str(item.get("device", ""))
            if not device.startswith("/dev/"):
                raise ConfigError("serial device must be an absolute /dev node")
            if not str(item.get("name", "")).strip():
                raise ConfigError("serial port name is required")
            if int(item.get("baudrate", 0)) not in ALLOWED_BAUDS:
                raise ConfigError("unsupported baudrate")
            if int(item.get("databits", 0)) not in (7, 8):
                raise ConfigError("databits must be 7 or 8")
            if int(item.get("stopbits", 0)) not in (1, 2):
                raise ConfigError("stopbits must be 1 or 2")
            if str(item.get("parity", "")).upper() not in ("N", "E", "O"):
                raise ConfigError("parity must be N, E or O")
            slave = int(item.get("slave_addr", -1))
            if not 0 <= slave <= 247:
                raise ConfigError("slave_addr must be 0..247")
            if bool(item.get("broadcast")) and slave != 0:
                raise ConfigError("broadcast ports must use slave address 0")
            self._require_range(item, "interval_sec", 1, 86400)
            self._require_range(item, "response_timeout_ms", 50, 10000)
            self._require_range(item, "full_register", 400001, 499999)
            mode = str(item.get("schedule_mode", "interval")).lower()
            if mode not in ("interval", "daily"):
                raise ConfigError("schedule_mode must be interval or daily")
            try:
                item["schedule_mode"] = mode
                item["daily_time"] = normalize_daily_time(
                    item.get("daily_time", "00:00:00")
                )
            except ScheduleError as exc:
                raise ConfigError(str(exc)) from exc

    @staticmethod
    def _require_range(
        mapping: Dict[str, Any], key: str, minimum: int, maximum: int
    ) -> None:
        value = int(mapping.get(key, minimum - 1))
        if not minimum <= value <= maximum:
            raise ConfigError(f"{key} must be {minimum}..{maximum}")

    def snapshot(self, *, public: bool = False) -> Dict[str, Any]:
        with self._lock:
            result = _deep_copy(self._config)
        if public:
            result["system"].pop("password_hash", None)
            result["system"]["initial_password_file"] = "initial-password.txt"
        return result

    def port(self, port_id: int) -> Dict[str, Any]:
        with self._lock:
            for item in self._config["ports"]:
                if int(item["id"]) == int(port_id):
                    return _deep_copy(item)
        raise ConfigError(f"unknown port {port_id}")

    def reset_ports_from_hardware(self) -> list[Dict[str, Any]]:
        discovered = discover_serial_ports(self.config_path.parent)
        if not discovered:
            raise ConfigError("no serial ports detected on this hardware")
        with self._lock:
            defaults = json.loads(self.default_path.read_text(encoding="utf-8"))
            template = (
                (self._config.get("ports") or defaults.get("ports") or [{}])[0]
            )
            ports = build_port_configs(
                discovered, template, existing_ports=self._config.get("ports", [])
            )
            candidate = _deep_copy(self._config)
            candidate["ports"] = ports
            self._validate(candidate)
            self._save_locked(candidate)
            self._config = candidate
            self._revision += 1
            return _deep_copy(ports)

    def reset_admin_password(self, password: Optional[str] = None) -> None:
        with self._lock:
            value = password or resolve_initial_password(self.config_path.parent)
            candidate = _deep_copy(self._config)
            candidate["system"]["password_hash"] = hash_password(value)
            candidate["system"]["must_change_password"] = False
            self._validate(candidate)
            self._save_locked(candidate)
            self._config = candidate
            self._revision += 1
            self._write_password_file(value)

    def update_sections(self, incoming: Dict[str, Any], allowed: Iterable[str]) -> None:
        with self._lock:
            candidate = _deep_copy(self._config)
            for section in allowed:
                if section in incoming:
                    if section == "ports":
                        candidate["ports"] = _deep_copy(incoming["ports"])
                    elif section == "system":
                        existing_hash = candidate["system"].get("password_hash", "")
                        incoming_system = _deep_copy(incoming["system"])
                        incoming_system.pop("password_hash", None)
                        _deep_update(candidate["system"], incoming_system)
                        candidate["system"]["password_hash"] = existing_hash
                    else:
                        _deep_update(candidate[section], incoming[section])
            self._migrate(candidate)
            self._validate(candidate)
            self._save_locked(candidate)
            self._config = candidate
            self._revision += 1

    def set_password(self, old_password: str, new_password: str) -> None:
        with self._lock:
            encoded = self._config["system"].get("password_hash", "")
            if not verify_password(old_password, encoded):
                raise ConfigError("old password is incorrect")
            if old_password == new_password:
                raise ConfigError("new password must be different")
            self._config["system"]["password_hash"] = hash_password(new_password)
            self._config["system"]["must_change_password"] = False
            self._save_locked(self._config)
            self._revision += 1

    def verify_admin(self, username: str, password: str) -> bool:
        with self._lock:
            system = self._config["system"]
            return str(username) == str(system["admin_user"]) and verify_password(
                password, system.get("password_hash", "")
            )

    def export(self) -> Dict[str, Any]:
        with self._lock:
            result = _deep_copy(self._config)
        result["system"].pop("password_hash", None)
        return result

    def import_config(self, incoming: Dict[str, Any]) -> None:
        with self._lock:
            candidate = _deep_copy(incoming)
            candidate.setdefault("system", {})
            candidate["system"]["password_hash"] = self._config["system"].get(
                "password_hash", ""
            )
            self._migrate(candidate)
            self._validate(candidate)
            self._save_locked(candidate)
            self._config = candidate
            self._revision += 1
