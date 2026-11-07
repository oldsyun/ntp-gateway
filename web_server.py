"""Web UI and REST API for the standalone NTP gateway."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import platform
import re
import socket
import threading
import time
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlsplit

from config_manager import ConfigError, ConfigManager
from network_manager import NetworkManager
from ntp_client import NtpClient
from port_worker import PortWorker


APP_VERSION = "0.1.0"


class SessionStore:
    def __init__(self, ttl_sec: int):
        self.ttl_sec = ttl_sec
        self._sessions: Dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        with self._lock:
            self._sessions[token] = time.monotonic() + self.ttl_sec
        return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        now = time.monotonic()
        with self._lock:
            expires = self._sessions.get(token)
            if not expires or expires <= now:
                self._sessions.pop(token, None)
                return False
            self._sessions[token] = now + self.ttl_sec
            return True

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()


class LoginLimiter:
    def __init__(self, max_failures: int = 5, window_sec: int = 300):
        self.max_failures = max_failures
        self.window_sec = window_sec
        self._failures: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def blocked(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            values = self._failures.get(address)
            if not values:
                return False
            while values and now - values[0] > self.window_sec:
                values.popleft()
            if not values:
                self._failures.pop(address, None)
                return False
            return len(values) >= self.max_failures

    def failed(self, address: str) -> None:
        with self._lock:
            self._failures.setdefault(address, deque()).append(time.monotonic())

    def success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


class GatewayApplication:
    def __init__(
        self,
        app_dir: Path,
        config: ConfigManager,
        ntp: NtpClient,
        network: NetworkManager,
        workers: Dict[int, PortWorker],
        logger: logging.Logger,
    ):
        self.app_dir = app_dir
        self.config = config
        self.ntp = ntp
        self.network = network
        self.workers = workers
        self.logger = logger
        self.web_dir = app_dir / "web"
        self.log_file = app_dir / "logs" / "gateway.log"
        system = config.snapshot()["system"]
        self.sessions = SessionStore(int(system.get("session_ttl_sec", 28800)))
        self.limiter = LoginLimiter()
        self.started_mono = time.monotonic()

    def status(self) -> Dict[str, Any]:
        config = self.config.snapshot(public=True)
        return {
            "time": {
                "local": datetime.now().isoformat(timespec="milliseconds"),
                "utc": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "epoch": round(time.time(), 3),
                "timezone": config["system"].get("timezone"),
            },
            "ntp": self.ntp.status(),
            "ports": [
                self.workers[port_id].status() for port_id in sorted(self.workers)
            ],
            "schedule": config["schedule"],
            "network": self.network.status(),
            "system": self._system_info(config),
            "alerts": [
                "串口默认禁用，启用前必须核对现场从站和 P3 报文。",
                "分对时功能已移除；所有端口固定发送全对时报文，毫秒字段固定为 0。",
                "定时发送按 epoch 整秒边界对齐，报文时间取触发秒。",
            ],
        }

    def _system_info(self, config: Dict[str, Any]) -> Dict[str, Any]:
        uptime = None
        try:
            with open("/proc/uptime", "r", encoding="ascii") as handle:
                uptime = round(float(handle.read().split()[0]), 1)
        except (OSError, ValueError):
            pass
        return {
            "app_version": APP_VERSION,
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "uptime_sec": uptime,
            "service_uptime_sec": round(time.monotonic() - self.started_mono, 1),
            "http_port": config["system"].get("http_port"),
            "initial_password_file": "initial-password.txt",
        }

    def login(self, payload: Dict[str, Any], address: str) -> Dict[str, Any]:
        if self.limiter.blocked(address):
            raise ConfigError("too many failed login attempts; try again later")
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
        if not self.config.verify_admin(username, password):
            self.limiter.failed(address)
            raise ConfigError("invalid username or password")
        self.limiter.success(address)
        token = self.sessions.create()
        system = self.config.snapshot()["system"]
        return {
            "token": token,
            "username": system["admin_user"],
            "must_change_password": bool(system.get("must_change_password")),
        }

    def tokens_from_headers(self, headers) -> str:
        value = headers.get("Authorization", "")
        if value.lower().startswith("bearer "):
            return value[7:].strip()
        return headers.get("token", "").strip()

    def api(
        self,
        method: str,
        path: str,
        query: Dict[str, list],
        payload: Optional[Dict[str, Any]],
        token: str,
        address: str,
    ) -> Any:
        if method == "POST" and path == "/api/v1/login":
            return self.login(payload or {}, address)
        if not self.sessions.valid(token):
            raise PermissionError("authentication required")

        if method == "POST" and path == "/api/v1/logout":
            self.sessions.revoke(token)
            return {"ok": True}
        if method == "GET" and path == "/api/v1/status":
            return self.status()
        if method == "GET" and path == "/api/v1/system/info":
            return self._system_info(self.config.snapshot(public=True))
        if method == "GET" and path == "/api/v1/ntp":
            return {
                "config": self.config.snapshot(public=True)["ntp"],
                "status": self.ntp.status(),
            }
        if method == "POST" and path == "/api/v1/ntp":
            self.config.update_sections({"ntp": payload or {}}, ("ntp",))
            return {"ok": True, "config": self.config.snapshot(public=True)["ntp"]}
        if method == "POST" and path == "/api/v1/ntp/sync":
            return self.ntp.sync_once()
        if method == "GET" and path == "/api/v1/serial":
            return [self.workers[port_id].status() for port_id in sorted(self.workers)]
        if method == "POST" and path == "/api/v1/serial":
            ports = (payload or {}).get("ports")
            if not isinstance(ports, list):
                raise ConfigError("ports list is required")
            self.config.update_sections({"ports": ports}, ("ports",))
            return {"ok": True}
        match = re.fullmatch(r"/api/v1/serial/(\d+)/(sync|test)", path)
        if method == "POST" and match:
            port_id = int(match.group(1))
            if port_id not in self.workers:
                raise ConfigError("unknown serial port")
            kind = match.group(2)
            return self.workers[port_id].manual(kind)
        if method == "GET" and path == "/api/v1/schedule":
            return self.config.snapshot(public=True)["schedule"]
        if method == "POST" and path == "/api/v1/schedule":
            self.config.update_sections({"schedule": payload or {}}, ("schedule",))
            return {"ok": True, "config": self.config.snapshot(public=True)["schedule"]}
        if method == "GET" and path == "/api/v1/logs":
            raw = (query.get("lines") or ["150"])[0]
            lines = max(10, min(500, int(raw)))
            return {"lines": self._tail_log(lines)}
        if method == "GET" and path == "/api/v1/network":
            return self.network.status()
        if method == "POST" and path == "/api/v1/network/stage":
            body = payload or {}
            interface = str(body.get("interface", ""))
            return self.network.stage(interface, body)
        if method == "POST" and path == "/api/v1/network/confirm":
            return self.network.confirm(str((payload or {}).get("change_id", "")))
        if method == "POST" and path == "/api/v1/network/rollback":
            return self.network.rollback(str((payload or {}).get("change_id", "")))
        if method == "POST" and path == "/api/v1/account/password":
            body = payload or {}
            self.config.set_password(
                str(body.get("old_password", "")), str(body.get("new_password", ""))
            )
            self.sessions.revoke_all()
            return {"ok": True, "message": "password changed; log in again"}
        if method == "GET" and path == "/api/v1/config/export":
            return self.config.export()
        if method == "POST" and path == "/api/v1/config/import":
            incoming = (payload or {}).get("config", payload)
            if not isinstance(incoming, dict):
                raise ConfigError("config object is required")
            self.config.import_config(incoming)
            return {"ok": True}
        if method == "POST" and path == "/api/v1/system/reboot":
            if not (payload or {}).get("confirm"):
                raise ConfigError("reboot confirmation is required")
            threading.Timer(1.0, lambda: os.system("sync; reboot")).start()
            return {"ok": True, "message": "rebooting"}
        raise FileNotFoundError(path)

    def _tail_log(self, line_count: int) -> str:
        try:
            with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
                return "".join(deque(handle, maxlen=line_count))
        except OSError as exc:
            return f"log unavailable: {exc}"


def make_handler(application: GatewayApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "NtpGateway/" + APP_VERSION

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)
            if path.startswith("/api/"):
                self._handle_api(method, path, query)
            elif method == "GET":
                self._serve_static(path)
            else:
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, 1001, "method not allowed")

        def _handle_api(self, method: str, path: str, query: Dict[str, list]) -> None:
            payload = None
            if method in ("POST", "PUT"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if length > 1024 * 1024:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 1001, "request too large")
                    return
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, 1001, f"invalid JSON: {exc}")
                    return
            token = application.tokens_from_headers(self.headers)
            try:
                result = application.api(
                    method, path, query, payload, token, self.client_address[0]
                )
                self._json(HTTPStatus.OK, 0, "success", result)
            except PermissionError as exc:
                self._json(HTTPStatus.UNAUTHORIZED, 5001, str(exc))
            except ConfigError as exc:
                self._json(HTTPStatus.BAD_REQUEST, 1001, str(exc))
            except FileNotFoundError:
                self._json(HTTPStatus.NOT_FOUND, 1004, "endpoint not found")
            except Exception as exc:
                application.logger.exception("API request failed path=%s", path)
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, 1000, str(exc))

        def _serve_static(self, path: str) -> None:
            if path in ("", "/"):
                relative = "index.html"
            else:
                relative = path.lstrip("/")
            candidate = (application.web_dir / relative).resolve()
            try:
                candidate.relative_to(application.web_dir.resolve())
            except ValueError:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not candidate.is_file():
                candidate = application.web_dir / "index.html"
            try:
                data = candidate.read_bytes()
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _json(
            self,
            status: HTTPStatus,
            code: int,
            message: str,
            data: Any = None,
        ) -> None:
            payload = json.dumps(
                {"code": code, "message": message, "data": data},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args) -> None:
            application.logger.debug("HTTP %s - %s", self.client_address[0], fmt % args)

    return Handler


class GatewayHTTPServer:
    def __init__(self, application: GatewayApplication, host: str, port: int):
        self.application = application
        self.httpd = ThreadingHTTPServer((host, int(port)), make_handler(application))
        self.httpd.daemon_threads = True
        self.httpd.allow_reuse_address = True
        self.thread = threading.Thread(
            target=self.httpd.serve_forever,
            name="http-server",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()
        self.application.logger.info(
            "Web management listening on %s:%s",
            self.httpd.server_address[0],
            self.httpd.server_address[1],
        )

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
