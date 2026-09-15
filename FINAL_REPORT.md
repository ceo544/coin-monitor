# Coin Monitor — FINAL 데이터 수집 버전 보고서

이 문서는 "FINAL 데이터 수집 버전" 작업 지시서(36개 섹션)에 따라 무엇을 했는지 정리한 보고서입니다.

---

## 0. 가장 먼저 확인해야 할 것 — PostgreSQL 관련

지시서 1번 섹션은 "PostgreSQL 저장"을 유지하라고 되어 있었지만, **이 프로젝트는 이미 여러 턴 전에 사용자님의 명시적 요청으로 SQLite로 전환**되었고, 그 위에 다중회원/자동매매/설정 시스템 전체가 구축되어 있습니다. PostgreSQL로 되돌리면 이 모든 게 깨지므로, **SQLite를 유지**한 채로 이번 작업을 진행했습니다. 기존 기능을 절대 깨지 않는다는 최우선 원칙에 따른 판단입니다.

---

## 1. 기존 기능 — 전부 유지 확인됨

아래 목록을 코드에서 직접 확인했고, 이번 작업으로 **컬럼 삭제/이름변경/타입변경은 0건**입니다. 전부 `ALTER TABLE ADD COLUMN` 방식으로만 확장했습니다.

- E-RANG 원본 페이지 수집 (`fetch_target`, 로그인 세션 유지 포함) — 유지
- LONG/SHORT ON/OFF 색상·CSS 기반 판별 (`parser.py`) — 유지, 변경 없음
- `current_price`, `current_price_raw` — 유지
- `raw_text`, `raw_html` 원본 보존 — 유지
- `/export.csv`, `/api/signals`, `/api/history`, `/api/debug-signal` 등 기존 API — 전부 유지, 신규 컬럼만 뒤에 추가됨
- 기존 웹 UI, 로그인/회원가입, 다중회원 설정 — 유지
- Railway 배포 방식, `Procfile`, `railway.json` — 유지
- 기존 `observations`, `users`, `user_settings`, `auto_trades` 테이블/컬럼 — 전부 그대로, 컬럼 추가만 발생

---

## 2. 이번에 새로 추가한 것

### 2-1. DB 스키마 (전부 `ALTER TABLE ADD COLUMN`, 기존 데이터 무손실)

| 컬럼 | 용도 |
|---|---|
| `observed_at_kst` | observed_at의 KST 버전 (지시서 2번) |
| `long_start` / `long_end` | LONG OFF→ON / ON→OFF 시점 (ON 유지 중엔 매 행마다 시작시각이 carry-forward됨) |
| `short_start` / `short_end` | SHORT 동일 |

인덱스 `idx_observations_long_start`, `idx_observations_short_start` 신규 추가.

**재시작 복구**: 서버 재시작 시 마지막 행을 보고 진행 중이던 스트릭(streak)을 복구합니다 (`_init_signal_streak_state`) — 재시작 때문에 "언제부터 켜져있었는지" 정보가 끊기지 않습니다.

**중복 방지**: `observed_at`이 완전히 동일한 행이 이미 있으면 새로 넣지 않고 기존 id를 반환합니다 (하드 UNIQUE 제약은 기존 테이블에 이미 중복이 있으면 마이그레이션 자체가 실패할 위험이 있어 애플리케이션 레벨 체크로 구현).

### 2-2. 지표 (indicators.py) — 순수 계산 함수, 전부 개별 테스트됨

**추세**
- EMA 9/20/21/50/100/200 (기존 20/50/200에 9/21/100 추가)
- `close_vs_emaX_pct` = `(close - EMA) / close * 100` (5개 EMA 전부)
- `ema9_ema21_pct`, `ema20_ema50_pct`, `ema50_ema200_pct` = EMA쌍 간 % 거리
- EMA 정배열/역배열(`ema_alignment`) + 골든/데드크로스(`ema_fast_cross`)
- DEMA(20) = `2*EMA - EMA(EMA)`
- HMA(20) = `WMA(2*WMA(n/2) - WMA(n), sqrt(n))`
- Supertrend (ATR 기반, 표준 final-band 알고리즘으로 전체 시리즈 순회 구현 — 단순 근사 아님)

**모멘텀**
- RSI 7/14/21
- ROC(12) = `(close - close[n]) / close[n] * 100`
- MACD + `macd_cross`(골든/데드크로스)
- Stochastic %K/%D + `stoch_cross`
- Stochastic RSI (RSI 시리즈에 스토캐스틱 공식 재적용)

**추세강도**
- ADX/+DI/-DI (Wilder 방식) + `di_cross` + `di_diff`(+DI - -DI)

**CCI**
- CCI 14 & 20 (Lambert 표준식: `(TP - SMA) / (0.015 * mean_deviation)`)
- zero-cross, +100-cross, -100-cross

**변동성**
- ATR 7 & 14, ATR% = `ATR / close * 100`
- Bollinger(20): upper/middle/lower + width + width% + position(0~1, band 밖이면 범위 벗어날 수 있음)
- Keltner Channel(20, EMA+ATR 기반) + BB-Keltner 스퀴즈 판정(`bb_keltner_squeeze`)

**VWAP**
- ⚠️ **계산 방식 명시**: 세션(하루) 리셋형이 아니라, **수집된 캔들 구간(최대 220개) 기준 롤링 VWAP**입니다. 무기한 영구 선물이라 정해진 세션 경계가 없어서 이렇게 설계했고, 코드 주석에도 명시했습니다.
- `vwap_diff` = close - VWAP, `vwap_diff_atr` = diff/ATR, `vwap_slope` = 1캔들 전 대비 변화

**거래량**
- `volume_ma_5/20/50`, `volume_ratio`, `volume_delta`, `volume_slope`, `volume_spike`(20MA 대비 2배 이상)

**체결(Taker)**
- `taker_buy_volume/sell_volume/ratio` — 바이낸스 kline 응답에 이미 포함된 필드(index 9)를 추출한 것이라 **API 호출 추가 없음**

**CVD**
- 수집 캔들 구간 내 누적 (taker_buy - taker_sell) — ⚠️ **재시작하면 초기화되는 구간 기준값**, 전역 누적 CVD 아님 (코드 주석 명시)
- `cvd_divergence`: 가격 상승+CVD 하락(or 반대) 감지

**시장구조**
- 스윙 고점/저점(피벗) 기반 HH/HL/LH/LL 판정
- **BOS**(추세 방향으로 돌파, 지속 확인) / **CHOCH**(추세 반대로 돌파, 반전 경고)
- `pivot_levels`: 최근 스윙 고점/저점 실제값 + 현재가와의 거리%
- `recent_high_low`: 단순 롤링 최고/최저(20기간) + 거리%

**일목균형표**
- 전환선/기준선/선행스팬A·B/후행스팬 + 구름 위/아래/안 판정 + 구름두께 + 구름까지 거리%

**가격 수익률**
- `price_change_pct`, `return_1/3/5/15`

**정규화 (지시서 28번)**
- ATR%, VWAP거리%, BB position, EMA거리%, 피벗거리% — 전부 절대가격이 아니라 %/비율 기반이라 BTC가 어느 가격대에 있든(2025년 데이터에도) 그대로 적용 가능

### 2-3. "변화량/기울기" — 두 가지 방식으로 구현

**A) 캔들 개수 기준** (`_with_history`): RSI14, MACD히스토그램, ATR%, CCI20, Stochastic %K, ADX — 현재/1캔들전/5캔들전/delta/slope

**B) 실제 시각(분) 기준** (`_build_minute_history`, 이번 지시서에서 요청한 "1/3/5/15분 변화량"): 캔들 개수 방식은 시간대마다 의미가 달라지는 문제(1h봉 1캔들 전 = 1시간 전, 1m봉 1캔들 전 = 1분 전)가 있어서, **우리 자체 DB에 쌓인 과거 관측치를 실제 시각으로 거슬러 조회**하는 방식으로 별도 구현했습니다.
- 대상: RSI14, MACD(원본), MACD히스토그램, CCI20, Stochastic%K, ADX, +DI, -DI, ATR%, VWAP거리%, 거래량, CVD, 체결매수비율, OI, Long/Short비율, 호가불균형
- 시간대별(1m/5m/15m/1h/4h) × 위 필드 각각 → 현재값/1분전/3분전/5분전/15분전/각 delta

### 2-4. 바이낸스 추가 API 연동 (binance_data.py)

| 함수 | 엔드포인트 | 용도 |
|---|---|---|
| `fetch_top_trader_ratios` | `/futures/data/topLongShortAccountRatio`, `/futures/data/topLongShortPositionRatio` | 탑트레이더 계좌기준/포지션기준 롱숏비율 |
| `fetch_order_book_imbalance` (확장) | `/fapi/v1/depth` (기존 호출 재사용) | 5/10/20단계 불균형을 **API 호출 추가 없이** 같은 응답에서 슬라이싱 |
| 4h 타임프레임 | `/fapi/v1/klines` (기존 반복문에 추가) | 기존 1m/5m/15m/1h에 4h 추가 |

### 2-5. 실시간 청산 데이터 (liquidation_stream.py, 지난 턴에 구축 완료)

바이낸스 선물 청산 스트림(`wss://fstream.binance.com/ws/!forceOrder@arr`)에 상시 연결하는 백그라운드 스레드. REST로는 시장 전체 청산량을 가져올 방법이 없어서(개인 청산 내역만 REST로 조회 가능) 웹소켓이 유일한 방법입니다. 자동 재연결 포함. 1/5/15분 구간 롱청산/숏청산 건수·수량·명목가치 + **청산 불균형**(`liq_imbalance` = (롱청산-숏청산)/(롱청산+숏청산)) 집계.

### 2-6. 파생 계산 (두 데이터 소스를 조합)

- `oi_price_classification`: 가격변화%(5분봉) + OI변화%(바이낸스 자체 이력 API) 조합 → `price_up_oi_up` 등 4분류
- `funding_extra`: 현재 펀딩비 vs 직전 정산값 변화량 + 극단치(기본 임계값 0.05%) 플래그

### 2-7. 시간 정보 (binance_data.py `time_context`)

`hour_kst`, `hour_utc`, `weekday`, `weekday_name`, `is_weekend`, `sessions`(asia/europe/us, 겹칠 수 있음), `session_overlap`

⚠️ 세션 경계(UTC 기준: Asia 00-09, Europe 07-16, US 12-21)는 업계에서 흔히 쓰는 근사치이며, 거래소 공식 세션 정의가 있는 건 아닙니다.

### 2-8. 신호 컨텍스트 (지시서 29번)

- `minutes_since_long_start` / `minutes_since_short_start`: 매 행마다 "현재 ON 스트릭이 몇 분째인지" (long_start/short_start 컬럼 기반 계산, `/export.csv`에서 매번 계산되어 나감)
- `/api/events`: 모든 LONG/SHORT 시작/종료 이벤트를, **그 순간의 전체 지표 스냅샷과 함께** 제공하는 신규 API
- `/export-signals.csv`: 신호 시작 시점만 모아서 그때의 모든 지표값을 CSV로 (1행 = 1개 신호 시작 이벤트)

### 2-9. 데이터 품질 (지시서 30번)

- `data_quality_score` (0~100): 이 행에서 바이낸스 데이터 그룹(ticker/premium_index/order_book/funding_history/long_short_ratio/top_trader_ratio/oi_change/indicators/time_context/minute_history/liquidations, 총 12개) 중 실제로 채워진 비율
- `missing_fields_count`: 비어있는 그룹 수 + 개별 API 에러 수
- 각 바이낸스 API 호출은 **독립적으로 try/except** 처리되어 있어서 하나 실패해도 나머지는 정상 저장됨 (기존부터 이렇게 설계되어 있었고, 이번에 추가한 것들도 전부 동일 패턴 유지)

### 2-10. Export (지시서 32번)

- `/export.csv` — 유지, 컬럼 **총 1,391개**로 확장 (5개 시간대 × 지표 세트 + 변화량 + 시장데이터 + 청산 등)
- `/api/signals` — 유지, 변경 없음
- `/api/events` — 신규
- `/export-signals.csv` — 신규

### 2-11. DB 인덱스 (지시서 33번)

`idx_observations_long_start`, `idx_observations_short_start` 신규 추가 (기존 `idx_observations_observed_at`, `idx_observations_signal`은 유지).

---

## 3. 계산식 정리 (핵심만)

```
RSI = 100 - 100/(1+RS), RS = 평균상승폭/평균하락폭 (Wilder 평활)
ATR = Wilder 평활된 True Range
ATR% = ATR / close * 100
MACD = EMA12 - EMA26, Signal = EMA9(MACD), Histogram = MACD - Signal
Stochastic %K = (close - lowest_low_N) / (highest_high_N - lowest_low_N) * 100
StochRSI = 위 %K 공식을 RSI 시리즈에 재적용
ADX/+DI/-DI = Wilder 표준 공식 (+DM/-DM/TR 평활 → DX → ADX)
CCI = (TP - SMA(TP)) / (0.015 * mean_deviation), TP = (H+L+C)/3
Bollinger = SMA(20) ± 2*표준편차
Keltner = EMA(20) ± 2*ATR(20)
VWAP = Σ(TP*Volume) / ΣVolume  (※ 수집된 캔들 구간 기준 롤링, 세션 리셋 아님)
Supertrend = 표준 final-band 알고리즘 (band 값을 이전 값과 비교하며 순차 갱신)
CVD = Σ(taker_buy_volume - taker_sell_volume)  (※ 수집 구간 기준, 전역 누적 아님)
Ichimoku: 전환선=(9기간 H+L)/2, 기준선=(26기간 H+L)/2, 선행스팬A=(전환+기준)/2, 선행스팬B=(52기간 H+L)/2
```

---

## 4. API Rate Limit 위험도

한 사이클(기본 30초)당 바이낸스 REST 호출 수:
- 기존: ticker_24h, premium_index, open_interest, order_book, funding_history, long_short_ratio, oi_change, klines×4 = **11회**
- 이번 추가분: top_trader_ratio×2, klines(4h) = **+3회**
- **사이클당 총 14회**, 30초 간격이면 분당 28회 수준

바이낸스 선물 공개 데이터 API의 weight 한도는 분당 2,400(IP 기준)으로 매우 여유 있어서, **이 정도 호출량은 rate limit 위험이 사실상 없습니다.** 다만 수집 주기를 사용자가 설정에서 더 짧게(예: 5초) 바꾸면 위험해질 수 있으니, 극단적으로 짧은 주기는 권장하지 않습니다.

청산 웹소켓은 REST 호출이 아니라 상시 연결이라 rate limit과 무관합니다.

---

## 5. NULL 처리된 것 (수집 불가능하거나 설계상 보류)

| 항목 | 이유 |
|---|---|
| 청산량 실시간 라이브 검증 | 이 환경에서 바이낸스 접속 자체가 막혀있어 실제 연결은 확인 못 함. 로직(메시지 파싱/집계)은 모의 데이터로 검증 완료 |
| VWAP raw 호가창 전체 보존 | DB 용량 문제로 집계값(불변량/스프레드 등)만 저장, 호가 레벨 전체 raw는 미보존 (지시서 31번에서도 "필요하면 별도 설계"라고 명시된 부분) |
| RSI7/RSI21에 대한 개별 변화량 추적 | RSI14만 변화량 추적 대상에 포함 (모든 RSI 기간에 대해 전부 추적하면 컬럼이 과도하게 늘어나 우선순위상 14만 선택) |
| Top Trader 비율의 변화량(minute_history) | 현재값만 저장, 1/3/5/15분 변화량은 미적용 (Long/Short Ratio 전역값만 변화량 추적 대상에 포함) |

이 표에 없는 나머지 항목들은 전부 구현되어 있으며, API 실패 시에도 (가짜 값이 아니라) None으로 저장됩니다.

---

## 6. 테스트 결과

```
203 passed (전체 테스트 스위트)
```

- indicators.py: 신규 지표 함수 전부 개별 유닛테스트 (상승추세/하락추세/횡보/데이터부족 시나리오)
- liquidation_stream.py: 메시지 파싱, 윈도우별 집계, 불균형 계산, pruning 로직
- 실제 파이프라인 E2E 테스트 (이번 세션에서 직접 실행 확인):
  - `long_start`/`long_end` 실제 OFF→ON→OFF 전환 재현 → 정확한 시점 기록 확인
  - 중복 `observed_at` 삽입 시도 → 정상적으로 스킵되고 기존 id 반환 확인
  - `/api/events` → 시작/종료 이벤트 정확히 반환 확인
  - `/export-signals.csv` → 신호 시작 시점 행 + 진입가 정확히 반영 확인
  - `/export.csv` → 1,391개 컬럼 전부 정상 매핑, `minutes_since_long_start`/`data_quality_score` 정확한 값 확인
  - 버그 1건 발견 및 수정: `funding_extra`가 히스토리 배열의 잘못된 인덱스(직전전 값)를 참조하던 오프바이원 오류 → 테스트로 잡아내서 수정

---

## 7. 절대 금지사항 준수 확인

- **Look-ahead bias**: 모든 지표는 수집 시점(now)까지의 klines만 사용. 미래 데이터를 쓰는 곳 없음. (2025년 과거 데이터 백테스트에 적용할 때도 동일 원칙 유지 필요 — 그건 별도 백테스트 스크립트 작성 시점의 책임입니다)
- **E-RANG 신호를 Binance 지표로 임의 생성**: 절대 하지 않았습니다. `long_signal`/`short_signal`은 여전히 100% E-RANG 원본 CSS/색상 판정 결과이고, 이번에 추가한 모든 Binance 지표는 **오직 "왜 신호가 떴는지 나중에 역추적하기 위한 설명변수"로만 저장**됩니다. 어떤 지표 임계값으로 LONG/SHORT를 만들어내는 로직은 코드 어디에도 없습니다.
- **기존 데이터 삭제**: 0건. 전부 ADD COLUMN.
- **API 실패 시 임의값 생성**: 없음. 전부 None/에러 딕셔너리.
