# Coin Monitor for Railway

Public page observer for `https://e-rang.kr/api/coin.php`. It requests the page once per minute until you stop the Railway service and persists every observation in Railway PostgreSQL.

## Railway deploy
1. Create a new Railway project and deploy this folder/repo.
2. Add a PostgreSQL service to the same project.
3. In the app service Variables, make sure `DATABASE_URL` is available from PostgreSQL (Railway reference variable if not injected automatically).
4. Optional variables: `TARGET_URL=https://e-rang.kr/api/coin.php`, `INTERVAL_SECONDS=60`.
5. Deploy and generate a public domain if you want to use `/status` and `/export.csv`.

## Endpoints
- `/status` collection count and latest observation
- `/export.csv` export parsed observations

Raw HTML and parsed page text are retained in PostgreSQL for later re-parsing. Collection errors are also stored instead of terminating the process.

Note: Keep request frequency reasonable and comply with the site's terms/robots/rate limits. Default/minimum interval in this build is 60 seconds.

## LONG / SHORT 진입 신호 수집
매 관측마다 Long/Short 라벨의 HTML/CSS class/style을 함께 분석합니다. Long이 파랑/초록 계열로 표시되면 `long_signal=true`, Short가 빨강 계열로 표시되면 `short_signal=true`로 저장합니다. 감지한 색상 힌트와 원본 시각 증거(`visual_evidence`, `label_html`), 페이지의 `진입해도 좋다/진입 가능/진입 추천` 계열 문구 여부도 `parsed_json`과 CSV에 보존합니다. 원본 HTML 자체도 저장되므로 이후 실제 사이트의 색상 규칙이 확인되면 과거 데이터도 재분석할 수 있습니다.
