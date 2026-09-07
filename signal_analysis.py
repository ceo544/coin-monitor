from __future__ import annotations
from typing import Any, Dict, Iterable

TIMEFRAMES = ('1m','5m','15m','1h')

def _f(v):
    try: return float(v)
    except (TypeError, ValueError): return None

def signal_snapshot(record: Dict[str, Any]) -> Dict[str, Any]:
    b = record.get('binance') or {}
    signal = 'BOTH' if record.get('long_signal') and record.get('short_signal') else 'LONG' if record.get('long_signal') else 'SHORT' if record.get('short_signal') else 'WAIT'
    out = {
        'id': record.get('id'), 'observed_at': record.get('observed_at'), 'signal': signal,
        'current_price': _f(record.get('current_price') or record.get('current_price_raw')),
        'funding_rate': _f((b.get('premium_index') or {}).get('lastFundingRate')),
        'open_interest': _f((b.get('open_interest') or {}).get('openInterest')),
        'timeframes': {}
    }
    for tf in TIMEFRAMES:
        ind = (b.get('indicators') or {}).get(tf) or {}; mac = ind.get('macd') or {}; bol = ind.get('bollinger20') or {}
        close, ema20 = _f(ind.get('close')), _f(ind.get('ema20'))
        out['timeframes'][tf] = {
            'close': close, 'ema20': ema20, 'ema50': _f(ind.get('ema50')), 'ema200': _f(ind.get('ema200')),
            'rsi14': _f(ind.get('rsi14')), 'atr14': _f(ind.get('atr14')),
            'macd': _f(mac.get('macd')), 'macd_signal': _f(mac.get('signal')), 'macd_histogram': _f(mac.get('histogram')),
            'bollinger_upper': _f(bol.get('upper')), 'bollinger_middle': _f(bol.get('middle')), 'bollinger_lower': _f(bol.get('lower')),
            'price_vs_ema20': ('above' if close > ema20 else 'below' if close < ema20 else 'equal') if close is not None and ema20 is not None else None,
        }
    return out

def _avg(vals):
    vals=[v for v in vals if v is not None]
    return round(sum(vals)/len(vals), 6) if vals else None

def summarize_signal_records(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    snaps=[signal_snapshot(r) for r in records]
    result={}
    for side in ('LONG','SHORT'):
        ss=[s for s in snaps if s['signal']==side]
        group={'count':len(ss),'avg_funding_rate':_avg([s['funding_rate'] for s in ss]),'avg_open_interest':_avg([s['open_interest'] for s in ss]),'timeframes':{}}
        for tf in TIMEFRAMES:
            rows=[s['timeframes'][tf] for s in ss]; h=[r['macd_histogram'] for r in rows if r['macd_histogram'] is not None]; rel=[r['price_vs_ema20'] for r in rows if r['price_vs_ema20']]
            group['timeframes'][tf]={
                'avg_rsi14':_avg([r['rsi14'] for r in rows]), 'avg_atr14':_avg([r['atr14'] for r in rows]),
                'avg_macd_histogram':_avg(h), 'macd_hist_positive_pct':round(sum(v>0 for v in h)*100/len(h),1) if h else None,
                'price_above_ema20_pct':round(sum(v=='above' for v in rel)*100/len(rel),1) if rel else None,
            }
        result[side]=group
    return result
