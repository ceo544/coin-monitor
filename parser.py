import re
from bs4 import BeautifulSoup

LONG_COLOR_HINTS = ('green','blue','#16a34a','#22c55e','#10b981','#008000','#0000ff','#2563eb','#3b82f6','rgb(0, 128, 0)','rgb(0, 0, 255)')
SHORT_COLOR_HINTS = ('red','#dc2626','#ef4444','#f00','#ff0000','rgb(255, 0, 0)')
ACTIVE_HINTS = ('active','signal','entry','enter','진입','매수','매도')

def _visual_context(node):
    if node is None: return ''
    parts=[]
    for el in [node, node.parent, node.find_parent('tr')]:
        if el is None: continue
        parts += [str(el.get('style','')), ' '.join(el.get('class',[]))]
    return ' | '.join(dict.fromkeys(x for x in parts if x)).strip()

def _signal_for(soup, side):
    label=soup.find(string=re.compile(rf'^\s*{side}\s*$', re.I))
    node=label.parent if label else None
    visual=_visual_context(node)
    low=visual.lower()
    color_hints=LONG_COLOR_HINTS if side.lower()=='long' else SHORT_COLOR_HINTS
    color_match=next((x for x in color_hints if x in low), None)
    # Colored Long/Short is the primary signal. Classes such as active/signal are preserved as evidence.
    active=bool(color_match)
    return {
        'active': active,
        'detected_color': color_match,
        'visual_evidence': visual,
        'label_html': str(node) if node else None,
    }

def parse_page(html):
    soup=BeautifulSoup(html,'html.parser')
    text='\n'.join(s.strip() for s in soup.stripped_strings)
    result={'page_text': text}
    price=re.search(r'(?:현재가|Current\s*Price|BTCUSDT)[^0-9]{0,40}([0-9][0-9,]*(?:\.\d+)?)',text,re.I)
    if price: result['current_price_raw']=price.group(1)
    sides={}
    for side in ('Long','Short'):
        block=re.search(side+r'(.*?)(?=Long|Short|$)',text,re.I|re.S)
        if block:
            sides[side.lower()]={'numbers_raw':re.findall(r'(?<!\w)-?\d[\d,]*(?:\.\d+)?',block.group(1))}
    result['sides']=sides
    result['signals']={'long':_signal_for(soup,'Long'),'short':_signal_for(soup,'Short')}
    result['entry_message']=bool(re.search(r'진입\s*(?:해도\s*)?(?:좋|가능)|진입\s*추천', text))
    return result
