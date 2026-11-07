import unittest

from ntp_client import NtpClient


class CompensationModeTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "auto_compensate": True,
            "slew_threshold_sec": 0.001,
            "step_threshold_sec": 0.05,
        }

    def test_disabled_auto_compensation(self):
        config = dict(self.config, auto_compensate=False)
        self.assertEqual(NtpClient.compensation_mode(0.01, config), "disabled")

    def test_offset_below_threshold_is_ignored(self):
        self.assertEqual(
            NtpClient.compensation_mode(0.0005, self.config), "none"
        )

    def test_millisecond_offset_is_slewed(self):
        self.assertEqual(NtpClient.compensation_mode(0.001, self.config), "slew")
        self.assertEqual(NtpClient.compensation_mode(-0.02, self.config), "slew")

    def test_large_offset_is_stepped(self):
        self.assertEqual(NtpClient.compensation_mode(0.05, self.config), "step")
        self.assertEqual(NtpClient.compensation_mode(-1.0, self.config), "step")


if __name__ == "__main__":
    unittest.main()
