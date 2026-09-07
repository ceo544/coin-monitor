import unittest
from parser import parse_page

class ParserSignalTests(unittest.TestCase):
    def test_long_green_signal(self):
        html = """
        <table><tr style="background-color:#16a34a;color:white"><td>BTCUSDT</td><td>Long</td><td>100</td></tr></table>
        <div>진입해도 좋다</div>
        """
        parsed = parse_page(html)
        self.assertTrue(parsed["signals"]["long"]["active"])
        self.assertFalse(parsed["signals"]["short"]["active"])
        self.assertTrue(parsed["entry_message"])

    def test_short_red_signal(self):
        html = '<table><tr class="short active red" style="background:#dc2626"><td>Short</td><td>110</td></tr></table>'
        parsed = parse_page(html)
        self.assertTrue(parsed["signals"]["short"]["active"])
        self.assertFalse(parsed["signals"]["long"]["active"])

    def test_plain_labels_not_active(self):
        html = '<div>BTCUSDT 현재가 79546.70</div><div>Long 79168.4 78523.8</div><div>Short 80808.1</div>'
        parsed = parse_page(html)
        self.assertEqual(parsed["current_price_raw"], "79546.70")
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertFalse(parsed["signals"]["short"]["active"])
        self.assertIn("long", parsed["sides"])
        self.assertIn("short", parsed["sides"])

if __name__ == "__main__":
    unittest.main()
