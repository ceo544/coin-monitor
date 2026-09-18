"""
매일 새로 쌓인 데이터를 받아서, 한 파일이 GitHub의 100MB 제한에 걸리지
않도록 여러 개의 작은 조각 파일(data/coin_observations_part001.csv, ...)로
나눠서 저장합니다.

왜 필요한가: 컬럼이 1,391개나 되는 구조라 겨우 나흘치 데이터도 (심지어
parsed_json/binance_json 원본 컬럼을 뺀 슬림 모드로도) 300MB를 넘습니다 -
행 개수가 아니라 컬럼 수가 진짜 원인이라, "하루치씩 나눠서 커밋"하는 것만
으론 부족하고 파일 자체를 여러 조각으로 쪼개야 합니다.

사용법:
    python append_daily_csv.py --url <COIN_MONITOR_URL> --token <EXPORT_API_TOKEN> --data-dir data
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys

import requests

CHUNK_MAX_ROWS = 5000  # 여유를 두고 100MB 한도보다 훨씬 작게 (컬럼이 더 늘어날 걸 감안)


def read_last_id(data_dir: str) -> int:
    path = os.path.join(data_dir, "last_id.txt")
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8") as f:
        text = f.read().strip()
    return int(text) if text.isdigit() else 0


def write_last_id(data_dir: str, value: int) -> None:
    with open(os.path.join(data_dir, "last_id.txt"), "w", encoding="utf-8") as f:
        f.write(str(value))


def find_chunk_files(data_dir: str) -> list:
    """coin_observations_part001.csv, part002.csv, ... 순서대로."""
    if not os.path.isdir(data_dir):
        return []
    names = [n for n in os.listdir(data_dir) if n.startswith("coin_observations_part") and n.endswith(".csv")]
    return sorted(names)


def count_data_rows(path: str) -> int:
    """헤더 제외한 실제 데이터 행 수."""
    if not os.path.exists(path):
        return 0
    with open(path, "r", encoding="utf-8", newline="") as f:
        return max(0, sum(1 for _ in f) - 1)


PAGE_SIZE = 2000  # 한 번의 HTTP 요청으로 가져올 최대 행 수 - 서버가 응답을
                   # 만드는 동안 메모리 부담이 커지거나 요청이 너무 오래
                   # 걸려서 502가 나는 걸 막기 위해 작게 나눠서 여러 번 받음


def fetch_page(url: str, token: str, since_id: int, limit: int = PAGE_SIZE) -> tuple:
    """(header, rows) 반환 - 한 페이지(최대 limit행)만. rows는 각 행이 문자열 리스트."""
    resp = requests.get(
        f"{url.rstrip('/')}/export.csv",
        params={"token": token, "since_id": since_id, "slim": "1", "limit": limit},
        timeout=120,
    )
    resp.raise_for_status()
    reader = csv.reader(io.StringIO(resp.text))
    rows = list(reader)
    if not rows:
        return [], []
    return rows[0], rows[1:]


def append_in_chunks(data_dir: str, header: list, rows: list) -> list:
    """새 행들을 마지막 조각 파일에 이어붙이고, 꽉 차면 새 조각 파일을 만듦.
    변경/생성된 파일 경로 목록을 반환합니다."""
    os.makedirs(data_dir, exist_ok=True)
    changed = set()
    existing = find_chunk_files(data_dir)
    if existing:
        current_name = existing[-1]
        current_idx = int(current_name.replace("coin_observations_part", "").replace(".csv", ""))
    else:
        current_name = None
        current_idx = 0

    current_path = os.path.join(data_dir, current_name) if current_name else None
    current_count = count_data_rows(current_path) if current_path else CHUNK_MAX_ROWS  # 없으면 바로 새 파일 시작

    i = 0
    while i < len(rows):
        if current_path is None or current_count >= CHUNK_MAX_ROWS:
            current_idx += 1
            current_name = f"coin_observations_part{current_idx:03d}.csv"
            current_path = os.path.join(data_dir, current_name)
            with open(current_path, "w", encoding="utf-8", newline="") as f:
                csv.writer(f).writerow(header)
            current_count = 0

        space_left = CHUNK_MAX_ROWS - current_count
        batch = rows[i:i + space_left]
        with open(current_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            for row in batch:
                writer.writerow(row)
        changed.add(current_path)
        current_count += len(batch)
        i += len(batch)

    return sorted(changed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--data-dir", default="data")
    args = ap.parse_args()

    cursor = read_last_id(args.data_dir)
    print(f"마지막으로 받은 id: {cursor}")

    total_rows = 0
    all_changed: set = set()
    page_num = 0
    while True:
        page_num += 1
        header, rows = fetch_page(args.url, args.token, cursor)
        if not rows:
            break
        print(f"페이지 {page_num}: {len(rows)}행 받음 (since_id={cursor})")

        changed_files = append_in_chunks(args.data_dir, header, rows)
        all_changed.update(changed_files)
        total_rows += len(rows)

        cursor = int(rows[-1][0])
        write_last_id(args.data_dir, cursor)  # 페이지마다 즉시 갱신 - 중간에
        # 실패해도 이미 받은 페이지만큼은 다시 안 받아도 됨

        if len(rows) < PAGE_SIZE:
            break  # 서버가 이보다 적게 줬다는 건 다 받았다는 뜻

    if total_rows == 0:
        print("새로 쌓인 데이터 없음 - 스킵")
        return 0

    print(f"총 새로 받은 행 수: {total_rows}")
    for path in sorted(all_changed):
        print(f"업데이트됨: {path} ({count_data_rows(path)}행)")
    print(f"last_id.txt 최종 갱신: {cursor}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
