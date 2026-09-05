import unittest
from parser import parse_page

class SignalTests(unittest.TestCase):
    def test_long_green_signal(self):
        html='''<table><tr style="background-color:#16a34a;color:white"><td>BTCUSDT</td><td>Long</td><td>100</td></tr><tr><td>TP</td><td>101</td></tr></table><div>진입해도 좋다</div>'''
        p=parse_page(html)
        self.assertTrue(p['signals']['long']['active'])
        self.assertFalse(p['signals']['short']['active'])
        self.assertIn('#16a34a', p['signals']['long']['visual_evidence'].lower())

    def test_short_red_signal(self):
        html='''<table><tr class="short active red" style="background:#dc2626"><td>Short</td><td>110</td></tr></table>'''
        p=parse_page(html)
        self.assertTrue(p['signals']['short']['active'])
        self.assertFalse(p['signals']['long']['active'])

    def test_inactive_plain_rows(self):
        html='''<table><tr><td>Long</td><td>100</td></tr><tr><td>Short</td><td>110</td></tr></table>'''
        p=parse_page(html)
        self.assertFalse(p['signals']['long']['active'])
        self.assertFalse(p['signals']['short']['active'])

if __name__=='__main__': unittest.main()
