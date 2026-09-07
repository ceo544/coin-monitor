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

## v3.3
- v3.2 signal parser + dashboard UI merged.

## v3.4 신호 분석
- 최근 수집 데이터에서 15분 RSI / MACD Histogram / EMA20 관계를 즉시 표시합니다.
- 행을 클릭하면 해당 수집 시점의 1m / 5m / 15m / 1h Binance 지표를 펼쳐봅니다.
- `/api/signal-analysis?limit=200`은 LONG/SHORT ON 당시 지표를 묶어 평균 RSI, MACD Histogram, EMA20 상회 비율 등을 제공합니다.
- 이 분석은 E-RANG ON 당시의 보조지표 상관 패턴이며, ON 발생 원인을 확정하는 산식은 아닙니다.

## v3.5 signal parser hardening
- LONG/SHORT 판정은 라벨 셀 자체의 background/background-color만 사용합니다.
- table header, border, text color, 부모 tr/container 색상은 신호 판정에서 제외합니다.
- 기본 베이지 LONG + 기본 베이지 SHORT = OFF/OFF (WAIT)
- SHORT 라벨 빨강 = OFF/ON, LONG 라벨 활성색 = ON/OFF
- v3.4 신호 분석/보조지표 UI는 유지합니다.

## v3.5.1 오탐 수정 (BOTH 오판정 버그)
- 라벨 셀의 class 이름(`text-blue-600`, `border-green-500` 등 텍스트/보더용 유틸리티 클래스)에 색상 단어가 포함되어 있으면
  실제 배경색과 무관하게 신호가 ON으로 잘못 판정되던 버그를 수정했습니다.
- 신호 판정은 이제 라벨 셀의 `background`/`background-color` 실제 선언 값만 사용하며,
  class 문자열/텍스트 색상/보더 색상은 판정에 전혀 관여하지 않습니다. (`/api/debug-signal`의 evidence 표시용 정보에는 계속 포함됩니다.)
- 기본(베이지) Long + 기본(베이지) Short = OFF/OFF(WAIT)로 정상 판정되는지 회귀 테스트를 추가했습니다.

## v3.5.2 실서비스 오탐 수정 (조상 클래스 무시 버그)
- 실제 e-rang.kr는 `.coin-strategy__long.blue .coin-strategy__side { background:#44C27B }` 같은
  **조상(부모 tr)에 별도 토글 클래스(`blue`)가 붙어야 활성화되는 선택자**를 사용합니다.
- 기존 코드는 선택자의 맨 마지막 부분(`.coin-strategy__side`)만 라벨 셀과 비교하고 앞쪽 조상 조건(`.coin-strategy__long.blue`)은
  전혀 검사하지 않아서, 조상에 `blue`가 없어도 규칙이 항상 매치되어 Long/Short가 항상 ON으로 오판정되던 것이 BOTH 고정 버그의 실제 원인이었습니다.
- 선택자 매칭을 조상 체인까지 실제로 검사하도록 (`_selector_matches_node`) 재작성했습니다: 각 공백 구분 부분이 자기 자신 또는
  올바른 순서의 조상에 실제로 매치되어야 배경색을 인정합니다.
- 실제 사이트 마크업 구조를 재현한 회귀 테스트(`test_ancestor_toggle_class_required_for_activation`)를 추가해
  OFF/OFF, LONG만 ON, SHORT만 ON, BOTH ON 네 가지 경우를 모두 검증했습니다.
