"""Linux termios serial port with optional RS485 driver direction control."""

from __future__ import annotations

import errno
import logging
import os
import select
import struct
import time
from typing import Any, Dict, Optional

try:
    import fcntl
    import termios
except ImportError:  # pragma: no cover - only used for local non-Linux checks
    fcntl = None
    termios = None


TIOCSRS485 = 0x542F
SER_RS485_ENABLED = 1 << 0
SER_RS485_RTS_ON_SEND = 1 << 1
SER_RS485_RX_DURING_TX = 1 << 4

if termios is not None:
    BAUD_MAP = {
        1200: termios.B1200,
        2400: termios.B2400,
        4800: termios.B4800,
        9600: termios.B9600,
        19200: termios.B19200,
        38400: termios.B38400,
        57600: termios.B57600,
        115200: termios.B115200,
    }
else:
    BAUD_MAP = {}


class SerialError(RuntimeError):
    pass


class SerialPort:
    def __init__(self, config: Dict[str, Any], logger: logging.Logger):
        self.config = dict(config)
        self.logger = logger
        self.fd: Optional[int] = None
        self.signature = self._signature(config)
        self.direction_mode = "not_opened"

    @staticmethod
    def _signature(config: Dict[str, Any]) -> tuple:
        return (
            config.get("device"),
            int(config.get("baudrate", 9600)),
            int(config.get("databits", 8)),
            int(config.get("stopbits", 1)),
            str(config.get("parity", "N")).upper(),
            bool(config.get("rs485_auto_direction", True)),
        )

    def ensure_open(self, config: Dict[str, Any]) -> None:
        signature = self._signature(config)
        if self.fd is not None and signature == self.signature:
            return
        self.close()
        self.config = dict(config)
        self.signature = signature
        self._open()

    def _open(self) -> None:
        if termios is None or fcntl is None:
            raise SerialError("serial ports require Linux termios")
        device = str(self.config["device"])
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attrs = termios.tcgetattr(fd)
            iflag, oflag, cflag, lflag = attrs[0], attrs[1], attrs[2], attrs[3]
            iflag = 0
            oflag = 0
            lflag = 0
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.PARODD | termios.CSTOPB)
            cflag |= termios.CREAD | termios.CLOCAL
            databits = int(self.config.get("databits", 8))
            cflag |= termios.CS8 if databits == 8 else termios.CS7
            parity = str(self.config.get("parity", "N")).upper()
            if parity in ("E", "O"):
                cflag |= termios.PARENB
                if parity == "O":
                    cflag |= termios.PARODD
            if int(self.config.get("stopbits", 1)) == 2:
                cflag |= termios.CSTOPB
            attrs[0], attrs[1], attrs[2], attrs[3] = iflag, oflag, cflag, lflag
            baud = BAUD_MAP[int(self.config.get("baudrate", 9600))]
            attrs[4] = baud
            attrs[5] = baud
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except Exception:
            os.close(fd)
            raise
        self.fd = fd
        self.direction_mode = "hardware"
        if bool(self.config.get("rs485_auto_direction", True)):
            self.direction_mode = self._enable_driver_rs485()
        self.logger.info(
            "Serial opened device=%s baud=%s databits=%s stopbits=%s parity=%s direction=%s",
            device,
            self.config.get("baudrate"),
            self.config.get("databits"),
            self.config.get("stopbits"),
            self.config.get("parity"),
            self.direction_mode,
        )

    def _enable_driver_rs485(self) -> str:
        assert self.fd is not None
        attempts = (
            ("rts-on-send+rx", SER_RS485_ENABLED | SER_RS485_RTS_ON_SEND | SER_RS485_RX_DURING_TX),
            ("rts-on-send", SER_RS485_ENABLED | SER_RS485_RTS_ON_SEND),
            ("enabled", SER_RS485_ENABLED),
        )
        last_error = ""
        for name, flags in attempts:
            request = struct.pack("<8I", flags, 0, 0, 0, 0, 0, 0, 0)
            try:
                fcntl.ioctl(self.fd, TIOCSRS485, request)
                return f"driver:{name}"
            except OSError as exc:
                last_error = str(exc)
        self.logger.warning(
            "TIOCSRS485 unavailable on %s (%s); using board/hardware direction control",
            self.config.get("device"),
            last_error,
        )
        return "driver-unsupported"

    def send(
        self,
        frame: bytes,
        *,
        wait_response: bool,
        response_timeout_ms: int,
        expected_length: int = 8,
    ) -> bytes:
        if termios is None:
            raise SerialError("serial ports require Linux termios")
        if self.fd is None:
            raise SerialError("serial port is not open")
        termios.tcflush(self.fd, termios.TCIFLUSH)
        offset = 0
        deadline = time.monotonic() + max(0.2, len(frame) / 100.0)
        while offset < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialError("serial write timeout")
            _, writable, _ = select.select([], [self.fd], [], remaining)
            if not writable:
                raise SerialError("serial write timeout")
            try:
                written = os.write(self.fd, frame[offset:])
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise
            offset += written
        termios.tcdrain(self.fd)
        if not wait_response:
            return b""

        deadline = time.monotonic() + max(0.05, response_timeout_ms / 1000.0)
        response = bytearray()
        while len(response) < expected_length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([self.fd], [], [], remaining)
            if not readable:
                break
            try:
                chunk = os.read(self.fd, max(1, expected_length - len(response)))
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise
            if not chunk:
                break
            response.extend(chunk)
        if not response:
            raise TimeoutError("serial response timeout")
        return bytes(response)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        self.direction_mode = "closed"

    def __del__(self):
        self.close()
