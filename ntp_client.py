"""Small SNTP client and time-validity state machine."""

from __future__ import annotations

import ctypes
import logging
import socket
import struct
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from config_manager import ConfigManager


NTP_DELTA = 2_208_988_800
NTP_PORT = 123
NTP_PACKET_SIZE = 48


class Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


def _ntp_timestamp_to_unix(seconds: int, fraction: int) -> float:
    return (seconds - NTP_DELTA) + (fraction / 4294967296.0)


def _unix_to_ntp_timestamp(value: float) -> bytes:
    seconds = int(value) + NTP_DELTA
    fraction = int((value - int(value)) * 4294967296.0) & 0xFFFFFFFF
    return struct.pack("!II", seconds & 0xFFFFFFFF, fraction)


def _set_system_time(value: float) -> None:
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    ts = Timespec()
    ts.tv_sec = int(value)
    ts.tv_nsec = int((value - int(value)) * 1_000_000_000)
    clock_realtime = 0
    if libc.clock_settime(clock_realtime, ctypes.byref(ts)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, "clock_settime failed")


def _slew_system_time(offset: float) -> None:
    if not sys.platform.startswith("linux"):
        return
    microseconds = int(round(float(offset) * 1_000_000.0))
    if microseconds == 0:
        return
    libc = ctypes.CDLL(None, use_errno=True)
    delta = Timeval(0, microseconds)
    if libc.adjtime(ctypes.byref(delta), None) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, "adjtime failed")


class NtpClient:
    def __init__(self, config_manager: ConfigManager, logger: logging.Logger):
        self.config_manager = config_manager
        self.logger = logger
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state = "TIME_INVALID"
        self._server = ""
        self._offset = 0.0
        self._delay = 0.0
        self._stratum: Optional[int] = None
        self._last_sync_mono = 0.0
        self._last_sync_wall = 0.0
        self._last_error = ""
        self._last_attempt_wall = 0.0
        self._sync_count = 0
        self._error_count = 0
        self._step_count = 0
        self._slew_count = 0
        self._last_adjustment = "none"
        self._last_adjustment_sec = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, name="ntp-client", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            config = self.config_manager.snapshot()["ntp"]
            if config.get("enabled", True):
                self.sync_once()
                interval = max(5, int(config.get("sync_interval_sec", 60)))
            else:
                interval = 10
            self._stop.wait(interval)

    def sync_once(self) -> Dict[str, Any]:
        with self._sync_lock:
            config = self.config_manager.snapshot()["ntp"]
            if not config.get("enabled", True):
                self._set_state("TIME_INVALID", error="NTP client disabled")
                return self.status()

            self._last_attempt_wall = time.time()
            results = []
            errors = []
            for server in config.get("servers", []):
                try:
                    result = self._query_server(server, config)
                    results.append(result)
                    self.logger.info(
                        "NTP response server=%s stratum=%s offset=%.6f delay=%.6f",
                        server,
                        result["stratum"],
                        result["offset"],
                        result["delay"],
                    )
                except Exception as exc:
                    errors.append(f"{server}: {exc}")
                    self.logger.warning("NTP query failed server=%s error=%s", server, exc)

            if not results:
                self._error_count += 1
                self._last_error = "; ".join(errors)[-400:]
                self._refresh_state(config)
                return self.status()

            best = min(results, key=lambda item: item["delay"])
            offset = float(best["offset"])
            if abs(offset) > float(config.get("max_step_sec", 86400)):
                self._error_count += 1
                self._last_error = (
                    f"offset {offset:.3f}s exceeds max_step_sec "
                    f"{config.get('max_step_sec')}"
                )
                self._refresh_state(config)
                return self.status()

            adjustment = self.compensation_mode(offset, config)
            if adjustment == "slew":
                try:
                    _slew_system_time(offset)
                    self._slew_count += 1
                except OSError as exc:
                    self.logger.warning(
                        "NTP slew compensation failed, falling back to step: %s", exc
                    )
                    _set_system_time(time.time() + offset)
                    self._step_count += 1
                    adjustment = "step_fallback"
            elif adjustment == "step":
                _set_system_time(time.time() + offset)
                self._step_count += 1

            with self._lock:
                self._server = str(best["server"])
                self._offset = offset
                self._delay = float(best["delay"])
                self._stratum = int(best["stratum"])
                self._last_sync_mono = time.monotonic()
                self._last_sync_wall = time.time()
                self._last_error = ""
                self._sync_count += 1
                self._last_adjustment = adjustment
                self._last_adjustment_sec = (
                    offset if adjustment in ("slew", "step", "step_fallback") else 0.0
                )
                self._state = "TIME_SYNCED"
            self.logger.info(
                "NTP sync complete server=%s adjustment=%s offset=%.6f delay=%.6f",
                best["server"],
                adjustment,
                offset,
                best["delay"],
            )
            return self.status()

    @staticmethod
    def compensation_mode(offset: float, config: Dict[str, Any]) -> str:
        if not bool(config.get("auto_compensate", True)):
            return "disabled"
        absolute = abs(float(offset))
        slew_threshold = float(config.get("slew_threshold_sec", 0.001))
        step_threshold = float(config.get("step_threshold_sec", 0.05))
        if absolute < slew_threshold:
            return "none"
        if absolute >= step_threshold:
            return "step"
        return "slew"

    def _query_server(self, server: str, config: Dict[str, Any]) -> Dict[str, Any]:
        timeout = max(0.1, int(config.get("timeout_ms", 1500)) / 1000.0)
        family = socket.AF_UNSPEC
        last_error: Optional[Exception] = None
        for af, socktype, proto, _, sockaddr in socket.getaddrinfo(
            server, NTP_PORT, family, socket.SOCK_DGRAM
        ):
            sock = socket.socket(af, socktype, proto)
            try:
                sock.settimeout(timeout)
                request = bytearray(NTP_PACKET_SIZE)
                request[0] = 0x23  # LI=0, VN=4, mode=3 (client)
                t1 = time.time()
                request[40:48] = _unix_to_ntp_timestamp(t1)
                sock.sendto(bytes(request), sockaddr)
                response, _ = sock.recvfrom(512)
                t4 = time.time()
                if len(response) < NTP_PACKET_SIZE:
                    raise ValueError("short NTP response")
                mode = response[0] & 0x07
                leap = (response[0] >> 6) & 0x03
                stratum = response[1]
                if mode != 4:
                    raise ValueError(f"unexpected NTP mode {mode}")
                if leap == 3:
                    raise ValueError("server clock is unsynchronised")
                if not 1 <= stratum <= 15:
                    raise ValueError(f"invalid NTP stratum {stratum}")
                receive_sec, receive_frac = struct.unpack("!II", response[32:40])
                transmit_sec, transmit_frac = struct.unpack("!II", response[40:48])
                if transmit_sec == 0:
                    raise ValueError("zero NTP transmit timestamp")
                t2 = _ntp_timestamp_to_unix(receive_sec, receive_frac)
                t3 = _ntp_timestamp_to_unix(transmit_sec, transmit_frac)
                offset = ((t2 - t1) + (t3 - t4)) / 2.0
                delay = (t4 - t1) - (t3 - t2)
                if delay < -0.01 or delay > 10:
                    raise ValueError(f"invalid NTP delay {delay:.3f}s")
                return {
                    "server": server,
                    "stratum": stratum,
                    "offset": offset,
                    "delay": max(0.0, delay),
                }
            except Exception as exc:
                last_error = exc
            finally:
                sock.close()
        raise last_error or OSError("no usable address")

    def _set_state(self, state: str, *, error: str = "") -> None:
        with self._lock:
            self._state = state
            if error:
                self._last_error = error

    def _refresh_state(self, config: Dict[str, Any]) -> None:
        with self._lock:
            state = self._classify_locked(config)
            self._state = state

    def _classify_locked(self, config: Dict[str, Any]) -> str:
        if self._last_sync_mono <= 0:
            return "TIME_INVALID"
        age = max(0.0, time.monotonic() - self._last_sync_mono)
        if age <= int(config.get("max_age_sec", 300)):
            return "TIME_SYNCED"
        if bool(config.get("allow_holdover", False)) and age <= int(
            config.get("holdover_max_sec", 3600)
        ):
            return "TIME_HOLDOVER"
        return "TIME_INVALID"

    def is_usable(self) -> bool:
        status = self.status()
        return status["state"] in ("TIME_SYNCED", "TIME_HOLDOVER")

    def status(self) -> Dict[str, Any]:
        config = self.config_manager.snapshot()["ntp"]
        with self._lock:
            if not config.get("enabled", True):
                state = "TIME_INVALID"
            else:
                state = self._classify_locked(config)
            age = (
                max(0.0, time.monotonic() - self._last_sync_mono)
                if self._last_sync_mono
                else None
            )
            return {
                "state": state,
                "usable": state in ("TIME_SYNCED", "TIME_HOLDOVER"),
                "server": self._server,
                "offset_sec": round(self._offset, 6),
                "delay_sec": round(self._delay, 6),
                "stratum": self._stratum,
                "last_sync": (
                    datetime.fromtimestamp(self._last_sync_wall).isoformat(
                        timespec="seconds"
                    )
                    if self._last_sync_wall
                    else None
                ),
                "age_sec": round(age, 1) if age is not None else None,
                "last_attempt": (
                    datetime.fromtimestamp(self._last_attempt_wall).isoformat(
                        timespec="seconds"
                    )
                    if self._last_attempt_wall
                    else None
                ),
                "last_error": self._last_error,
                "sync_count": self._sync_count,
                "step_count": self._step_count,
                "slew_count": self._slew_count,
                "last_adjustment": self._last_adjustment,
                "last_adjustment_sec": round(self._last_adjustment_sec, 6),
                "auto_compensate": bool(config.get("auto_compensate", True)),
                "slew_threshold_sec": float(
                    config.get("slew_threshold_sec", 0.001)
                ),
                "step_threshold_sec": float(
                    config.get("step_threshold_sec", 0.05)
                ),
                "error_count": self._error_count,
            }
