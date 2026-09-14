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


def stochastic(highs: List[float], lows: List[float], closes: List[float], k_period: int = 14, d_period: int = 3) -> Dict[str, Optional[float]]:
    """%K = position of the latest close within the high/low range of the
    last k_period candles (0-100). %D = simple moving average of %K over
    the last d_period values - smooths %K the same way a signal line does."""
    n = len(closes)
    if n < k_period:
        return {"k": None, "d": None}
    k_values: List[float] = []
    count = min(n - k_period + 1, d_period)  # only need the last few %K values for %D
    for offset in range(count):
        end = n - offset
        start = end - k_period
        window_high = max(highs[start:end])
        window_low = min(lows[start:end])
        close = closes[end - 1]
        if window_high == window_low:
            k_values.append(50.0)  # flat range - avoid division by zero, neutral reading
        else:
            k_values.append(100 * (close - window_low) / (window_high - window_low))
    k_values.reverse()  # oldest-of-the-window first, so k_values[-1] is the latest %K
    d_value = sum(k_values) / len(k_values) if len(k_values) >= 1 else None
    return {"k": k_values[-1] if k_values else None, "d": d_value}


def adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Dict[str, Optional[float]]:
    """Average Directional Index (Wilder's method) - trend STRENGTH
    (0-100, direction-agnostic), plus +DI/-DI which together show trend
    DIRECTION (whichever is higher is the dominant side)."""
    n = len(closes)
    if n < period * 2:
        return {"adx": None, "plus_di": None, "minus_di": None}
    plus_dm: List[float] = []
    minus_dm: List[float] = []
    trs: List[float] = []
    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0.0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0.0)
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if len(trs) < period:
        return {"adx": None, "plus_di": None, "minus_di": None}

    def _wilder_smooth(series: List[float], period: int) -> List[float]:
        smoothed = [sum(series[:period])]
        for v in series[period:]:
            smoothed.append(smoothed[-1] - (smoothed[-1] / period) + v)
        return smoothed

    tr_smooth = _wilder_smooth(trs, period)
    plus_dm_smooth = _wilder_smooth(plus_dm, period)
    minus_dm_smooth = _wilder_smooth(minus_dm, period)
    dx_values: List[float] = []
    for tr_s, pdm_s, mdm_s in zip(tr_smooth, plus_dm_smooth, minus_dm_smooth):
        if tr_s == 0:
            continue
        plus_di = 100 * pdm_s / tr_s
        minus_di = 100 * mdm_s / tr_s
        di_sum = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / di_sum if di_sum else 0.0)
    if len(dx_values) < period:
        return {"adx": None, "plus_di": None, "minus_di": None}
    adx_value = sum(dx_values[:period]) / period
    for dx_v in dx_values[period:]:
        adx_value = (adx_value * (period - 1) + dx_v) / period
    latest_plus_di = 100 * plus_dm_smooth[-1] / tr_smooth[-1] if tr_smooth[-1] else None
    latest_minus_di = 100 * minus_dm_smooth[-1] / tr_smooth[-1] if tr_smooth[-1] else None
    return {"adx": adx_value, "plus_di": latest_plus_di, "minus_di": latest_minus_di}


def cci(highs: List[float], lows: List[float], closes: List[float], period: int = 20) -> Optional[float]:
    """Commodity Channel Index - how far the typical price has strayed from
    its recent average, in units of mean deviation (the standard 0.015
    constant is Lambert's original scaling so +-100 roughly bounds normal
    price action)."""
    n = len(closes)
    if n < period:
        return None
    typical_prices = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
    window = typical_prices[-period:]
    sma = sum(window) / period
    mean_deviation = sum(abs(tp - sma) for tp in window) / period
    if mean_deviation == 0:
        return 0.0
    return (typical_prices[-1] - sma) / (0.015 * mean_deviation)


def vwap(highs: List[float], lows: List[float], closes: List[float], volumes: List[float]) -> Optional[float]:
    """Volume-weighted average price over the fetched candle window. NOTE:
    a "true" VWAP resets at the start of each trading session/day - this
    version is a rolling VWAP over whatever window of candles was fetched
    (e.g. the last 220 candles of that interval), not a session VWAP, since
    perpetual futures have no fixed session boundary anyway."""
    if not closes or len(highs) != len(closes) or len(lows) != len(closes) or len(volumes) != len(closes):
        return None
    total_vol = sum(volumes)
    if total_vol == 0:
        return None
    total_pv = sum(((highs[i] + lows[i] + closes[i]) / 3) * volumes[i] for i in range(len(closes)))
    return total_pv / total_vol


def taker_flow(klines: List[List[Any]]) -> Dict[str, Optional[float]]:
    """Binance futures klines already include taker buy volume per candle
    (index 9 of each row) alongside total volume (index 5) - no extra API
    call needed. taker_sell_volume = total - taker_buy_volume. A ratio
    above 0.5 means buyers were more aggressive (market/taker buy orders)
    than sellers over that candle, and vice versa."""
    if not klines:
        return {"taker_buy_volume": None, "taker_sell_volume": None, "taker_buy_ratio": None}
    last = klines[-1]
    if len(last) < 10:
        return {"taker_buy_volume": None, "taker_sell_volume": None, "taker_buy_ratio": None}
    total_vol = _safe_float(last[5])
    taker_buy_vol = _safe_float(last[9])
    if total_vol is None or taker_buy_vol is None or total_vol == 0:
        return {"taker_buy_volume": taker_buy_vol, "taker_sell_volume": None, "taker_buy_ratio": None}
    taker_sell_vol = total_vol - taker_buy_vol
    return {
        "taker_buy_volume": taker_buy_vol,
        "taker_sell_volume": taker_sell_vol,
        "taker_buy_ratio": taker_buy_vol / total_vol,
    }


def ichimoku(
    highs: List[float], lows: List[float], closes: List[float],
    tenkan_period: int = 9, kijun_period: int = 26, senkou_b_period: int = 52,
) -> Dict[str, Optional[float]]:
    """일목균형표(Ichimoku Kinko Hyo) / 구름대(Kumo cloud). Standard periods:
    전환선(Tenkan-sen)=9, 기준선(Kijun-sen)=26, 선행스팬B(Senkou Span B)=52.
    On a real chart, Senkou Span A/B are plotted 26 periods AHEAD (forming
    the cloud that currently sits ahead of price) and Chikou Span is
    plotted 26 periods BEHIND - since this returns a single "latest value"
    snapshot rather than a full plotted series, that forward/backward
    displacement isn't meaningful here; what's returned is today's
    computed value of each line, plus where the LATEST close sits relative
    to today's cloud (above/below/inside) as a simple, directly-usable
    signal."""
    n = len(closes)
    if n < kijun_period:
        return {
            "tenkan_sen": None, "kijun_sen": None,
            "senkou_span_a": None, "senkou_span_b": None, "chikou_span": None,
            "cloud_top": None, "cloud_bottom": None, "price_vs_cloud": None,
        }

    def _mid(period: int) -> Optional[float]:
        if n < period:
            return None
        return (max(highs[-period:]) + min(lows[-period:])) / 2

    tenkan = _mid(tenkan_period)
    kijun = _mid(kijun_period)
    senkou_a = (tenkan + kijun) / 2 if (tenkan is not None and kijun is not None) else None
    senkou_b = _mid(senkou_b_period)
    chikou = closes[-1]

    cloud_top = cloud_bottom = price_vs_cloud = None
    if senkou_a is not None and senkou_b is not None:
        cloud_top = max(senkou_a, senkou_b)
        cloud_bottom = min(senkou_a, senkou_b)
        price = closes[-1]
        if price > cloud_top:
            price_vs_cloud = "above"    # 구름 위 - 강세 구간
        elif price < cloud_bottom:
            price_vs_cloud = "below"    # 구름 아래 - 약세 구간
        else:
            price_vs_cloud = "inside"   # 구름 안 - 방향성 불명확/전환 구간

    return {
        "tenkan_sen": tenkan, "kijun_sen": kijun,
        "senkou_span_a": senkou_a, "senkou_span_b": senkou_b, "chikou_span": chikou,
        "cloud_top": cloud_top, "cloud_bottom": cloud_bottom, "price_vs_cloud": price_vs_cloud,
    }


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
        "stochastic": stochastic(highs, lows, closes),
        "adx14": adx(highs, lows, closes, 14),
        "cci20": cci(highs, lows, closes, 20),
        "vwap": vwap(highs, lows, closes, volumes),
        "taker_flow": taker_flow(klines),
        "ichimoku": ichimoku(highs, lows, closes),
    }
    return latest
