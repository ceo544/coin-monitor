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

## v3.13 텔레그램 알림
- LONG 또는 SHORT 신호가 **OFF → ON으로 전환되는 순간**에만 텔레그램 메시지를 보냅니다 (30초마다 계속 ON이어도 스팸 발송하지 않음).
- 메시지에는 판정된 방향(LONG/SHORT), 현재가(Binance 실시간가), 진입 1~5 가격과 TP/SL, 감지된 배경색, KST 시각이 포함됩니다.
- 배포 직후(재시작 직후) 첫 수집 사이클에서는 알림을 보내지 않습니다 — 재시작 전부터 이미 ON이었던 상태를 "새로 발생"으로 잘못 알리는 것을 막기 위함입니다. DB에 저장된 마지막 관측치를 기준으로 이전 상태를 복원합니다.
- 필요한 환경변수:
  - `TELEGRAM_BOT_TOKEN` — BotFather에서 발급받은 봇 토큰
  - `TELEGRAM_CHAT_ID` — 메시지를 받을 채팅방/사용자 ID
  - (선택) `TELEGRAM_NOTIFY_OFF=false` — 기본값은 `true`라서 ON→OFF로 꺼질 때도 1회 알림이 갑니다. ON 될 때만 받고 싶으면 `false`로 설정하세요.
  - (선택) `DASHBOARD_URL` — 메시지 마지막 줄에 대시보드 링크를 붙이고 싶으면 설정
- `POST /api/telegram-test` — 신호 발생을 기다리지 않고 텔레그램 설정이 정상인지 바로 확인할 수 있는 테스트 발송 엔드포인트
- `/api/status`의 `telegram` 필드에서 활성화 여부, 마지막 발송 시각/오류, 누적 발송 수를 확인할 수 있습니다.

## v3.16 텔레그램 메시지에 근거 포함
- 텔레그램 메시지에 진입가 표 아래로 두 가지 근거 섹션이 추가됩니다:
  - **📌 E-RANG 실제 판정 근거** — 감지된 배경색, 매치된 CSS 규칙, 매치된 라벨 텍스트 (진짜 ON/OFF 판정 근거)
  - **📊 Binance 지표 참고** — 15분/1시간봉 기준 RSI, MACD 히스토그램, EMA20/50 관계, 볼린저밴드 위치를 문장으로 정리한 참고용 관측 (대시보드의 "선택한 수집 시점 보조지표" 패널과 동일한 로직). 실제 ON/OFF 원인이 아니라 그 시점에 같이 관찰된 지표 상태임을 명시합니다.

## v3.17 텔레그램 메시지 버그 수정 + 테스트 미리보기 개선
- **실제 버그 수정**: EMA20이 EMA50보다 낮을 때 문구에 `<` 부등호 문자가 그대로 들어가 있었는데, 텔레그램 HTML 모드가 이를 깨진 태그로 해석해서 그 아래 내용(근거 섹션 포함)이 잘리거나 메시지 전체 전송이 실패할 수 있었습니다. `<` → "미만"으로 교체했습니다.
- 감지 배경색/CSS 규칙/라벨 텍스트/지표 문구 등 메시지에 들어가는 모든 동적 텍스트를 HTML 이스케이프 처리해서, 향후 어떤 값이 들어와도 메시지가 깨지지 않도록 했습니다.
- `POST /api/telegram-test`가 이제 최근 관측치에 LONG 또는 SHORT가 켜져 있으면, 실제 알림과 완전히 동일한 형식(진입가 표 + E-RANG 실제 판정 근거 + Binance 지표 참고)으로 미리보기를 보내줍니다. 지금 신호가 없으면 그 사실을 알리는 안내 메시지를 보냅니다.
- 재배포 직후처럼 Binance 실시간 스냅샷이 아직 준비 안 된 순간에는 자동으로 즉시 한 번 동기 호출로 대체해서, 그 사이클의 관측/알림에 지표가 빠지지 않도록 보강했습니다.

## v3.18 ON/OFF 둘 다 근거 포함
- 이전에는 OFF(해제) 메시지에 근거 섹션이 통째로 빠졌습니다 — OFF는 정의상 "활성 배경색이 없는 상태"라서, ON 근거를 만드는 로직(`_sig_evidence_text`)이 빈 문자열을 반환했기 때문입니다.
- 이제 ON과 OFF 각각 다른 형태의 "E-RANG 실제 판정 근거"를 보여줍니다:
  - **ON**: 지금 감지된 배경색 + 매치된 CSS 규칙 + 라벨 텍스트
  - **OFF**: 직전에 감지됐던 배경색이 무엇이었는지 + 기본색으로 복귀했다는 사실 + **몇 초/몇 분간 켜져 있다가 꺼졌는지(활성 유지 시간)**
- Binance 지표 참고 섹션도 ON/OFF 각각 "진입 시점" / "해제 시점" 라벨을 붙여서 두 메시지 모두에 포함됩니다.

## v3.21 로그인 화면 추가 + 텔레그램 메시지 간소화
- **로그인 화면**: 대시보드(`/`), 모든 `/api/*`, `/export.csv`가 이제 로그인해야 접근 가능합니다. `/healthz`만 배포 플랫폼 헬스체크를 위해 예외로 남겨뒀습니다.
  - 기본 계정: `admin` / `1Q2w3e4r5t!!` — 환경변수 `ADMIN_USERNAME`, `ADMIN_PASSWORD`로 덮어쓸 수 있습니다 (소스코드에 비밀번호를 그대로 두는 게 걱정되면 반드시 Railway 환경변수로 바꿔서 쓰세요).
  - `FLASK_SECRET_KEY` 환경변수를 꼭 고정값으로 설정하세요. 설정 안 하면 재배포/재시작마다 랜덤 키가 새로 생성돼서 기존 로그인 세션이 전부 풀리고 다시 로그인해야 합니다.
  - 로그인 세션은 30일 유지, `/logout`으로 로그아웃 가능 (대시보드 상단 버튼에도 추가됨).
- **텔레그램 메시지 간소화**:
  - **신호 해제(OFF)** 메시지는 이제 `E-RANG {LONG/SHORT} 신호해제` 한 줄 + 시각만 옵니다. 진입가 표, 판정 근거, Binance 지표, AI 의견 등 아래 내용은 전부 제거했습니다.
  - **AI 의견(1차 진입 TP 적정성)** 섹션은 ON/OFF 메시지 모두에서 제거했습니다. (내부 로직/테스트는 남겨뒀으니 나중에 다시 켜고 싶으면 말씀해주세요.)
  - **신호 발생(ON)** 메시지는 그대로 유지 (진입가 표 + 판정 근거 + Binance 지표), 제목만 "진입 신호 발생" → "신호발생"으로 조금 더 간결하게 바꿨습니다.

## v3.20 AI 의견 (1차 진입 TP 적정성) 추가
- 텔레그램 메시지 맨 아래에 **🤖 AI 의견** 섹션이 추가됩니다. LONG/SHORT 각각 1차 진입가 기준 TP가 적정한지를 아래 규칙으로 자동 판정합니다 (외부 LLM 호출 없이 서버에서 즉시 계산, 별도 API 키 불필요):
  - **손익비 (TP 거리 ÷ SL 거리)**: 1 미만이면 "낮은 편(SL에 먼저 닿을 위험)", 1~3이면 "무난한 편", 3 초과면 "매우 높은 편(도달 확률 낮을 수 있음)".
  - **15분봉 ATR 대비 TP 거리**: 0.5배 미만이면 "타이트함(빠르게 도달 가능)", 2배 초과면 "먼 편(추세 지속 필요)".
  - **모멘텀 경고**: LONG인데 15분 RSI 70 이상(과매수권), SHORT인데 RSI 30 이하(과매도권)면 되돌림/반등 리스크를 경고. 펀딩비가 한쪽으로 과열(±0.05% 이상)되어 있으면 스퀴즈/커버링 리스크를 경고.
  - LONG/SHORT 진입가 표와 마찬가지로 신호를 촉발한 방향뿐 아니라 **양쪽 다** 항상 표시됩니다.
  - 이 의견은 규칙 기반 자동분석이며, 실제 시장 결과를 보장하지 않는 참고용 정보입니다 (투자 조언 아님).

## v3.19 롱/숏 진입가 동시 표시 + 알림 누락 방지 강화
- 메시지 본문에 트리거된 쪽 한 방향의 진입가만 있던 것을, **LONG/SHORT 진입가·TP·SL을 항상 둘 다** 보여주도록 바꿨습니다 — `[LONG 진입가]` / `[SHORT 진입가]` 두 블록이 매번 같이 옵니다.
- LONG 메시지 작성/전송과 SHORT 메시지 작성/전송을 서로 독립적인 예외 처리로 분리했습니다. 한쪽 메시지를 만들다 예상 못한 오류가 나도 반대쪽 알림은 영향 없이 정상 전송되고, 실패 사유는 로그에 `LONG {ON|OFF} message` / `SHORT {ON|OFF} message` 로 구분되어 남습니다.
- **중요한 구조적 한계**: 신호 감지는 e-rang.kr을 30초(`INTERVAL_SECONDS`)마다 확인하는 방식이라, **두 번의 확인 사이(30초 이내)에 ON→OFF→ON처럼 아주 짧게 깜빡이면 그 사이 상태 변화 자체를 관측할 기회가 없어** 알림이 못 갈 수 있습니다. 이건 코드 버그가 아니라 폴링 주기의 근본적인 한계라서, 이런 짧은 깜빡임까지 다 잡고 싶으면 `INTERVAL_SECONDS`를 더 짧게 낮추는 것 외에는 방법이 없습니다 (다만 너무 짧으면 e-rang.kr 차단 위험이 커집니다).
