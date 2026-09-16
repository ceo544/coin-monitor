"""
2025년 BTC 데이터로 A/B/C/D 진입등급 공식 백테스트
================================================

중요: 이 스크립트는 "E-RANG이 2025년에 뭐라고 신호를 줬을지"를 재구성하는 게
아닙니다 (그건 원천적으로 불가능합니다 - E-RANG 신호는 실시간 화면 색상이라
과거 기록이 없습니다). 대신 우리 앱의 assess_entry_risk() 등급 공식 자체를
독립적인 "만약 이 조건이면 롱/숏 진입" 가상 전략으로 놓고, 그 공식이 실제로
방향을 얼마나 잘 맞췄는지 순수하게 바이낸스 데이터만으로 검증합니다.

즉 여기서 나오는 "A등급 승률 62%" 같은 숫자는 "E-RANG+등급 조합"이 아니라
"등급 공식 단독"의 예측력을 말하는 것입니다.

사용법:
    pip install requests pandas numpy
    python backtest_2025.py --start 2025-01-01 --end 2025-09-01 --market spot
    (또는 --market futures 로 무기한 선물 데이터 사용)

출력:
    backtest_result.csv   - 매 1시간 봉마다 LONG/SHORT 등급 + 이후 수익률
    backtest_summary.txt  - 등급별 집계 (평균수익률, 승률, 표본수)

------------------------------------------------------------------------------
데이터 출처 안내 (v2): 바이낸스 실거래 API(api.binance.com / fapi.binance.com)는
미국 리전에서 호출하면 파생상품 관련 규제 때문에 HTTP 451로 차단됩니다 -
GitHub Actions의 기본 러너가 미국 데이터센터에 있어서 실제로 이 문제가
발생했습니다. 그래서 이 스크립트는 대신 data.binance.vision (바이낸스가
공개 배포하는 과거 시세 아카이브 - 실거래 API가 아니라 그냥 정적 파일
다운로드라 지역 규제 차단 대상이 아닙니다)에서 월별 zip을 받아옵니다.
------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import io
import time
import zipfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import requests

SYMBOL = "BTCUSDT"
ARCHIVE_BASE = "https://data.binance.vision/data"


# ---------------------------------------------------------------------------
# 1. 데이터 수집 (look-ahead bias 없음 - 매 시점 그때까지의 캔들만 사용)
#
# data.binance.vision의 월별 klines zip을 하나씩 받아서 이어붙입니다. 실거래
# API처럼 지역 차단이 없는 정적 파일 아카이브라 GitHub Actions에서도 정상
# 동작합니다. 각 zip 안에는 헤더가 있는 버전/없는 버전이 섞여 있을 수 있어
# 방어적으로 둘 다 처리합니다.
# ---------------------------------------------------------------------------
KLINE_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
              "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]


def _month_range(start_ms: int, end_ms: int):
    start = pd.Timestamp(start_ms, unit="ms", tz="UTC").replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = pd.Timestamp(end_ms, unit="ms", tz="UTC")
    cur = start
    while cur < end:
        yield cur.year, cur.month
        cur = (cur + pd.DateOffset(months=1)).replace(day=1)


def _fetch_month_csv(interval: str, year: int, month: int, market: str) -> pd.DataFrame:
    market_path = "spot" if market == "spot" else "futures/um"
    fname = f"{SYMBOL}-{interval}-{year}-{month:02d}"
    url = f"{ARCHIVE_BASE}/{market_path}/monthly/klines/{SYMBOL}/{interval}/{fname}.zip"
    resp = requests.get(url, timeout=30)
    if resp.status_code == 404:
        return pd.DataFrame(columns=KLINE_COLS)  # 아직 해당 월 데이터가 아카이브에 없음 (너무 최근 달 등)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        csv_name = [n for n in zf.namelist() if n.endswith(".csv")][0]
        with zf.open(csv_name) as f:
            raw = pd.read_csv(f, header=None)
    # 헤더 행이 포함된 최신 포맷 zip은 첫 행의 첫 값이 숫자가 아니므로 걸러냄
    if not str(raw.iloc[0, 0]).strip().lstrip("-").isdigit():
        raw = raw.iloc[1:].reset_index(drop=True)
    raw.columns = KLINE_COLS[: raw.shape[1]]
    return raw


def fetch_klines(interval: str, start_ms: int, end_ms: int, market: str = "spot") -> pd.DataFrame:
    frames = []
    for year, month in _month_range(start_ms, end_ms):
        try:
            month_df = _fetch_month_csv(interval, year, month, market)
        except Exception as exc:
            print(f"  경고: {year}-{month:02d} {interval} 데이터 다운로드 실패 ({exc}) - 건너뜀")
            continue
        if not month_df.empty:
            frames.append(month_df)
        time.sleep(0.2)  # 아카이브 서버 여유
    if not frames:
        return pd.DataFrame(columns=KLINE_COLS + ["taker_buy_ratio"])
    df = pd.concat(frames, ignore_index=True)
    for c in ("open", "high", "low", "close", "volume", "taker_buy_base"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # open_time은 파일 버전에 따라 ms 또는 us(마이크로초) 단위가 섞여 있어 자릿수로 구분
    open_time_num = pd.to_numeric(df["open_time"], errors="coerce")
    unit = "us" if open_time_num.dropna().astype("int64").astype(str).str.len().median() >= 16 else "ms"
    df["open_time"] = pd.to_datetime(open_time_num, unit=unit, utc=True)
    df["taker_buy_ratio"] = np.where(df["volume"] > 0, df["taker_buy_base"] / df["volume"], np.nan)
    df = df.dropna(subset=["open_time", "close"])
    df = df.drop_duplicates(subset="open_time").sort_values("open_time").reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# 2. 지표 계산 (main.py / indicators.py 의 로직과 최대한 동일하게 이식)
# ---------------------------------------------------------------------------
def calc_supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    hl2 = (df["high"] + df["low"]) / 2
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    basic_upper = hl2 + mult * atr
    basic_lower = hl2 - mult * atr

    n = len(df)
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.full(n, None, dtype=object)
    close = df["close"].values
    bu, bl = basic_upper.values, basic_lower.values

    start = period
    if start >= n:
        return pd.Series(direction, index=df.index)
    final_upper[start] = bu[start]
    final_lower[start] = bl[start]
    direction[start] = "down"
    for i in range(start + 1, n):
        if np.isnan(bu[i]) or np.isnan(final_upper[i - 1]):
            continue
        final_upper[i] = bu[i] if (bu[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
        final_lower[i] = bl[i] if (bl[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]
        if direction[i - 1] == "down":
            direction[i] = "up" if close[i] > final_upper[i] else "down"
        else:
            direction[i] = "down" if close[i] < final_lower[i] else "up"
    return pd.Series(direction, index=df.index)


def calc_adx(df: pd.DataFrame, period: int = 14):
    up_move = df["high"].diff()
    down_move = -df["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_val, plus_di, minus_di


def calc_market_structure(df: pd.DataFrame, pivot_window: int = 3) -> pd.Series:
    highs, lows = df["high"].values, df["low"].values
    n = len(df)
    swing_high_type = np.full(n, None, dtype=object)  # 'HH' or 'LH' at the index it was confirmed
    swing_low_type = np.full(n, None, dtype=object)
    last_swing_high = last_swing_low = None
    last_high_type = last_low_type = None
    structure = np.full(n, None, dtype=object)
    for i in range(pivot_window, n - pivot_window):
        window_h = highs[i - pivot_window:i + pivot_window + 1]
        if highs[i] == window_h.max():
            if last_swing_high is not None:
                last_high_type = "HH" if highs[i] > last_swing_high else "LH"
            last_swing_high = highs[i]
        window_l = lows[i - pivot_window:i + pivot_window + 1]
        if lows[i] == window_l.min():
            if last_swing_low is not None:
                last_low_type = "HL" if lows[i] > last_swing_low else "LL"
            last_swing_low = lows[i]
        if last_high_type == "HH" and last_low_type == "HL":
            structure[i] = "uptrend"
        elif last_high_type == "LH" and last_low_type == "LL":
            structure[i] = "downtrend"
        elif last_high_type and last_low_type:
            structure[i] = "mixed"
        # forward-fill so every bar after a confirmed pivot carries the
        # last known structure, without ever looking ahead of "i"
        if i > 0 and structure[i] is None:
            structure[i] = structure[i - 1]
    return pd.Series(structure, index=df.index)


def calc_ichimoku_cloud_position(df: pd.DataFrame) -> pd.Series:
    tenkan = (df["high"].rolling(9).max() + df["low"].rolling(9).min()) / 2
    kijun = (df["high"].rolling(26).max() + df["low"].rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((df["high"].rolling(52).max() + df["low"].rolling(52).min()) / 2).shift(26)
    cloud_top = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1)
    cloud_bottom = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1)
    pos = pd.Series(np.where(df["close"] > cloud_top, "above",
                     np.where(df["close"] < cloud_bottom, "below", "inside")), index=df.index)
    pos[cloud_top.isna() | cloud_bottom.isna()] = None
    return pos


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["supertrend_dir"] = calc_supertrend(df)
    out["adx"], out["plus_di"], out["minus_di"] = calc_adx(df)
    out["structure"] = calc_market_structure(df)
    out["cloud_pos"] = calc_ichimoku_cloud_position(df)
    return out


# ---------------------------------------------------------------------------
# 3. 등급 공식 (main.py의 assess_entry_risk()과 동일한 로직 - 단, 백테스트에는
#    "신호 시작 후 경과시간" 개념 자체가 없으므로 신선도 항목은 제외하고
#    지표 정합성 항목들만 채점합니다)
# ---------------------------------------------------------------------------
def grade_at_row(side: str, row15: pd.Series, row1h: pd.Series, row4h: pd.Series) -> tuple:
    good_dir = "up" if side == "long" else "down"
    bad_dir = "down" if side == "long" else "up"
    score = 0.0
    max_score = 0.0

    for r in (row15, row1h, row4h):
        d = r.get("supertrend_dir")
        if d in ("up", "down"):
            max_score += 1
            if d == good_dir:
                score += 1

    for r in (row15, row1h):
        adx_v, pdi, mdi = r.get("adx"), r.get("plus_di"), r.get("minus_di")
        if pd.notna(adx_v) and pd.notna(pdi) and pd.notna(mdi) and adx_v >= 20:
            max_score += 1
            favorable = (pdi > mdi) if side == "long" else (mdi > pdi)
            if favorable:
                score += 1

    taker = row15.get("taker_buy_ratio")
    if pd.notna(taker):
        max_score += 1
        favorable = (taker >= 0.55) if side == "long" else (taker <= 0.45)
        if favorable:
            score += 1

    structure = row15.get("structure")
    good_structure = "uptrend" if side == "long" else "downtrend"
    bad_structure = "downtrend" if side == "long" else "uptrend"
    if structure in (good_structure, bad_structure):
        max_score += 1
        if structure == good_structure:
            score += 1

    cloud = row1h.get("cloud_pos")
    good_cloud = "above" if side == "long" else "below"
    bad_cloud = "below" if side == "long" else "above"
    if cloud in (good_cloud, bad_cloud):
        max_score += 1
        if cloud == good_cloud:
            score += 1

    if max_score == 0:
        return None, None
    pct = score / max_score * 100
    if pct >= 75:
        grade = "A"
    elif pct >= 55:
        grade = "B"
    elif pct >= 35:
        grade = "C"
    else:
        grade = "D"
    return grade, round(pct, 1)


def factor_signals_at_row(row15: pd.Series, row1h: pd.Series, row4h: pd.Series) -> dict:
    """개별 지표 하나하나가 (다른 지표들과 무관하게) 어느 방향을 가리키는지
    - "up"/"down"/None - 를 뽑아냅니다. 등급 공식은 이걸 전부 합산한 것인데,
    합산하기 전에 각 지표 단독으로 방향 예측력이 있는지 따로 검증하기 위함."""
    out = {}
    out["supertrend_15m"] = row15.get("supertrend_dir") if row15.get("supertrend_dir") in ("up", "down") else None
    out["supertrend_1h"] = row1h.get("supertrend_dir") if row1h.get("supertrend_dir") in ("up", "down") else None
    out["supertrend_4h"] = row4h.get("supertrend_dir") if row4h.get("supertrend_dir") in ("up", "down") else None

    for tf, r in (("15m", row15), ("1h", row1h)):
        adx_v, pdi, mdi = r.get("adx"), r.get("plus_di"), r.get("minus_di")
        if pd.notna(adx_v) and pd.notna(pdi) and pd.notna(mdi) and adx_v >= 20:
            out[f"adx_di_{tf}"] = "up" if pdi > mdi else "down"
        else:
            out[f"adx_di_{tf}"] = None

    taker = row15.get("taker_buy_ratio")
    if pd.notna(taker):
        if taker >= 0.55:
            out["taker_flow_15m"] = "up"
        elif taker <= 0.45:
            out["taker_flow_15m"] = "down"
        else:
            out["taker_flow_15m"] = None
    else:
        out["taker_flow_15m"] = None

    structure = row15.get("structure")
    out["structure_15m"] = "up" if structure == "uptrend" else ("down" if structure == "downtrend" else None)

    cloud = row1h.get("cloud_pos")
    out["ichimoku_1h"] = "up" if cloud == "above" else ("down" if cloud == "below" else None)
    return out


# ---------------------------------------------------------------------------
# 4. 백테스트 실행: 매 1시간 봉마다 LONG/SHORT 등급 계산 후, 이후 수익률 기록
# ---------------------------------------------------------------------------
def run_backtest(df15: pd.DataFrame, df1h: pd.DataFrame, df4h: pd.DataFrame) -> pd.DataFrame:
    df15i = build_indicators(df15).set_index("open_time")
    df1hi = build_indicators(df1h).set_index("open_time")
    df4hi = build_indicators(df4h).set_index("open_time")

    df1hi = df1hi.sort_index()
    closes_1h = df1hi["close"]
    n = len(df1hi)
    rows = []
    for i in range(n):
        t = df1hi.index[i]
        row1h = df1hi.iloc[i]
        # asof lookup: 해당 시각까지 확정된 가장 최근 15m/4h 캔들만 사용 (미래 데이터 참조 금지)
        row15_idx = df15i.index.asof(t)
        row4h_idx = df4hi.index.asof(t)
        if pd.isna(row15_idx) or pd.isna(row4h_idx):
            continue
        row15 = df15i.loc[row15_idx]
        row4h = df4hi.loc[row4h_idx]

        long_grade, long_pct = grade_at_row("long", row15, row1h, row4h)
        short_grade, short_pct = grade_at_row("short", row15, row1h, row4h)
        factors = factor_signals_at_row(row15, row1h, row4h)

        # "이 순간 어느 방향을 선택했겠는가" - LONG/SHORT 등급 점수(%)를 비교해서
        # 더 높은 쪽을 채택. 둘 다 데이터가 없으면 방향 없음(None). 동점이면
        # 방향성이 불분명하다는 뜻으로 그대로 None 처리(억지로 한쪽을 고르지 않음).
        if long_pct is not None and short_pct is not None:
            if long_pct > short_pct:
                decision, decision_grade, decision_pct = "long", long_grade, long_pct
            elif short_pct > long_pct:
                decision, decision_grade, decision_pct = "short", short_grade, short_pct
            else:
                decision, decision_grade, decision_pct = None, None, None
        elif long_pct is not None:
            decision, decision_grade, decision_pct = "long", long_grade, long_pct
        elif short_pct is not None:
            decision, decision_grade, decision_pct = "short", short_grade, short_pct
        else:
            decision, decision_grade, decision_pct = None, None, None

        price0 = row1h["close"]
        rec = {"time": t, "price": price0, "long_grade": long_grade, "long_pct": long_pct,
               "short_grade": short_grade, "short_pct": short_pct,
               "decision": decision, "decision_grade": decision_grade, "decision_pct": decision_pct}
        for fname, fdir in factors.items():
            rec[f"factor_{fname}"] = fdir
        for label, hrs in (("1h", 1), ("4h", 4), ("24h", 24)):
            future_t = t + pd.Timedelta(hours=hrs)
            future_idx = closes_1h.index.asof(future_t)
            if pd.notna(future_idx) and future_idx > t:
                rec[f"ret_{label}_pct"] = (closes_1h.loc[future_idx] - price0) / price0 * 100
            else:
                rec[f"ret_{label}_pct"] = np.nan
        rows.append(rec)
    return pd.DataFrame(rows)


def summarize(result: pd.DataFrame) -> str:
    lines = []
    lines.append("=" * 60)
    lines.append("매 순간 LONG/SHORT 중 등급 점수가 더 높은 쪽을 '선택된 방향'으로")
    lines.append("채택했을 때의 결과 (이게 핵심 질문에 대한 답입니다:")
    lines.append("'A등급이면 롱이 됐는지 숏이 됐는지, 그리고 그게 실제로 맞았는지')")
    lines.append("=" * 60)
    decided = result.dropna(subset=["decision"])
    total = len(result)
    lines.append(f"\n전체 {total}개 시간 중 방향이 정해진 경우: {len(decided)}개 "
                 f"(무승부/데이터부족으로 미정: {total - len(decided)}개)")
    long_count = (decided["decision"] == "long").sum()
    short_count = (decided["decision"] == "short").sum()
    lines.append(f"  그중 LONG으로 선택된 경우: {long_count}개 / SHORT으로 선택된 경우: {short_count}개")

    for grade in ("A", "B", "C", "D"):
        sub = decided[decided["decision_grade"] == grade]
        if sub.empty:
            lines.append(f"\n  [{grade}등급] 표본 없음")
            continue
        n_long = (sub["decision"] == "long").sum()
        n_short = (sub["decision"] == "short").sum()
        lines.append(f"\n  [{grade}등급] 표본 {len(sub)}개 (이 중 LONG {n_long}개 · SHORT {n_short}개)")
        for label in ("1h", "4h", "24h"):
            col = f"ret_{label}_pct"
            valid = sub[[col, "decision"]].dropna()
            if valid.empty:
                continue
            # decision이 long이면 상승이 맞은 것, short이면 하락이 맞은 것
            correct = np.where(valid["decision"] == "long", valid[col] > 0, valid[col] < 0)
            # 선택된 방향 기준 수익률(숏이면 부호 반전)
            directional_ret = np.where(valid["decision"] == "long", valid[col], -valid[col])
            lines.append(f"    {label}: 평균 방향성 수익률 {directional_ret.mean():+.3f}% · "
                          f"적중률(승률) {correct.mean()*100:.1f}%")

    lines.append("\n" + "=" * 60)
    lines.append("참고: LONG/SHORT 각각 따로 놓고 본 결과 (선택 안 하고 둘 다 관찰만 했을 때)")
    lines.append("=" * 60)
    for side, grade_col, pct_col in (("LONG", "long_grade", "long_pct"), ("SHORT", "short_grade", "short_pct")):
        lines.append(f"\n=== {side} 등급별 결과 ===")
        for grade in ("A", "B", "C", "D"):
            sub = result[result[grade_col] == grade]
            if sub.empty:
                lines.append(f"  {grade}등급: 표본 없음")
                continue
            line = f"  {grade}등급 (표본 {len(sub)}개): "
            for label in ("1h", "4h", "24h"):
                col = f"ret_{label}_pct"
                valid = sub[col].dropna()
                if valid.empty:
                    continue
                avg = valid.mean()
                # LONG은 상승이 유리, SHORT는 하락이 유리
                win = (valid > 0).mean() if side == "LONG" else (valid < 0).mean()
                line += f"[{label} 평균 {avg:+.3f}% 승률 {win*100:.1f}%] "
            lines.append(line)
    return "\n".join(lines)


FACTOR_NAMES = [
    "supertrend_15m", "supertrend_1h", "supertrend_4h",
    "adx_di_15m", "adx_di_1h", "taker_flow_15m", "structure_15m", "ichimoku_1h",
]


def summarize_factors(result: pd.DataFrame) -> str:
    """등급 공식을 구성하는 8개 지표 각각을, 다른 지표와 합치지 않고 단독으로
    "up이면 상승/down이면 하락을 예측한다"고 놓았을 때의 승률을 따로 검증합니다.
    합산 점수는 예측력이 없었지만 혹시 그 중 하나라도 단독으로는 신호가 있는지
    확인하기 위함."""
    lines = ["=" * 60, "개별 지표 단독 예측력 검증 (8개 지표 각각 독립적으로)", "=" * 60]
    for fname in FACTOR_NAMES:
        col = f"factor_{fname}"
        if col not in result.columns:
            continue
        sub = result.dropna(subset=[col])
        n_up = (sub[col] == "up").sum()
        n_down = (sub[col] == "down").sum()
        lines.append(f"\n[{fname}] (up 판정 {n_up}개 · down 판정 {n_down}개)")
        for label in ("1h", "4h", "24h"):
            ret_col = f"ret_{label}_pct"
            valid = sub.dropna(subset=[ret_col])
            if valid.empty:
                continue
            correct = np.where(valid[col] == "up", valid[ret_col] > 0, valid[ret_col] < 0)
            n = len(valid)
            p = correct.mean()
            se = np.sqrt(0.5 * 0.5 / n) if n > 0 else np.nan
            lines.append(f"  {label}: 승률 {p*100:.1f}% (n={n}, 95% CI [{(p-1.96*se)*100:.1f}%, {(p+1.96*se)*100:.1f}%])")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-01-01")
    ap.add_argument("--market", choices=["spot", "futures"], default="spot")
    args = ap.parse_args()

    start_ms = int(pd.Timestamp(args.start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(args.end, tz="UTC").timestamp() * 1000)

    print(f"바이낸스 {args.market} {SYMBOL} 데이터 수집 중 ({args.start} ~ {args.end})...")
    df15 = fetch_klines("15m", start_ms, end_ms, args.market)
    df1h = fetch_klines("1h", start_ms, end_ms, args.market)
    df4h = fetch_klines("4h", start_ms, end_ms, args.market)
    print(f"  15m: {len(df15)}개, 1h: {len(df1h)}개, 4h: {len(df4h)}개 캔들 수집됨")

    print("등급 계산 + 백테스트 실행 중...")
    result = run_backtest(df15, df1h, df4h)
    result.to_csv("backtest_result.csv", index=False)

    summary = summarize(result)
    with open("backtest_summary.txt", "w", encoding="utf-8") as f:
        f.write(summary)
    print(summary)

    factor_summary = summarize_factors(result)
    with open("factor_analysis.txt", "w", encoding="utf-8") as f:
        f.write(factor_summary)
    print("\n" + factor_summary)

    print("\n완료: backtest_result.csv, backtest_summary.txt, factor_analysis.txt 생성됨")


if __name__ == "__main__":
    main()
