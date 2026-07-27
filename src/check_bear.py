from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


# プロジェクトの場所
ROOT_DIR = Path(__file__).resolve().parents[1]

SOURCE_FILE = ROOT_DIR / "config" / "sources.json"
STATE_FILE = ROOT_DIR / "data" / "notified.json"


# クマ関連キーワード
BEAR_RE = re.compile(
    r"(熊|クマ|くま|ツキノワグマ)",
    re.IGNORECASE
)


def now_iso() -> str:
    """現在時刻をISO形式で返す"""
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    """JSONファイルを読み込む"""
    if not path.exists():
        return default

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json(path: Path, data: Any) -> None:
    """JSONファイルを保存する"""
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as file:
        json.dump(
            data,
            file,
            ensure_ascii=False,
            indent=2
        )


def normalize_lines(soup):
    """
    HTMLから表示されている文字を取り出す。
    script、styleなどは除外する。
    """

    for tag in soup.find_all(
        ["script", "style", "noscript", "svg"]
    ):
        tag.decompose()

    text = soup.get_text(
        "\n",
        strip=True
    )

    lines = []

    for line in text.splitlines():
        line = re.sub(
            r"\s+",
            " ",
            line
        ).strip()

        if line:
            lines.append(line)

    return lines


def extract_target_text(html_text: str) -> str:
    """
    HTMLから、クマに関係する文字を取り出す。
    地域キーワードは使用しない。
    """

    soup = BeautifulSoup(
        html_text,
        "html.parser"
    )

    lines = normalize_lines(soup)

    all_text = "\n".join(lines)

    # クマ関連キーワードがなければ対象外
    if not BEAR_RE.search(all_text):
        return ""

    selected_lines = []

    for index, line in enumerate(lines):
        if BEAR_RE.search(line):
            start = max(0, index - 1)
            end = min(len(lines), index + 2)

            for selected in lines[start:end]:
                if selected not in selected_lines:
                    selected_lines.append(selected)

    return "\n".join(selected_lines)


def make_hash(text: str) -> str:
    """文字列から比較用のハッシュ値を作成する"""
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def fetch_page(source: dict[str, str]) -> str:
    """Webページを取得する"""

    response = requests.get(
        source["url"],
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "compatible; BearAlert/1.0"
            )
        },
        timeout=30
    )

    response.raise_for_status()

    return extract_target_text(response.text)


def send_email(items: list[dict[str, str]]) -> None:
    """新しい情報をメール送信する"""

    smtp_host = os.getenv(
        "SMTP_HOST",
        "smtp.gmail.com"
    )

    smtp_port = int(
        os.getenv(
            "SMTP_PORT",
            "465"
        )
    )

    smtp_user = os.getenv(
        "SMTP_USER",
        ""
    ).strip()

    smtp_password = os.getenv(
        "SMTP_PASSWORD",
        ""
    ).strip()

    mail_from = os.getenv(
        "MAIL_FROM",
        smtp_user
    ).strip()

    mail_to_text = os.getenv(
        "MAIL_TO",
        ""
    ).strip()

    if not smtp_user:
        raise RuntimeError(
            "SMTP_USERが設定されていません"
        )

    if not smtp_password:
        raise RuntimeError(
            "SMTP_PASSWORDが設定されていません"
        )

    if not mail_to_text:
        raise RuntimeError(
            "MAIL_TOが設定されていません"
        )

    mail_to = [
        address.strip()
        for address in mail_to_text.split(",")
        if address.strip()
    ]

    body_parts = [
        "クマに関する新しい情報が見つかりました。",
        "",
        f"取得日時: {now_iso()}",
        "",
    ]

    for number, item in enumerate(
        items,
        start=1
    ):
        body_parts.extend(
            [
                f"----- 情報 {number} -----",
                f"情報源: {item['name']}",
                f"URL: {item['url']}",
                "",
                item["text"],
                "",
            ]
        )

    body_parts.extend(
        [
            "※この通知は自動取得によるものです。",
            "※緊急時は自治体・警察などの公式情報を確認してください。",
        ]
    )

    message = EmailMessage()
    message["Subject"] = "【クマ情報】新しい情報があります"
    message["From"] = mail_from
    message["To"] = ", ".join(mail_to)
    message.set_content(
        "\n".join(body_parts)
    )

    with smtplib.SMTP_SSL(
        smtp_host,
        smtp_port,
        timeout=30
    ) as smtp:
        smtp.login(
            smtp_user,
            smtp_password
        )

        smtp.send_message(message)

    print("メールを送信しました")


def main() -> None:
    sources = load_json(
        SOURCE_FILE,
        []
    )

    state = load_json(
        STATE_FILE,
        {
            "sources": {}
        }
    )

    if "sources" not in state:
        state["sources"] = {}

    new_items = []
    state_changed = False
    success_count = 0

    for source in sources:
        name = source["name"]
        url = source["url"]

        print("=" * 60)
        print(f"確認中: {name}")
        print(url)

        try:
            target_text = fetch_page(source)
            success_count += 1

            if not target_text:
                print(
                    "クマ関連情報をページ内から検出できませんでした"
                )
                continue

            current_hash = make_hash(
                target_text
            )

            previous = state["sources"].get(
                name
            )

            if previous is None:
                # 初回は通知せず、現在の状態を保存する
                print(
                    "初回確認のため、通知せず記録します"
                )

                state["sources"][name] = {
                    "url": url,
                    "hash": current_hash,
                    "text": target_text,
                    "saved_at": now_iso()
                }

                state_changed = True

            elif previous.get("hash") != current_hash:
                # 前回と内容が変わった場合
                print(
                    "ページ内のクマ情報が変わりました"
                )

                new_items.append(
                    {
                        "name": name,
                        "url": url,
                        "text": target_text
                    }
                )

                state["sources"][name] = {
                    "url": url,
                    "hash": current_hash,
                    "text": target_text,
                    "saved_at": now_iso()
                }

                state_changed = True

            else:
                print("変更はありません")

        except Exception as error:
            print(f"取得エラー: {error}")

    if success_count == 0:
        raise RuntimeError(
            "すべてのWebページの取得に失敗しました"
        )

    # 新しい情報がある場合だけメール送信
    if new_items:
        send_email(new_items)
    else:
        print("新しい情報はありません")

    # 状態を保存
    if state_changed:
        save_json(
            STATE_FILE,
            state
        )

        print("通知済み情報を保存しました")


if __name__ == "__main__":
    main()
