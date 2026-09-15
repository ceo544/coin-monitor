from __future__ import annotations
from typing import List, Optional, Dict, Any


def _safe_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def slope(values: List[float]) -> Optional[float]:
    """Simple linear-regression slope over an evenly-spaced series (x =
    0..n-1) - a noise-resistant "average rate of change per step" for a
    short window, more stable than a plain first-minus-last difference."""
    n = len(values)
    if n < 2:
        return None
    x_mean = (n - 1) / 2
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (values[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den else None


def _with_history(fn, series_list: List[List[float]], lookbacks: tuple = (1, 5)) -> Dict[str, Optional[float]]:
    """Wraps any scalar-returning indicator function (e.g. rsi, cci) so it
    also reports where that indicator stood a few candles ago and how fast
    it's moving - a single number like "RSI=63" says much less than
    "63, was 58 a candle ago, was 40 five candles ago, rising ~4.6/candle".
    Works by recomputing `fn` on progressively-truncated copies of each
    input series (cheap enough at this data scale - a few hundred candles,
    recomputed every 30s) rather than requiring every indicator function to
    maintain its own rolling-history state."""
    def _call(cut: int) -> Optional[float]:
        if cut == 0:
            args = series_list
        else:
            args = [s[:-cut] if len(s) > cut else [] for s in series_list]
        if any(len(a) == 0 for a in args):
            return None
        return fn(*args)

    current = _call(0)
    prev1 = _call(lookbacks[0])
    prev5 = _call(lookbacks[1])
    delta1 = (current - prev1) if (current is not None and prev1 is not None) else None
    delta5 = (current - prev5) if (current is not None and prev5 is not None) else None
    slope5 = (delta5 / lookbacks[1]) if delta5 is not None else None
    return {"current": current, "prev1": prev1, "prev5": prev5, "delta1": delta1, "delta5": delta5, "slope5": slope5}


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


def atr_pct(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    """ATR expressed as a percentage of price - lets volatility be compared
    across different price levels/coins, unlike raw ATR which is in the
    instrument's own price units."""
    a = atr(highs, lows, closes, period)
    if a is None or not closes or closes[-1] == 0:
        return None
    return a / closes[-1] * 100


def _macd_hist_scalar(closes: List[float]) -> Optional[float]:
    d = macd(closes)
    return d.get("histogram") if d else None


def _stoch_k_scalar(highs: List[float], lows: List[float], closes: List[float]) -> Optional[float]:
    d = stochastic(highs, lows, closes)
    return d.get("k") if d else None


def _adx_scalar(highs: List[float], lows: List[float], closes: List[float]) -> Optional[float]:
    d = adx(highs, lows, closes)
    return d.get("adx") if d else None


def stoch_rsi(closes: List[float], rsi_period: int = 14, stoch_period: int = 14, d_smooth: int = 3) -> Dict[str, Optional[float]]:
    """Stochastic RSI: applies the %K stochastic formula to a rolling
    window of RSI values (not price) - more sensitive/leading than plain
    RSI, at the cost of more noise. %D is a simple moving average of the
    last `d_smooth` %K readings."""
    n = len(closes)
    if n < rsi_period + stoch_period + d_smooth:
        return {"k": None, "d": None}

    def _k_at(cut: int) -> Optional[float]:
        sub = closes[:n - cut] if cut else closes
        m = len(sub)
        rsi_window = []
        for i in range(stoch_period):
            back = stoch_period - 1 - i
            rsi_val = rsi(sub[:m - back] if back else sub, rsi_period)
            if rsi_val is None:
                return None
            rsi_window.append(rsi_val)
        current_rsi = rsi_window[-1]
        lo, hi = min(rsi_window), max(rsi_window)
        if hi == lo:
            return 50.0
        return 100 * (current_rsi - lo) / (hi - lo)

    k_values = []
    for cut in range(d_smooth - 1, -1, -1):
        k_val = _k_at(cut)
        if k_val is None:
            return {"k": None, "d": None}
        k_values.append(k_val)
    return {"k": k_values[-1], "d": sum(k_values) / len(k_values)}


def supertrend(highs: List[float], lows: List[float], closes: List[float], period: int = 10, multiplier: float = 3.0) -> Dict[str, Any]:
    """ATR-based trend-following line that flips above/below price on
    trend changes - a simple, very commonly used "is this an uptrend or
    downtrend right now" signal. Implements the standard iterative
    final-band/flip algorithm (not just a one-off band calculation), so
    the flip logic is chart-accurate rather than an approximation."""
    n = len(closes)
    if n < period + 2:
        return {"value": None, "direction": None}

    # Wilder-smoothed ATR at every point (needed for the full band series).
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, n)]
    if len(trs) < period:
        return {"value": None, "direction": None}
    atr_series = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atr_series.append((atr_series[-1] * (period - 1) + tr) / period)
    offset = n - len(atr_series)  # atr_series[i] corresponds to closes[i + offset]

    final_upper: List[float] = []
    final_lower: List[float] = []
    trend: List[float] = []
    direction: List[str] = []
    for i, atr_v in enumerate(atr_series):
        idx = i + offset
        hl2 = (highs[idx] + lows[idx]) / 2
        basic_upper = hl2 + multiplier * atr_v
        basic_lower = hl2 - multiplier * atr_v
        if i == 0:
            final_upper.append(basic_upper)
            final_lower.append(basic_lower)
            direction.append("down")
            trend.append(basic_upper)
            continue
        prev_close = closes[idx - 1]
        fu = basic_upper if (basic_upper < final_upper[-1] or prev_close > final_upper[-1]) else final_upper[-1]
        fl = basic_lower if (basic_lower > final_lower[-1] or prev_close < final_lower[-1]) else final_lower[-1]
        final_upper.append(fu)
        final_lower.append(fl)
        close = closes[idx]
        if direction[-1] == "down":
            if close > fu:
                direction.append("up")
                trend.append(fl)
            else:
                direction.append("down")
                trend.append(fu)
        else:
            if close < fl:
                direction.append("down")
                trend.append(fu)
            else:
                direction.append("up")
                trend.append(fl)
    return {"value": trend[-1], "direction": direction[-1]}


def market_structure(highs: List[float], lows: List[float], pivot_window: int = 3) -> Dict[str, Optional[str]]:
    """시장구조(HH/HL/LH/LL) - finds recent swing highs/lows (a candle whose
    high/low is the most extreme within `pivot_window` candles on each
    side) and compares the last two of each to classify structure:
    Higher-High + Higher-Low = uptrend structure; Lower-High + Lower-Low =
    downtrend structure; anything else = mixed/transitioning.

    Also reports BOS (Break of Structure - price breaks past the most
    recent swing point IN the direction of the current structure,
    confirming the trend is continuing) and CHOCH (Change of Character -
    price breaks past the most recent swing point AGAINST the current
    structure, an early warning the trend may be reversing)."""
    n = len(highs)
    swing_highs: List[float] = []
    swing_lows: List[float] = []
    for i in range(pivot_window, n - pivot_window):
        window_h = highs[i - pivot_window:i + pivot_window + 1]
        if highs[i] == max(window_h):
            swing_highs.append(highs[i])
        window_l = lows[i - pivot_window:i + pivot_window + 1]
        if lows[i] == min(window_l):
            swing_lows.append(lows[i])
    last_high_type = None
    if len(swing_highs) >= 2:
        last_high_type = "HH" if swing_highs[-1] > swing_highs[-2] else "LH"
    last_low_type = None
    if len(swing_lows) >= 2:
        last_low_type = "HL" if swing_lows[-1] > swing_lows[-2] else "LL"
    structure = None
    if last_high_type and last_low_type:
        if last_high_type == "HH" and last_low_type == "HL":
            structure = "uptrend"
        elif last_high_type == "LH" and last_low_type == "LL":
            structure = "downtrend"
        else:
            structure = "mixed"

    return {
        "last_high_type": last_high_type, "last_low_type": last_low_type, "structure": structure,
        "_swing_highs": swing_highs, "_swing_lows": swing_lows,
    }


def market_structure_breaks(structure_result: Dict[str, Any], closes: List[float]) -> Dict[str, Optional[str]]:
    """BOS/CHOCH computed from market_structure()'s swing points against the
    latest close - kept as a separate call (rather than folded into
    market_structure() itself) so market_structure() doesn't need a closes
    series just to classify HH/HL/LH/LL, which only needs highs/lows."""
    swing_highs = structure_result.get("_swing_highs") or []
    swing_lows = structure_result.get("_swing_lows") or []
    structure = structure_result.get("structure")
    price = closes[-1] if closes else None
    if price is None or not swing_highs or not swing_lows:
        return {"bos": None, "choch": None}
    last_swing_high = swing_highs[-1]
    last_swing_low = swing_lows[-1]
    bos = choch = None
    if structure == "uptrend":
        if price > last_swing_high:
            bos = "bullish"
        elif price < last_swing_low:
            choch = "bearish"
    elif structure == "downtrend":
        if price < last_swing_low:
            bos = "bearish"
        elif price > last_swing_high:
            choch = "bullish"
    return {"bos": bos, "choch": choch}


def cvd_from_klines(klines: List[List[Any]], window: int = 5) -> Dict[str, Optional[float]]:
    """Cumulative Volume Delta: running total of (taker buy volume - taker
    sell volume) across the fetched candle window. NOTE: this is a
    rolling-window CVD reset to 0 at the start of whatever candles were
    fetched this cycle (up to 220 candles back), NOT a session/day-reset
    CVD and NOT persisted across app restarts - useful as a within-window
    momentum-of-aggression feature, not an absolute all-time value."""
    total = 0.0
    series: List[float] = []
    for row in klines:
        if len(row) < 10:
            continue
        vol = _safe_float(row[5])
        buy = _safe_float(row[9])
        if vol is None or buy is None:
            continue
        total += buy - (vol - buy)
        series.append(total)
    if not series:
        return {"cvd": None, "cvd_slope5": None}
    cvd_slope5 = (series[-1] - series[-1 - window]) / window if len(series) > window else None
    return {"cvd": series[-1], "cvd_slope5": cvd_slope5}


def dema(values: List[float], period: int) -> Optional[float]:
    """Double EMA - 2*EMA - EMA(EMA), reacts faster to price changes than a
    plain EMA of the same period (less lag, at the cost of more noise)."""
    if len(values) < period * 2:
        return None
    e1 = ema(values, period)
    # Need a full EMA-of-EMA series, not just the final EMA value, so build
    # the EMA1 series first then EMA the result.
    k = 2 / (period + 1)
    ema1_series = [sum(values[:period]) / period]
    for price in values[period:]:
        ema1_series.append(price * k + ema1_series[-1] * (1 - k))
    if len(ema1_series) < period:
        return None
    e2 = ema(ema1_series, period)
    if e1 is None or e2 is None:
        return None
    return 2 * e1 - e2


def hma(values: List[float], period: int) -> Optional[float]:
    """Hull Moving Average - a weighted-MA construction designed to track
    price closely with much less lag than a standard MA of the same
    period: HMA = WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    def _wma(series: List[float], p: int) -> Optional[float]:
        if len(series) < p:
            return None
        window = series[-p:]
        weights = list(range(1, p + 1))
        return sum(w * v for w, v in zip(weights, window)) / sum(weights)

    def _wma_series(series: List[float], p: int, count: int) -> Optional[List[float]]:
        if len(series) < p + count - 1:
            return None
        return [_wma(series[:len(series) - (count - 1 - i)] if (count - 1 - i) else series, p) for i in range(count)]

    n = period
    half = max(1, n // 2)
    sqrt_n = max(1, round(n ** 0.5))
    if len(values) < n + sqrt_n:
        return None
    wma_half_series = _wma_series(values, half, sqrt_n)
    wma_full_series = _wma_series(values, n, sqrt_n)
    if wma_half_series is None or wma_full_series is None or any(v is None for v in wma_half_series + wma_full_series):
        return None
    raw_series = [2 * h - f for h, f in zip(wma_half_series, wma_full_series)]
    return _wma(raw_series, sqrt_n)


def roc(values: List[float], period: int = 12) -> Optional[float]:
    """Rate of Change - percentage price change over `period` candles, a
    simple, direct read of price "acceleration"."""
    if len(values) <= period or values[-1 - period] == 0:
        return None
    return (values[-1] - values[-1 - period]) / values[-1 - period] * 100


def keltner_channel(highs: List[float], lows: List[float], closes: List[float], period: int = 20, multiplier: float = 2.0) -> Dict[str, Optional[float]]:
    """EMA-and-ATR based channel (unlike Bollinger's SMA-and-stddev). When
    the Bollinger Band sits INSIDE the Keltner Channel, that's the standard
    "squeeze" reading (low volatility, often precedes a breakout)."""
    mid = ema(closes, period)
    band_atr = atr(highs, lows, closes, period)
    if mid is None or band_atr is None:
        return {"upper": None, "middle": None, "lower": None}
    return {"upper": mid + multiplier * band_atr, "middle": mid, "lower": mid - multiplier * band_atr}


def bb_keltner_squeeze(bb: Dict[str, Optional[float]], kc: Dict[str, Optional[float]]) -> Optional[bool]:
    """True when the Bollinger Band is fully inside the Keltner Channel -
    the textbook volatility-squeeze condition."""
    if None in (bb.get("upper"), bb.get("lower"), kc.get("upper"), kc.get("lower")):
        return None
    return bb["upper"] < kc["upper"] and bb["lower"] > kc["lower"]


def volume_ma(volumes: List[float], period: int = 20) -> Dict[str, Optional[float]]:
    """Volume moving average plus how the latest candle's volume compares
    to it (a ratio well above 1 flags a volume spike)."""
    if len(volumes) < period:
        return {"volume_ma": None, "volume_ratio": None}
    ma_val = sum(volumes[-period:]) / period
    latest = volumes[-1]
    ratio = (latest / ma_val) if ma_val else None
    return {"volume_ma": ma_val, "volume_ratio": ratio}


def pivot_levels(highs: List[float], lows: List[float], closes: List[float], pivot_window: int = 3) -> Dict[str, Optional[float]]:
    """The most recent confirmed swing high/low (actual price levels, not
    just the HH/LH/HL/LL classification that market_structure() gives),
    plus how far current price sits from each - useful as lightweight
    support/resistance and "how close to a breakout level" features."""
    n = len(highs)
    last_high = last_low = None
    for i in range(n - pivot_window - 1, pivot_window - 1, -1):
        if last_high is None:
            window_h = highs[i - pivot_window:i + pivot_window + 1]
            if highs[i] == max(window_h):
                last_high = highs[i]
        if last_low is None:
            window_l = lows[i - pivot_window:i + pivot_window + 1]
            if lows[i] == min(window_l):
                last_low = lows[i]
        if last_high is not None and last_low is not None:
            break
    price = closes[-1] if closes else None
    dist_to_high = (price - last_high) if (price is not None and last_high is not None) else None
    dist_to_low = (price - last_low) if (price is not None and last_low is not None) else None
    dist_to_high_pct = (dist_to_high / last_high * 100) if (dist_to_high is not None and last_high) else None
    dist_to_low_pct = (dist_to_low / last_low * 100) if (dist_to_low is not None and last_low) else None
    return {
        "recent_pivot_high": last_high, "recent_pivot_low": last_low,
        "dist_to_pivot_high_pct": dist_to_high_pct, "dist_to_pivot_low_pct": dist_to_low_pct,
    }


def vwap_distance_pct(closes: List[float], vwap_value: Optional[float]) -> Optional[float]:
    """How far (in %) the current price sits above/below VWAP - positive
    means trading above the volume-weighted average (buyers in control of
    that window), negative means below."""
    if vwap_value is None or not closes or vwap_value == 0:
        return None
    return (closes[-1] - vwap_value) / vwap_value * 100


def ema_alignment(closes: List[float], periods: tuple = (9, 21, 50, 100, 200)) -> Dict[str, Any]:
    """정배열/역배열 - whether the requested EMAs are stacked in ascending
    order (bullish "정배열": fast > slow, price trending up cleanly) or
    descending order (bearish "역배열"), plus whether the fastest two just
    crossed (a simple, commonly-watched signal in its own right)."""
    values = {p: ema(closes, p) for p in periods}
    if any(v is None for v in values.values()):
        return {"alignment": None, "fast_cross": None}
    ordered = [values[p] for p in periods]  # fastest period first
    bullish = all(ordered[i] > ordered[i + 1] for i in range(len(ordered) - 1))
    bearish = all(ordered[i] < ordered[i + 1] for i in range(len(ordered) - 1))
    alignment = "bullish" if bullish else ("bearish" if bearish else "mixed")

    fast_cross = None
    if len(periods) >= 2:
        fast_p, slow_p = periods[0], periods[1]
        if len(closes) > 1:
            prev_fast = ema(closes[:-1], fast_p)
            prev_slow = ema(closes[:-1], slow_p)
            cur_fast, cur_slow = values[fast_p], values[slow_p]
            if None not in (prev_fast, prev_slow, cur_fast, cur_slow):
                if prev_fast <= prev_slow and cur_fast > cur_slow:
                    fast_cross = "golden"   # fast crossed above slow
                elif prev_fast >= prev_slow and cur_fast < cur_slow:
                    fast_cross = "death"    # fast crossed below slow
                else:
                    fast_cross = "none"
    return {"alignment": alignment, "fast_cross": fast_cross, "values": values}


def di_cross(highs: List[float], lows: List[float], closes: List[float]) -> Optional[str]:
    """Whether +DI/-DI (from adx()) just crossed - a commonly-watched
    trend-direction-change signal distinct from the ADX strength value
    itself."""
    if len(closes) < 3:
        return None
    cur = adx(highs, lows, closes)
    prev = adx(highs[:-1], lows[:-1], closes[:-1])
    if None in (cur.get("plus_di"), cur.get("minus_di"), prev.get("plus_di"), prev.get("minus_di")):
        return None
    if prev["plus_di"] <= prev["minus_di"] and cur["plus_di"] > cur["minus_di"]:
        return "bullish"
    if prev["plus_di"] >= prev["minus_di"] and cur["plus_di"] < cur["minus_di"]:
        return "bearish"
    return "none"


def price_returns(closes: List[float], periods: tuple = (1, 3, 5, 15)) -> Dict[str, Optional[float]]:
    """percent return over each requested candle count, plus the latest
    single-candle % change - simple, direct momentum reads."""
    out: Dict[str, Optional[float]] = {}
    if len(closes) >= 2 and closes[-2]:
        out["price_change_pct"] = (closes[-1] - closes[-2]) / closes[-2] * 100
    else:
        out["price_change_pct"] = None
    for p in periods:
        if len(closes) > p and closes[-1 - p]:
            out[f"return_{p}"] = (closes[-1] - closes[-1 - p]) / closes[-1 - p] * 100
        else:
            out[f"return_{p}"] = None
    return out


def recent_high_low(highs: List[float], lows: List[float], closes: List[float], period: int = 20) -> Dict[str, Optional[float]]:
    """Simple rolling highest-high/lowest-low over the last `period`
    candles (distinct from the swing-pivot-based pivot_levels() - this is
    a plain rolling extreme, not a confirmed reversal point), plus how far
    (in %) the current price sits from each."""
    if len(highs) < period or not closes:
        return {"highest_high": None, "lowest_low": None, "dist_to_highest_high_pct": None, "dist_to_lowest_low_pct": None}
    hh = max(highs[-period:])
    ll = min(lows[-period:])
    price = closes[-1]
    return {
        "highest_high": hh, "lowest_low": ll,
        "dist_to_highest_high_pct": ((price - hh) / hh * 100) if hh else None,
        "dist_to_lowest_low_pct": ((price - ll) / ll * 100) if ll else None,
    }


def ema_distance_features(closes: List[float]) -> Dict[str, Optional[float]]:
    """(close - EMA) / close for each core EMA, plus the distance between
    EMA pairs as a % - all normalized so they're comparable across
    different BTC price eras (spec section 28: don't depend on absolute
    price level)."""
    e9, e20, e21, e50, e100, e200 = (ema(closes, p) for p in (9, 20, 21, 50, 100, 200))
    price = closes[-1] if closes else None

    def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
        return ((a - b) / b * 100) if (a is not None and b) else None

    return {
        "close_vs_ema9_pct": _pct(price, e9), "close_vs_ema20_pct": _pct(price, e20),
        "close_vs_ema50_pct": _pct(price, e50), "close_vs_ema100_pct": _pct(price, e100),
        "close_vs_ema200_pct": _pct(price, e200),
        "ema9_ema21_pct": _pct(e9, e21), "ema20_ema50_pct": _pct(e20, e50), "ema50_ema200_pct": _pct(e50, e200),
    }


def macd_cross(closes: List[float]) -> Optional[str]:
    """Whether the MACD line just crossed its signal line - the classic
    MACD trade trigger, distinct from just watching the histogram sign."""
    if len(closes) < 3:
        return None
    cur = macd(closes)
    prev = macd(closes[:-1])
    if None in (cur.get("macd"), cur.get("signal"), prev.get("macd"), prev.get("signal")):
        return None
    if prev["macd"] <= prev["signal"] and cur["macd"] > cur["signal"]:
        return "bullish"
    if prev["macd"] >= prev["signal"] and cur["macd"] < cur["signal"]:
        return "bearish"
    return "none"


def stoch_cross(highs: List[float], lows: List[float], closes: List[float]) -> Optional[str]:
    """Whether Stochastic %K just crossed %D."""
    if len(closes) < 3:
        return None
    cur = stochastic(highs, lows, closes)
    prev = stochastic(highs[:-1], lows[:-1], closes[:-1])
    if None in (cur.get("k"), cur.get("d"), prev.get("k"), prev.get("d")):
        return None
    if prev["k"] <= prev["d"] and cur["k"] > cur["d"]:
        return "bullish"
    if prev["k"] >= prev["d"] and cur["k"] < cur["d"]:
        return "bearish"
    return "none"


def cci_cross(highs: List[float], lows: List[float], closes: List[float], period: int = 20) -> Dict[str, Optional[str]]:
    """Whether CCI just crossed the zero line, +100, or -100 - the three
    levels CCI is conventionally read against."""
    if len(closes) < 3:
        return {"zero_cross": None, "plus100_cross": None, "minus100_cross": None}
    cur = cci(highs, lows, closes, period)
    prev = cci(highs[:-1], lows[:-1], closes[:-1], period)
    if cur is None or prev is None:
        return {"zero_cross": None, "plus100_cross": None, "minus100_cross": None}

    def _cross(level: float) -> str:
        if prev <= level < cur:
            return "up"
        if prev >= level > cur:
            return "down"
        return "none"

    return {"zero_cross": _cross(0), "plus100_cross": _cross(100), "minus100_cross": _cross(-100)}


def bollinger_extra(closes: List[float], bb: Dict[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """BB width (absolute and %), and %B position (0 = at lower band, 1 =
    at upper band, can go outside 0-1 when price pierces a band)."""
    upper, middle, lower = bb.get("upper"), bb.get("middle"), bb.get("lower")
    price = closes[-1] if closes else None
    if None in (upper, middle, lower) or middle == 0:
        return {"bb_width": None, "bb_width_pct": None, "bb_position": None}
    width = upper - lower
    position = ((price - lower) / width) if (price is not None and width) else None
    return {"bb_width": width, "bb_width_pct": width / middle * 100, "bb_position": position}


def vwap_extra(
    highs: List[float], lows: List[float], closes: List[float], volumes: List[float],
    vwap_value: Optional[float], atr_value: Optional[float],
) -> Dict[str, Optional[float]]:
    """Raw and ATR-normalized distance from VWAP, plus a lightweight VWAP
    slope (current VWAP vs. VWAP computed one candle back, same rolling-
    window definition as vwap() itself)."""
    price = closes[-1] if closes else None
    diff = (price - vwap_value) if (price is not None and vwap_value is not None) else None
    diff_atr = (diff / atr_value) if (diff is not None and atr_value) else None
    vwap_slope = None
    if len(closes) > 1:
        prev_vwap = vwap(highs[:-1], lows[:-1], closes[:-1], volumes[:-1])
        if prev_vwap is not None and vwap_value is not None:
            vwap_slope = vwap_value - prev_vwap
    return {"vwap_diff": diff, "vwap_diff_atr": diff_atr, "vwap_slope": vwap_slope}


def volume_extra(volumes: List[float]) -> Dict[str, Optional[float]]:
    """Multiple volume MAs, volume delta (vs. prior candle), a simple
    slope, and a plain boolean "is this a volume spike" flag (ratio to the
    20-period MA above 2x)."""
    ma5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else None
    ma20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else None
    ma50 = sum(volumes[-50:]) / 50 if len(volumes) >= 50 else None
    latest = volumes[-1] if volumes else None
    delta = (volumes[-1] - volumes[-2]) if len(volumes) >= 2 else None
    vol_slope = slope(volumes[-5:]) if len(volumes) >= 5 else None
    ratio = (latest / ma20) if (latest is not None and ma20) else None
    spike = (ratio is not None and ratio >= 2.0)
    return {
        "volume_ma_5": ma5, "volume_ma_20": ma20, "volume_ma_50": ma50,
        "volume_delta": delta, "volume_slope": vol_slope, "volume_spike": spike if ratio is not None else None,
    }


def cvd_divergence(closes: List[float], cvd_series_last2: Optional[tuple]) -> Optional[str]:
    """Price/CVD divergence over the most recent candle: price up while CVD
    down (or vice versa) suggests the move isn't backed by aggressive
    order flow - a classic (if noisy) reversal warning. cvd_series_last2 =
    (cvd_prev, cvd_current); pass None if unavailable."""
    if len(closes) < 2 or not cvd_series_last2 or None in cvd_series_last2:
        return None
    price_up = closes[-1] > closes[-2]
    cvd_up = cvd_series_last2[1] > cvd_series_last2[0]
    if price_up and not cvd_up:
        return "bearish_divergence"
    if not price_up and cvd_up:
        return "bullish_divergence"
    return "none"


def oi_price_classification(price_change_pct: Optional[float], oi_change_pct: Optional[float]) -> Optional[str]:
    """The standard 4-way read of price move + OI move together (e.g. price
    up + OI up = new longs opening = a "confirmed" up move; price up + OI
    down = shorts covering, generally considered a weaker up move)."""
    if price_change_pct is None or oi_change_pct is None:
        return None
    price_up = price_change_pct > 0
    oi_up = oi_change_pct > 0
    if price_up and oi_up:
        return "price_up_oi_up"
    if price_up and not oi_up:
        return "price_up_oi_down"
    if not price_up and oi_up:
        return "price_down_oi_up"
    return "price_down_oi_down"


def funding_extra(current_rate: Optional[float], history: List[Dict[str, Any]], extreme_threshold: float = 0.0005) -> Dict[str, Any]:
    """Funding rate change vs. the most recent past settlement (the last
    entry of `history`, which is expected oldest-to-newest as Binance's own
    fundingRate history endpoint returns it), and a simple "is this an
    extreme funding rate" flag (default threshold 0.05%, consistent with
    commonly-cited "expensive to hold" levels)."""
    prev_rate = None
    if history:
        try:
            prev_rate = float(history[-1].get("fundingRate"))
        except (TypeError, ValueError, AttributeError):
            prev_rate = None
    change = (current_rate - prev_rate) if (current_rate is not None and prev_rate is not None) else None
    extreme = (current_rate is not None and abs(current_rate) >= extreme_threshold)
    return {"funding_change": change, "funding_extreme": extreme}


def ichimoku_extra(ichi: Dict[str, Optional[float]], price: Optional[float]) -> Dict[str, Optional[float]]:
    """Cloud thickness (how wide the Senkou A/B gap is - a thin cloud is
    weak support/resistance, a thick one is stronger) and distance from
    price to the nearest edge of the cloud."""
    top, bottom = ichi.get("cloud_top"), ichi.get("cloud_bottom")
    if top is None or bottom is None or price is None:
        return {"cloud_thickness": None, "distance_to_cloud_pct": None}
    thickness = top - bottom
    if price > top:
        dist = (price - top) / top * 100
    elif price < bottom:
        dist = (price - bottom) / bottom * 100
    else:
        dist = 0.0
    return {"cloud_thickness": thickness, "distance_to_cloud_pct": dist}


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
    bb20 = bollinger(closes, 20)
    kc20 = keltner_channel(highs, lows, closes, 20)
    vwap_val = vwap(highs, lows, closes, volumes)
    atr14_val = atr(highs, lows, closes, 14)
    ichi = ichimoku(highs, lows, closes)
    cvd_d = cvd_from_klines(klines)
    _ms = market_structure(highs, lows)
    latest = {
        "close": closes[-1] if closes else None,
        "high": highs[-1] if highs else None,
        "low": lows[-1] if lows else None,
        "volume": volumes[-1] if volumes else None,
        "ema9": ema(closes, 9), "ema21": ema(closes, 21),
        "ema20": ema(closes, 20),
        "ema50": ema(closes, 50),
        "ema100": ema(closes, 100),
        "ema200": ema(closes, 200),
        "ema_alignment": ema_alignment(closes),
        "ema_distance": ema_distance_features(closes),
        "dema20": dema(closes, 20), "hma20": hma(closes, 20),
        "rsi7": rsi(closes, 7), "rsi14": rsi(closes, 14), "rsi21": rsi(closes, 21),
        "roc12": roc(closes, 12),
        "atr7": atr(highs, lows, closes, 7), "atr14": atr14_val,
        "macd": macd(closes),
        "macd_cross": macd_cross(closes),
        "bollinger20": bb20,
        "bollinger_extra": bollinger_extra(closes, bb20),
        "keltner20": kc20,
        "bb_keltner_squeeze": bb_keltner_squeeze(bb20, kc20),
        "stochastic": stochastic(highs, lows, closes),
        "stoch_cross": stoch_cross(highs, lows, closes),
        "adx14": adx(highs, lows, closes, 14),
        "di_cross": di_cross(highs, lows, closes),
        "cci14": cci(highs, lows, closes, 14), "cci20": cci(highs, lows, closes, 20),
        "cci_cross": cci_cross(highs, lows, closes, 20),
        "vwap": vwap_val,
        "vwap_distance_pct": vwap_distance_pct(closes, vwap_val),
        "vwap_extra": vwap_extra(highs, lows, closes, volumes, vwap_val, atr14_val),
        "volume_ma20": volume_ma(volumes, 20),
        "volume_extra": volume_extra(volumes),
        "taker_flow": taker_flow(klines),
        "ichimoku": ichi,
        "ichimoku_extra": ichimoku_extra(ichi, closes[-1] if closes else None),
        "atr_pct": atr_pct(highs, lows, closes, 14),
        "stoch_rsi": stoch_rsi(closes),
        "supertrend": supertrend(highs, lows, closes),
        "market_structure": {k: v for k, v in _ms.items() if not k.startswith("_")},
        "market_structure_breaks": market_structure_breaks(_ms, closes),
        "pivot_levels": pivot_levels(highs, lows, closes),
        "recent_high_low": recent_high_low(highs, lows, closes, 20),
        "price_returns": price_returns(closes),
        "cvd": cvd_d,
        "cvd_divergence": cvd_divergence(
            closes,
            (cvd_from_klines(klines[:-1]).get("cvd") if len(klines) > 1 else None, cvd_d.get("cvd")),
        ),
        # "변화량/기울기" - current value isn't enough to tell whether an
        # indicator is freshly turning or has been sitting flat; each of
        # these reports current / 1-candle-ago / 5-candles-ago / deltas /
        # slope so that can be read directly instead of inferred later from
        # separate rows.
        "rsi14_history": _with_history(rsi, [closes]),
        "macd_hist_history": _with_history(_macd_hist_scalar, [closes]),
        "atr_pct_history": _with_history(atr_pct, [highs, lows, closes]),
        "cci20_history": _with_history(cci, [highs, lows, closes]),
        "stoch_k_history": _with_history(_stoch_k_scalar, [highs, lows, closes]),
        "adx14_history": _with_history(_adx_scalar, [highs, lows, closes]),
    }
    return latest
