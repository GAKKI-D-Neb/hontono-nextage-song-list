#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ニコニコ動画の公開マイリストから動画情報を取得し、CSVに出力するスクリプト。

必要パッケージ:
    pip install requests

Googleスプレッドシートへ直接書き込む場合:
    pip install gspread google-auth
"""

import csv
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests


# ============================================================
# 設定
# ============================================================

MYLIST_URL = "https://www.nicovideo.jp/mylist/79699409"
OUTPUT_CSV = "data/hontono_selection_cd_list_2026.csv"

PAGE_SIZE = 100
REQUEST_INTERVAL_SECONDS = 1.0
REQUEST_TIMEOUT_SECONDS = 30

# Googleスプレッドシートへ直接書き込む場合に設定する。
# 空欄ならCSVだけを出力する。
SPREADSHEET_ID = ""
WORKSHEET_NAME = "マイリスト取得"
SERVICE_ACCOUNT_FILE = "service_account.json"

# タイトルから自動抽出できない場合は、動画タイトルをキーにして
# 正しい曲名を指定できます。必要になったものだけ追加してください。
# 例:
# SONG_TITLE_OVERRIDES = {
#     "動画タイトルをそのまま記載": "正しい曲名",
# }
SONG_TITLE_OVERRIDES = {
    "STARMAN -イダトーア- / HARUKA, HAYATO (VOCALOID6)": "STARMAN",
}


API_URL = "https://nvapi.nicovideo.jp/v2/mylists/{mylist_id}"
API_HEADERS = {
    "X-Frontend-ID": "6",
    "X-Frontend-Version": "0",
    "X-Niconico-Language": "ja-jp",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nicovideo.jp/",
}

COLUMNS = [
    "順番",
    "曲名",
    "動画タイトル",
    "投稿者名",
    "作者名",
    "動画ID",
    "ニコニコ動画URL",
    "サムネイルURL",
]


def extract_song_title(video_title: str) -> str:
    """
    動画タイトルから曲名らしい部分を抽出する。

    対応する主な形式:
      曲名 / ボーカル名
      曲名 feat. ボーカル名
      曲名_投稿者feat.ボーカル名
      投稿者名『曲名』feat.ボーカル名
      曲名 - ボーカル名

    動画タイトル自体は別列に残すため、抽出結果を後から確認・修正できる。
    """
    title = unicodedata.normalize("NFC", str(video_title or ""))
    title = title.replace("\u00a0", " ").strip()
    title = re.sub(r"\s+", " ", title)

    # 個別の例外は、設定した曲名を優先する
    if title in SONG_TITLE_OVERRIDES:
        return SONG_TITLE_OVERRIDES[title]

    # 『曲名』のように曲名がかぎ括弧で囲まれている形式
    quoted = re.search(r"『([^』]+)』", title)
    if quoted:
        title = quoted.group(1)

    # / または ／ より前を曲名候補とする
    parts = re.split(r"\s*[／/]\s*", title, maxsplit=1)
    title_part = parts[0]

    # 「曲名_投稿者feat.歌声」のように、スラッシュを使わない形式
    if len(parts) == 1:
        title_part = re.split(
            r"_(?=[^_]*?(?:feat\.?|ft\.?))",
            title_part,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0]

    # feat. / ft. 以降は歌唱キャラクター等の情報として除去
    title_part = re.split(
        r"(?i)\s*feat(?:\.|uring)?|feat(?=[^\s])|\s*ft\.?\s*",
        title_part,
        maxsplit=1,
    )[0]

    # 「HAPPY DAY - ナースロボ_タイプT」のような形式
    voice_names = [
        "ナースロボ", "初音ミク", "重音テト", "宮舞モカ", "花隈千冬",
        "可不", "知声", "狐子", "彩澄りりせ", "雨衣", "ゲキヤク",
        "音街ウナ", "鏡音リン", "フリモメン", "Mai", "HARUKA",
        "HAYATO", "VOCALOID",
    ]
    voice_pattern = "|".join(map(re.escape, voice_names))
    title_part = re.sub(
        rf"\s+[-－–—]\s+(?=.*(?:{voice_pattern}|タイプT|鎮痛剤)).*$",
        "",
        title_part,
        flags=re.IGNORECASE,
    )

    # 動画タイトル末尾に付くイベント名・ジャンル等の注記
    title_part = re.sub(
        r"(?:【[^】]*】|\[[^\]]*\])+$",
        "",
        title_part,
    ).strip()

    # (○○ver) のようなバージョン注記
    title_part = re.sub(
        r"\s*[(（][^()（）]*ver[^()（）]*[)）]\s*$",
        "",
        title_part,
        flags=re.IGNORECASE,
    )

    # 曲名を囲む引用符や余分な空白を除去
    title_part = title_part.strip(" \t\r\n　\"'“”‘’「」『』")
    return title_part.strip()


def extract_mylist_id(url: str) -> str:
    """公開マイリストURLからIDを抽出する。"""
    match = re.search(r"/mylist/(?:#/)?(\d+)", url)
    if not match:
        raise ValueError(
            "マイリストIDをURLから取得できませんでした。"
            "公開マイリストのURLを確認してください。"
        )
    return match.group(1)


def request_mylist_page(
    session: requests.Session,
    mylist_id: str,
    page: int,
) -> dict:
    """APIからマイリストの指定ページを取得する。"""
    url = API_URL.format(mylist_id=mylist_id)
    params = {"page": page, "pageSize": PAGE_SIZE}

    for attempt in range(4):
        try:
            response = session.get(
                url,
                params=params,
                headers=API_HEADERS,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    wait_seconds = 2 ** (attempt + 1)
                    print(
                        f"HTTP {response.status_code}: "
                        f"{wait_seconds}秒後に再試行します..."
                    )
                    time.sleep(wait_seconds)
                    continue

            response.raise_for_status()
            payload = response.json()
            mylist = (payload.get("data") or {}).get("mylist")

            if not isinstance(mylist, dict):
                raise RuntimeError(
                    "API応答に data.mylist がありません。"
                    "マイリストが公開されているか、URLを確認してください。"
                )

            return mylist

        except requests.RequestException:
            if attempt == 3:
                raise
            wait_seconds = 2 ** (attempt + 1)
            print(f"通信エラー: {wait_seconds}秒後に再試行します...")
            time.sleep(wait_seconds)

    raise RuntimeError("マイリスト情報を取得できませんでした。")


def get_owner_name(video: dict) -> str:
    """動画情報から投稿者名を取り出す。"""
    owner = video.get("owner") or {}

    name = owner.get("name")
    if name:
        return str(name)

    user = owner.get("user") or {}
    name = user.get("nickname") or owner.get("nickname")
    if name:
        return str(name)

    channel = video.get("channel") or {}
    return str(channel.get("name") or channel.get("nickname") or "")


def get_thumbnail_url(video: dict) -> str:
    """サムネイルURLを取り出す。"""
    thumbnail = video.get("thumbnail") or {}

    for key in ("nHdUrl", "largeUrl", "listingUrl", "url"):
        value = thumbnail.get(key)
        if value:
            return str(value)

    return ""


def fetch_all_videos(mylist_url: str) -> tuple[str, str, list[dict]]:
    """公開マイリスト内の動画をすべて取得する。"""
    mylist_id = extract_mylist_id(mylist_url)
    session = requests.Session()

    print(f"マイリストID: {mylist_id}")
    print("動画情報を取得しています...")

    videos = []
    mylist_name = ""
    page = 1

    while True:
        mylist = request_mylist_page(session, mylist_id, page)

        if not mylist_name:
            mylist_name = str(mylist.get("name") or "")

        items = mylist.get("items") or []
        if not items:
            break

        for item in items:
            video = item.get("video") or item
            video_id = str(video.get("id") or "")

            # 削除済み動画など、動画IDがない項目はスキップ
            if not video_id:
                print("動画IDを取得できない項目をスキップしました。")
                continue

            video_title = str(video.get("title") or "")

            videos.append({
                "順番": len(videos) + 1,
                "曲名": extract_song_title(video_title),
                "動画タイトル": video_title,
                "投稿者名": get_owner_name(video),
                "動画ID": video_id,
                "ニコニコ動画URL": (
                    f"https://www.nicovideo.jp/watch/{video_id}"
                ),
                "サムネイルURL": get_thumbnail_url(video),
            })

        print(f"{len(videos)}件取得（{page}ページ目）")
        page += 1
        time.sleep(REQUEST_INTERVAL_SECONDS)

        if page > 5000:
            raise RuntimeError("ページ数が上限を超えたため中断しました。")

    session.close()
    return mylist_id, mylist_name, videos


def save_csv(videos: list[dict], output_path: str) -> None:
    """Googleスプレッドシートに取り込めるCSVを保存する。"""
    path = Path(output_path)

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()

        for video in videos:
            writer.writerow({
                "順番": video["順番"],
                "曲名": video["曲名"],
                "動画タイトル": video["動画タイトル"],
                "投稿者名": video["投稿者名"],
                "作者名": video["投稿者名"],
                "動画ID": video["動画ID"],
                "ニコニコ動画URL": video["ニコニコ動画URL"],
                "サムネイルURL": video["サムネイルURL"],
            })

    print(f"CSVを保存しました: {path.resolve()}")


def write_google_sheet(videos: list[dict]) -> None:
    """SPREADSHEET_IDが設定されている場合、シートへ直接書き込む。"""
    if not SPREADSHEET_ID:
        return

    try:
        import gspread
    except ImportError as exc:
        raise RuntimeError(
            "Google Sheets連携には gspread が必要です。"
            " pip install gspread google-auth を実行してください。"
        ) from exc

    gc = gspread.service_account(filename=SERVICE_ACCOUNT_FILE)
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME,
            rows=max(len(videos) + 10, 100),
            cols=len(COLUMNS),
        )

    values = [COLUMNS]
    for video in videos:
        values.append([
            video["順番"],
            video["曲名"],
            video["動画タイトル"],
            video["投稿者名"],
            video["投稿者名"],
            video["動画ID"],
            video["ニコニコ動画URL"],
            video["サムネイルURL"],
        ])

    # 指定したワークシートの内容を置き換える
    worksheet.clear()
    worksheet.update(
        values=values,
        range_name="A1",
        value_input_option="RAW",
    )

    print(f"Googleスプレッドシートに書き込みました: {spreadsheet.url}")
    print(f"ワークシート: {WORKSHEET_NAME}")


def main() -> None:
    try:
        mylist_id, mylist_name, videos = fetch_all_videos(MYLIST_URL)

        if not videos:
            print("動画が1件も取得できませんでした。")
            return

        save_csv(videos, OUTPUT_CSV)
        write_google_sheet(videos)

        print("\n取得完了")
        print(f"マイリスト名: {mylist_name}")
        print(f"マイリストID: {mylist_id}")
        print(f"取得件数: {len(videos)}")

    except Exception as error:
        print(f"\nエラー: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
