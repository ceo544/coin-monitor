from __future__ import annotations
from typing import List, Optional, Dict, Any


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ema(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    k = 2 / (period + 1)
    current = sum(values[:period]) / period
    for price in values[period:]:
        current = price * k + current * (1 - k)
    return current


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) <= period:
        return None
    gains: List[float] = []
    losses: List[float] = []
    for prev, cur in zip(values, values[1:period + 1]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for prev, cur in zip(values[period:], values[period + 1:]):
        diff = cur - prev
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    if len(highs) <= period or len(lows) <= period or len(closes) <= period:
        return None
    trs: List[float] = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    current = sum(trs[:period]) / period
    for tr in trs[period:]:
        current = (current * (period - 1) + tr) / period
    return current


def macd(values: List[float]) -> Dict[str, Optional[float]]:
    if len(values) < 35:
        return {"macd": None, "signal": None, "histogram": None}
    ema12_series = _ema_series(values, 12)
    ema26_series = _ema_series(values, 26)
    macd_values = []
    for a, b in zip(ema12_series[-len(ema26_series):], ema26_series):
        if a is not None and b is not None:
            macd_values.append(a - b)
    signal = ema(macd_values, 9) if len(macd_values) >= 9 else None
    line = macd_values[-1] if macd_values else None
    return {"macd": line, "signal": signal, "histogram": (line - signal) if line is not None and signal is not None else None}


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = []
    if len(values) < period:
        return [None] * len(values)
    current = sum(values[:period]) / period
    out.extend([None] * (period - 1))
    out.append(current)
    k = 2 / (period + 1)
    for price in values[period:]:
        current = price * k + current * (1 - k)
        out.append(current)
    return out


def bollinger(values: List[float], period: int = 20, deviations: float = 2.0) -> Dict[str, Optional[float]]:
    if len(values) < period:
        return {"middle": None, "upper": None, "lower": None}
    window = values[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = variance ** 0.5
    return {"middle": mean, "upper": mean + deviations * std, "lower": mean - deviations * std}


def indicators_from_klines(klines: List[List[Any]]) -> Dict[str, Any]:
    highs: List[float] = []
    lows: List[float] = []
    closes: List[float] = []
    volumes: List[float] = []
    for row in klines:
        if len(row) < 6:
            continue
        h = _safe_float(row[2]); l = _safe_float(row[3]); c = _safe_float(row[4]); v = _safe_float(row[5])
        if h is None or l is None or c is None or v is None:
            continue
        highs.append(h); lows.append(l); closes.append(c); volumes.append(v)
    latest = {
        "close": closes[-1] if closes else None,
        "high": highs[-1] if highs else None,
        "low": lows[-1] if lows else None,
        "volume": volumes[-1] if volumes else None,
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "ema200": ema(closes, 200),
        "rsi14": rsi(closes, 14),
        "atr14": atr(highs, lows, closes, 14),
        "macd": macd(closes),
        "bollinger20": bollinger(closes, 20),
    }
    return latest
