"""Independent scheduler and sender for one RS485 port."""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, Optional

from config_manager import ConfigManager
from ntp_client import NtpClient
from p3_protocol import (
    FULL_FUNCTION,
    FULL_REGISTER_COUNT,
    build_full_frame,
    validate_write_response,
)
from schedule_utils import compute_next_due_epoch
from serial_port import SerialError, SerialPort


class TimeInvalidError(RuntimeError):
    pass


class PortWorker(threading.Thread):
    def __init__(
        self,
        port_id: int,
        config_manager: ConfigManager,
        ntp_client: NtpClient,
        logger: logging.Logger,
    ):
        super().__init__(name=f"rs485-{port_id}", daemon=True)
        self.port_id = int(port_id)
        self.config_manager = config_manager
        self.ntp_client = ntp_client
        self.logger = logger
        self._stop_event = threading.Event()
        self._manual: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._lock = threading.RLock()
        self._serial: Optional[SerialPort] = None
        self._config_revision = -1
        self._next_due_epoch = 0.0
        self._status: Dict[str, Any] = {
            "id": self.port_id,
            "name": f"COM{self.port_id}",
            "enabled": False,
            "state": "starting",
            "last_result": "not_run",
            "last_error": "",
            "last_sync": None,
            "last_attempt": None,
            "next_due": None,
            "tx_count": 0,
            "success_count": 0,
            "error_count": 0,
            "last_frame": "",
            "last_response": "",
            "direction_mode": "closed",
            "serial_open": False,
        }

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=5)
        with self._lock:
            if self._serial:
                self._serial.close()
        self._drain_manual("service stopping")

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                config = self.config_manager.port(self.port_id)
                revision = self.config_manager.revision
                if revision != self._config_revision:
                    self._config_revision = revision
                    if self._serial:
                        self._serial.close()
                    self._next_due_epoch = self._compute_next_due_epoch(config)
                    self._set_status(
                        name=str(config.get("name", f"COM{self.port_id}")),
                        enabled=bool(config.get("enabled")),
                        direction_mode="closed",
                        serial_open=False,
                    )

                self._drain_manual_queue(config)
                if not config.get("enabled"):
                    self._close_serial()
                    self._set_status(
                        enabled=False,
                        state="disabled",
                        next_due=None,
                        last_result="disabled",
                    )
                    self._stop_event.wait(1.0)
                    continue

                now = time.time()
                if self._next_due_epoch <= 0:
                    self._next_due_epoch = self._compute_next_due_epoch(config)
                if now >= self._next_due_epoch:
                    scheduled_epoch = self._next_due_epoch
                    self._perform_sync(
                        config,
                        trigger="schedule",
                        force=False,
                        scheduled_epoch=scheduled_epoch,
                    )
                    self._next_due_epoch = self._compute_next_due_epoch(
                        config, after_epoch=scheduled_epoch
                    )
                self._set_status(
                    enabled=True,
                    next_due=self._format_due_epoch(self._next_due_epoch),
                )
                wait_for = self._next_due_epoch - time.time()
                if wait_for > 0.05:
                    self._stop_event.wait(min(0.2, wait_for - 0.02))
                elif wait_for > 0:
                    time.sleep(wait_for)
            except Exception as exc:
                self.logger.exception("Port worker %s failed: %s", self.port_id, exc)
                self._set_status(state="error", last_result="worker_error", last_error=str(exc))
                self._stop_event.wait(1.0)

    def _compute_next_due_epoch(
        self,
        config: Dict[str, Any],
        *,
        after_epoch: Optional[float] = None,
    ) -> float:
        base = time.time() if after_epoch is None else float(after_epoch)
        return compute_next_due_epoch(
            schedule_mode=str(config.get("schedule_mode", "interval")),
            interval_sec=int(config.get("interval_sec", 60)),
            daily_time=str(config.get("daily_time", "00:00:00")),
            after_epoch=base,
        )

    @staticmethod
    def _format_due_epoch(epoch: float) -> Optional[str]:
        if epoch <= 0:
            return None
        return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")

    def _close_serial(self) -> None:
        with self._lock:
            if self._serial:
                self._serial.close()
        self._set_status(serial_open=False, direction_mode="closed")

    def _ensure_serial(self, config: Dict[str, Any]) -> SerialPort:
        with self._lock:
            if self._serial is None:
                self._serial = SerialPort(config, self.logger)
            self._serial.ensure_open(config)
            self._set_status(
                serial_open=True,
                direction_mode=self._serial.direction_mode,
            )
            return self._serial

    def _perform_sync(
        self,
        config: Dict[str, Any],
        *,
        trigger: str,
        force: bool,
        scheduled_epoch: Optional[float] = None,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        self._set_status(
            state="sending",
            last_attempt=datetime.now().isoformat(timespec="seconds"),
            last_error="",
        )
        schedule = self.config_manager.snapshot()["schedule"]
        if (
            trigger == "schedule"
            and bool(schedule.get("ntp_required", True))
            and not self.ntp_client.is_usable()
        ):
            message = "system time is not valid for automatic synchronisation"
            self._set_status(
                state="waiting_ntp",
                last_result="blocked",
                last_error=message,
            )
            return {"ok": False, "error": message, "blocked": True}

        broadcast = bool(config.get("broadcast", False))
        wait_response = bool(config.get("wait_response", False)) and not broadcast
        retry_count = int(schedule.get("retry_count", 2))
        retry_interval = int(schedule.get("retry_interval_sec", 2))
        attempts = 1 if broadcast or force else 1 + retry_count
        last_error = ""
        response = b""
        frame = b""
        for attempt in range(1, attempts + 1):
            try:
                if scheduled_epoch is not None and attempt == 1:
                    send_second = int(scheduled_epoch)
                else:
                    send_second = int(time.time())
                send_time = datetime.fromtimestamp(send_second)
                frame = build_full_frame(
                    int(config["slave_addr"]),
                    send_time,
                    register=int(config.get("full_register", 402504)),
                )
                serial = self._ensure_serial(config)
                response = serial.send(
                    frame,
                    wait_response=wait_response,
                    response_timeout_ms=int(config.get("response_timeout_ms", 500)),
                    expected_length=8,
                )
                if wait_response:
                    result = validate_write_response(
                        response,
                        slave_addr=int(config["slave_addr"]),
                        function=FULL_FUNCTION,
                        register=int(config.get("full_register", 402504)),
                        register_count=FULL_REGISTER_COUNT,
                    )
                    if not result.valid:
                        raise SerialError(result.message)
                duration_ms = int((time.monotonic() - started) * 1000)
                self._set_status(
                    state="ok",
                    last_result="success",
                    last_error="",
                    last_sync=datetime.now().isoformat(timespec="seconds"),
                    last_frame=frame.hex(" ").upper(),
                    last_response=response.hex(" ").upper(),
                    tx_count=self._status["tx_count"] + 1,
                    success_count=self._status["success_count"] + 1,
                )
                self.logger.info(
                    "Sync success port=%s trigger=%s attempt=%s frame=%s response=%s duration_ms=%s",
                    self.port_id,
                    trigger,
                    attempt,
                    frame.hex(" "),
                    response.hex(" ") if response else "-",
                    duration_ms,
                )
                return {
                    "ok": True,
                    "port": self.port_id,
                    "trigger": trigger,
                    "mode": "rtc",
                    "frame": frame.hex(" ").upper(),
                    "response": response.hex(" ").upper(),
                    "duration_ms": duration_ms,
                }
            except Exception as exc:
                last_error = str(exc)
                self.logger.warning(
                    "Sync failed port=%s trigger=%s attempt=%s/%s error=%s",
                    self.port_id,
                    trigger,
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    self._stop_event.wait(max(0, retry_interval))

        duration_ms = int((time.monotonic() - started) * 1000)
        state = "timeout" if "timeout" in last_error.lower() else "error"
        self._set_status(
            state=state,
            last_result="failed",
            last_error=last_error,
            last_frame=frame.hex(" ").upper(),
            last_response=response.hex(" ").upper() if response else "",
            tx_count=self._status["tx_count"] + 1,
            error_count=self._status["error_count"] + 1,
        )
        return {
            "ok": False,
            "port": self.port_id,
            "trigger": trigger,
            "error": last_error,
            "frame": frame.hex(" ").upper(),
            "response": response.hex(" ").upper() if response else "",
            "duration_ms": duration_ms,
        }

    def manual(self, kind: str = "sync", timeout: float = 35.0) -> Dict[str, Any]:
        event = threading.Event()
        item = {
            "kind": kind,
            "force": kind == "test",
            "event": event,
            "result": None,
        }
        self._manual.put(item)
        if not event.wait(timeout):
            return {"ok": False, "port": self.port_id, "error": "manual operation timeout"}
        return item["result"] or {
            "ok": False,
            "port": self.port_id,
            "error": "manual operation produced no result",
        }

    def _drain_manual_queue(self, config: Dict[str, Any]) -> None:
        while True:
            try:
                item = self._manual.get_nowait()
            except queue.Empty:
                return
            try:
                result = self._perform_sync(
                    config,
                    trigger=item["kind"],
                    force=bool(item["force"]),
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "port": self.port_id,
                    "error": str(exc),
                }
            item["result"] = result
            item["event"].set()

    def _drain_manual(self, error: str) -> None:
        while True:
            try:
                item = self._manual.get_nowait()
            except queue.Empty:
                return
            item["result"] = {"ok": False, "port": self.port_id, "error": error}
            item["event"].set()

    def _set_status(self, **updates: Any) -> None:
        with self._lock:
            self._status.update(updates)

    def status(self) -> Dict[str, Any]:
        try:
            config = self.config_manager.port(self.port_id)
        except Exception:
            config = {}
        with self._lock:
            result = dict(self._status)
            result["config"] = config
            result["next_due"] = (
                self._format_due_epoch(self._next_due_epoch)
                if config.get("enabled")
                else None
            )
            return result
