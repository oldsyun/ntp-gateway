import unittest
from datetime import datetime

from crc16 import append_crc, crc_is_valid, modbus_crc16
from p3_protocol import (
    build_full_frame,
    register_to_pdu_address,
    validate_write_response,
)


class CrcTests(unittest.TestCase):
    def test_documented_crc_vector(self):
        body = bytes.fromhex("01 10 09 C7 00 04 08 00 10 20 0A 1F 05 07 E0")
        self.assertEqual(modbus_crc16(body), 0xF4C7)
        self.assertEqual(append_crc(body).hex(" ").upper(), "01 10 09 C7 00 04 08 00 10 20 0A 1F 05 07 E0 C7 F4")

    def test_crc_roundtrip(self):
        frame = append_crc(b"\x01\x03\x00\x00\x00\x01")
        self.assertTrue(crc_is_valid(frame))


class ProtocolTests(unittest.TestCase):
    def test_register_mapping(self):
        self.assertEqual(register_to_pdu_address(402504), 0x09C7)
        self.assertEqual(register_to_pdu_address(402502), 0x09C5)

    def test_full_frame_fields(self):
        when = datetime(2016, 5, 31, 10, 32, 16, 0)
        frame = build_full_frame(1, when)
        self.assertEqual(
            frame.hex(" ").upper(),
            "01 10 09 C7 00 04 08 00 10 20 0A 1F 05 07 E0 C7 F4",
        )

    def test_full_frame_year_is_big_endian(self):
        frame = build_full_frame(
            0,
            datetime(2026, 11, 7, 19, 30, 45, 123000),
        )
        self.assertEqual(frame[7:15], bytes.fromhex("00 2D 1E 13 07 0B 07 EA"))

    def test_full_frame_does_not_overflow(self):
        frame = build_full_frame(
            0,
            datetime(2026, 11, 7, 19, 30, 45, 849000),
        )
        self.assertEqual(frame[7], 0)
        self.assertEqual(len(frame), 17)

    def test_response_validation(self):
        response = append_crc(bytes.fromhex("01 10 09 C7 00 04"))
        result = validate_write_response(
            response,
            slave_addr=1,
            function=0x10,
            register=402504,
            register_count=4,
        )
        self.assertTrue(result.valid, result.message)


if __name__ == "__main__":
    unittest.main()
