# Coin Monitor v3

- E-RANG Long/Short 라벨의 직접 style/class + 문서 내 CSS 규칙을 함께 검사합니다.
- 주변 헤더 색상을 신호로 오인하지 않도록 라벨 중심으로 판정합니다.
- `/` 대시보드에 신호 판정 근거를 표시합니다.
- `/api/debug-signal`에서 최신 Long/Short 판정 근거와 원본 HTML 주변 조각을 확인할 수 있습니다.
- 기존 PostgreSQL 수집, Binance 보조지표, CSV, history API는 유지합니다.

## v3 Dashboard UI refresh
- E-RANG LONG/SHORT entry, TP, SL values shown in an aligned 5-stage table.
- Binance raw JSON replaced by readable 1m / 5m / 15m / 1h indicator tabs.
- EMA20/50/200, RSI14, MACD, Bollinger Bands and ATR14 are shown as a table.
- Current signal, entry-distance cards, collection/DB status and signal evidence are visible on one dashboard.
