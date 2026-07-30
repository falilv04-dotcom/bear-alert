from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, List, Dict

import requests
from bs4 import BeautifulSoup


# プロジェクトの場所
ROOT_DIR = Path(__file__).resolve().parents[1]

SOURCE_FILE = ROOT_DIR / "config" / "sources.json"
STATE_FILE = ROOT_DIR / "data" / "notified.json"


# 監視する見出し（ページ上の正確な文章に合わせてください）
HEADING_TO_WATCH = "滋賀県の最新出没傾向"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def normalize_lines(soup: BeautifulSoup) -> List\[str]:
    for tag in soup.find_all(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def make_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def extract_section_by_heading(html_text: str, heading_text: str) -> str:
    """
    指定見出しに続くセクションのテキストを返す。
    見出しが見つからなければ空文字を返す。
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # 1) 見出しタグ(h1..h6)を探す
    heading = None
    for tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        heading = soup.find(tag_name, string=lambda s: s and heading_text in s)
        if heading:
            break

    # 2) 見出しとして strong/b/p 等を探す
    if not heading:
        heading = soup.find(lambda tag: tag.name in ("strong", "b", "p", "div") and tag.get_text() and heading_text in tag.get_text())

    if not heading:
        return ""

    # 3) heading の次の兄弟要素を集める（上限あり）
    texts = []
    node = heading.next_sibling
    count = 0
    while node is not None and count < 50:
        if getattr(node, "get_text", None):
            txt = node.get_text(" ", strip=True)
            if txt:
                texts.append(txt)
        elif isinstance(node, str):
            s = node.strip()
            if s:
                texts.append(s)
        node = node.next_sibling
        count += 1

    if texts:
        return "\n".join(texts).strip()

    # 4) 兄弟がなければ親要素のテキストから切り出す（フォールバック）
    parent = heading.parent
    if parent:
        full = parent.get_text(" ", strip=True)
        # 先頭の見出し文言を取り除く
        full = full.replace(heading_text, "", 1).strip()
        return full

    return ""


def fetch_section_text(source: dict, heading_text: str) -> str:
    url = source.get("url", "").strip()
    if not url:
        raise RuntimeError("source.url が空です")
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BearAlert/1.0)"},
        timeout=30
    )
    response.raise_for_status()
    html = response.text
    return extract_section_by_heading(html, heading_text)


def send_email(items: List[Dict[str, str]]) -> None:
    """
    items: list of dict with keys: title, url, text, source_name (and optional name)
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    mail_from = os.getenv("MAIL_FROM", smtp_user).strip()
    mail_to_text = os.getenv("MAIL_TO", "").strip()

    if not smtp_user:
        raise RuntimeError("SMTP_USERが設定されていません")
    if not smtp_password:
        raise RuntimeError("SMTP_PASSWORDが設定されていません")
    if not mail_to_text:
        raise RuntimeError("MAIL_TOが設定されていません")

    mail_to = [a.strip() for a in mail_to_text.split(",") if a.strip()]

    body_parts = [
        "クマに関する新しい情報が見つかりました。",
        "",
        f"取得日時: {now_iso()}",
        "",
    ]

    for number, item in enumerate(items, start=1):
        source_name = item.get("name") or item.get("source_name", "")
        title = item.get("title", "")
        url = item.get("url", "")
        text = item.get("text", "")

        body_parts.extend(
            [
                f"----- 情報 {number} -----",
                f"情報源: {source_name}",
                f"タイトル: {title}",
                f"URL: {url}",
                "",
                text,
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
    message["Subject"] = "【クマ情報】該当セクションが更新されました"
    message["From"] = mail_from
    message["To"] = ", ".join(mail_to)
    message.set_content("\n".join(body_parts))

    with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=30) as smtp:
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)

    print("メールを送信しました")


def main() -> None:
    sources = load_json(SOURCE_FILE, [])
    state = load_json(STATE_FILE, {"sources": {}})

    if "sources" not in state:
        state["sources"] = {}

    state_changed = False
    success_count = 0

    for source in sources:
        name = source.get("name", "")
        url = source.get("url", "")

        print("=" * 60)
        print(f"確認中: {name}")
        print(url)

        try:
            section_text = fetch_section_text(source, HEADING_TO_WATCH)
            success_count += 1

            if not section_text:
                print(f"見出し『{HEADING_TO_WATCH}』をページ内で検出できませんでした")
                continue

            current_hash = make_hash(section_text)
            previous = state["sources"].get(name)

            if previous is None:
                # 初回は通知せず記録
                print("初回確認のため、該当セクションは通知せず記録します")
                state["sources"][name] = {
                    "url": url,
                    "section_hash": current_hash,
                    "section_text": section_text,
                    "saved_at": now_iso()
                }
                state_changed = True
                continue

            if previous.get("section_hash") != current_hash:
                print("該当セクションが更新されました")

                new_item = {
                    "id": current_hash,
                    "title": HEADING_TO_WATCH,
                    "url": url,
                    "text": section_text,
                    "source_name": name,
                    "name": name
                }

                # 送信
                send_email([new_item])

                # 保存
                state["sources"][name] = {
                    "url": url,
                    "section_hash": current_hash,
                    "section_text": section_text,
                    "saved_at": now_iso()
                }
                state_changed = True
            else:
                print("対象セクションに変更はありません")

        except Exception as error:
            print(f"取得エラー: {error}")

    if success_count == 0:
        raise RuntimeError("すべてのWebページの取得に失敗しました")

    if state_changed:
        save_json(STATE_FILE, state)
        print("通知済み情報を保存しました")


if __name__ == "__main__":
    main()
