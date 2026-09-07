# Coin Monitor v2 for Railway

`https://e-rang.kr/api/coin.php` 공개 페이지를 1분마다 수집하고, 원본 HTML / 파싱값 / LONG·SHORT 색상 신호 / Binance 보조 지표를 Railway PostgreSQL에 저장하는 모니터링 앱입니다.

## 핵심 변경점

- `gunicorn daemon thread` 구조 제거
- Railway 시작 명령을 `python main.py`로 변경
- 웹서버 시작과 동시에 수집기 스레드가 확실히 시작됨
- 수집 단계별 로그 출력
- `/` 대시보드 UI 제공
- `/api/status`, `/api/history`, `/api/signals`, `/api/collect-now`, `/export.csv` 제공
- 실패해도 프로세스 종료 없이 오류 행 저장 후 다음 1분에 재시도

## Railway 배포 순서

1. 이 ZIP 압축을 풉니다.
2. GitHub `coin-monitor` 저장소에 기존 파일을 교체 업로드합니다.
   - ZIP 파일 자체가 아니라 압축 안의 파일들을 올립니다.
   - 이전에 올라간 `__pycache__` 폴더는 삭제해도 됩니다.
3. Railway가 GitHub 변경을 감지해 자동 재배포합니다.
4. `coin-monitor` 서비스 Variables에 아래가 있는지 확인합니다.
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
5. 배포 후 `Deploy Logs`에서 아래 로그가 보이면 정상입니다.

```text
[BOOT] starting Coin Monitor v2
[DB] schema ready
[BOOT] collector started interval=60s
[FETCH] GET https://e-rang.kr/api/coin.php
[HTTP] 200 OK
[PARSE] BTC=...
[SIGNAL] LONG=... SHORT=...
[DB] saved observation id=...
[NEXT] sleeping 60.0s
```

## 접속 주소 만들기

Railway 서비스 Settings 또는 Networking에서 `Generate Domain`을 누르면 브라우저로 접속할 수 있는 주소가 생깁니다.

- `/` : 관리자 대시보드
- `/api/status` : 상태 JSON
- `/api/history?limit=80` : 최근 수집 데이터
- `/api/signals` : LONG/SHORT 신호 발생 데이터
- `/export.csv` : 전체 CSV 다운로드

## 환경변수

| 변수 | 기본값 | 설명 |
|---|---:|---|
| `DATABASE_URL` | 필수 | Railway Postgres 연결 문자열 |
| `TARGET_URL` | `https://e-rang.kr/api/coin.php` | 수집 대상 |
| `INTERVAL_SECONDS` | `60` | 수집 주기. 이 버전은 최소 60초로 고정 보호 |
| `ENABLE_BINANCE` | `true` | Binance 보조 데이터 수집 여부 |
| `REQUEST_TIMEOUT` | `20` | coin.php 요청 제한시간 |
| `MAX_HTML_BYTES` | `2000000` | 원본 HTML 저장 최대 크기 |

## 주의

공개 페이지에 정상 접근해 표시값을 기록하는 용도입니다. 서버 침입, 인증 우회, 비공개 소스 조회, 취약점 탐색 기능은 없습니다.
