"""Modbus RTU CRC16 helpers."""


def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def append_crc(frame: bytes) -> bytes:
    crc = modbus_crc16(frame)
    return frame + bytes((crc & 0xFF, (crc >> 8) & 0xFF))


def crc_is_valid(frame: bytes) -> bool:
    if len(frame) < 3:
        return False
    expected = modbus_crc16(frame[:-2])
    actual = frame[-2] | (frame[-1] << 8)
    return expected == actual
