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

    def test_short_label_cell_active_via_css_class(self):
        html = """
        <style>.sigA9 { background-color: #ff3b30; color: white; }</style>
        <table>
          <tr><td>Long</td><td>79481.4</td></tr>
          <tr><td class="sigA9">Short</td><td>80173.7</td></tr>
        </table>
        """
        parsed = parse_page(html)
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertTrue(parsed["signals"]["short"]["active"])


    def test_inactive_cells_do_not_match_active_css_rules(self):
        html = """
        <style>
          .long-label { background:#e8e3dc; }
          .long-label.active { background:#2563eb; color:white; }
          .short-label { background:#e8e3dc; }
          .short-label.active { background:#ff3b30; color:white; }
        </style>
        <table>
          <tr><td class="long-label">Long</td><td>79481.4</td></tr>
          <tr><td class="short-label">Short</td><td>80173.7</td></tr>
        </table>
        """
        parsed = parse_page(html)
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertFalse(parsed["signals"]["short"]["active"])


    def test_generic_blue_border_and_red_css_do_not_activate_inactive_labels(self):
        html = """
        <style>
          td { border:1px solid #2563eb; color:#111; background:#e8e3dc; }
          .short-label.active { background:#ff3b30; color:white; }
        </style>
        <table>
          <tr><td class="long-label">Long</td><td>79481.4</td></tr>
          <tr><td class="short-label">Short</td><td>80173.7</td></tr>
        </table>
        """
        parsed = parse_page(html)
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertFalse(parsed["signals"]["short"]["active"])

    def test_only_short_active_when_short_label_background_is_red(self):
        html = """
        <style>
          td { border:1px solid #2563eb; background:#e8e3dc; }
          .short-label.active { background:#ff3b30; color:white; }
        </style>
        <table>
          <tr><td class="long-label">Long</td><td>79481.4</td></tr>
          <tr><td class="short-label active">Short</td><td>80173.7</td></tr>
        </table>
        """
        parsed = parse_page(html)
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertTrue(parsed["signals"]["short"]["active"])

    def test_utility_class_color_word_does_not_activate_signal(self):
        # Regression: Tailwind-style utility classes like "text-blue-600" or
        # "border-green-500" contain color words but describe text/border
        # color, not the label cell's background. They must never flip a
        # signal ON when the actual background is a neutral default color.
        html = """
        <style>.row-default { background:#e8e3dc; }</style>
        <table>
          <tr><td class="row-default text-blue-600 font-bold">Long</td><td>79481.4</td></tr>
          <tr><td class="row-default" style="background:#dc2626;color:#fff">Short</td><td>80173.7</td></tr>
        </table>
        """
        parsed = parse_page(html)
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertTrue(parsed["signals"]["short"]["active"])

    def test_long_short_both_default_are_off(self):
        # Matches the real E-RANG page case: Long label default beige,
        # Short label default beige => both OFF (WAIT), not BOTH ON.
        html = """
        <style>.default-cell { background:#f1ece3; color:#111; }</style>
        <table>
          <tr><td class="default-cell">Long</td><td>79481.4</td></tr>
          <tr><td class="default-cell">Short</td><td>80173.7</td></tr>
        </table>
        """
        parsed = parse_page(html)
        self.assertFalse(parsed["signals"]["long"]["active"])
        self.assertFalse(parsed["signals"]["short"]["active"])


if __name__ == "__main__":
    unittest.main()
