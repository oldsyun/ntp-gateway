"""P3 Specific full-time frame construction.

Only the documented Modbus RTU 0x10 full-time request is generated.
The single-byte millisecond field is always zero, so each frame represents
an exact whole second.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from crc16 import append_crc, crc_is_valid


FULL_FUNCTION = 0x10
FULL_REGISTER = 402504
FULL_REGISTER_COUNT = 4


class ProtocolError(ValueError):
    pass


def register_to_pdu_address(register: int) -> int:
    address = register - 400001
    if not 0 <= address <= 0xFFFF:
        raise ProtocolError("register address is outside the Modbus data area")
    return address


def _u16(value: int, name: str) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ProtocolError(f"{name} is outside 0..65535")
    return bytes(((value >> 8) & 0xFF, value & 0xFF))


def build_full_frame(
    slave_addr: int,
    time_value: datetime,
    *,
    register: int = FULL_REGISTER,
) -> bytes:
    """Build the documented 8-byte full-time P3 request with ms=0."""
    if not 0 <= slave_addr <= 247:
        raise ProtocolError("slave address must be 0..247")

    year = time_value.year
    payload = bytes(
        (
            0,
            time_value.second,
            time_value.minute,
            time_value.hour,
            time_value.day,
            time_value.month,
            (year >> 8) & 0xFF,
            year & 0xFF,
        )
    )
    pdu_address = register_to_pdu_address(register)
    body = bytes((slave_addr, FULL_FUNCTION)) + _u16(pdu_address, "address")
    body += _u16(FULL_REGISTER_COUNT, "register count")
    body += bytes((len(payload),)) + payload
    return append_crc(body)


@dataclass(frozen=True)
class ResponseResult:
    valid: bool
    message: str
    raw: bytes


def validate_write_response(
    frame: bytes,
    *,
    slave_addr: int,
    function: int,
    register: int,
    register_count: int,
) -> ResponseResult:
    """Validate a standard Modbus write-single/write-multiple response."""
    if len(frame) < 5:
        return ResponseResult(False, "response too short", frame)
    if not crc_is_valid(frame):
        return ResponseResult(False, "CRC mismatch", frame)
    if frame[0] != slave_addr:
        return ResponseResult(False, "slave address mismatch", frame)
    if frame[1] == (function | 0x80):
        code = frame[2] if len(frame) > 2 else 0
        return ResponseResult(False, f"Modbus exception 0x{code:02X}", frame)
    if frame[1] != function:
        return ResponseResult(False, "function code mismatch", frame)

    expected_address = register_to_pdu_address(register)
    if len(frame) < 8:
        return ResponseResult(False, "response length mismatch", frame)
    actual_address = (frame[2] << 8) | frame[3]
    actual_count = (frame[4] << 8) | frame[5]
    if actual_address != expected_address:
        return ResponseResult(False, "register address mismatch", frame)
    if actual_count != register_count:
        return ResponseResult(False, "register count mismatch", frame)
    return ResponseResult(True, "ok", frame)
