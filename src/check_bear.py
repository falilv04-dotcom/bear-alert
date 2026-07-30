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

# 監視する見出しのリスト（ページ上の正確な文言に合わせて必要に応じて編集）
HEADING_LIST = [
    "滋賀県の最新出没傾向",
    "滋賀県の最新の出没情報"
]

# User-Agent（必要なら変更）
USER_AGENT = "Mozilla/5.0 (compatible; BearAlert/1.0)"


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


def normalize_lines(soup):
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
    指定見出しに続くセクションのテキストを返す（部分一致）。
    見出しが見つからなければ空文字を返す。
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # 1) 見出しタグ(h1..h6)を探す（部分一致）
    heading = None
    for tag_name in ["h1", "h2", "h3", "h4", "h5", "h6"]:
        heading = soup.find(tag_name, string=lambda s: s and heading_text in s)
        if heading:
            break

    # 2) 見出しとして strong/b/p/div 等も探す
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

    # 4) 兄弟がない場合は親要素のテキストを返す（フォールバック）
    parent = heading.parent
    if parent:
        full = parent.get_text(" ", strip=True)
        full = full.replace(heading_text, "", 1).strip()
        return full

    return ""


# Playwright を使う版の fetch_section_text
# 注意: playwright がインストールされている前提です（requirements に追加）
from playwright.sync_api import sync_playwright

def fetch_section_text_playwright(source: dict, heading_text: str, timeout: int = 30000) -> str:
    """
    Playwright を使ってページを開き、JS実行後のHTMLからセクションを抽出する。
    同期APIを使うため、GitHub Actions でもそのまま動きます。
    """
    url = source.get("url", "").strip()
    if not url:
        raise RuntimeError("source.url が空です")

    # 起動・ページ取得
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)  # headless で起動
        context = browser.new_context()
        page = context.new_page()

        # ページを開く。networkidle まで待つ（必要なら wait_for_timeout を追加）
        page.goto(url, wait_until="networkidle", timeout=timeout)

        # ページのHTMLを取得（JSで描画済みのDOM）
        html = page.content()

        # デバッグ保存（data/debug に保存しておく）
        try:
            debug_dir = ROOT_DIR / "data" / "debug"
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^0-9A-Za-z_-]", "_", source.get("name", "source"))
            debug_path = debug_dir / f"pw_page_{safe_name}.html"
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            print("Saved Playwright debug HTML to:", str(debug_path))
        except Exception as e:
            print("Failed to save Playwright debug HTML:", e)

        # 終了
        page.close()
        context.close()
        browser.close()

    # 取得したHTMLからセクションを抽出して返す
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
            updated_any = False

            for heading_text in HEADING_LIST:
                print(f"  チェック見出し: {heading_text}")

                section_text = fetch_section_text(source, heading_text)
                if not section_text:
                    print(f"    見出し『{heading_text}』をページ内で検出できませんでした")
                    continue

                current_hash = make_hash(section_text)
                previous = state["sources"].get(name)

                # 初回: stateが無ければ見出しごとに保存（通知しない）
                if previous is None:
                    print("    初回確認のため、該当セクションは通知せず記録します")
                    state["sources"][name] = {
                        "url": url,
                        "section_hashes": {heading_text: current_hash},
                        "section_texts": {heading_text: section_text},
                        "saved_at": now_iso()
                    }
                    state_changed = True
                    # 初回は個別に登録→次の見出しへ
                    continue

                # ensure keys exist
                if "section_hashes" not in previous:
                    previous["section_hashes"] = {}
                if "section_texts" not in previous:
                    previous["section_texts"] = {}

                prev_hash = previous["section_hashes"].get(heading_text)

                if prev_hash != current_hash:
                    print(f"    見出し『{heading_text}』が更新されました")

                    new_item = {
                        "id": current_hash,
                        "title": heading_text,
                        "url": url,
                        "text": section_text,
                        "source_name": name,
                        "name": name
                    }

                    send_email([new_item])

                    # 保存（上書き）
                    state["sources"][name]["section_hashes"][heading_text] = current_hash
                    state["sources"][name]["section_texts"][heading_text] = section_text
                    state["sources"][name]["saved_at"] = now_iso()

                    state_changed = True
                    updated_any = True
                else:
                    print(f"    見出し『{heading_text}』に変更はありません")

            if not updated_any:
                print("  このソースの監視見出しに更新はありません")

            success_count += 1

        except Exception as error:
            print(f"取得エラー: {error}")

    if success_count == 0:
        raise RuntimeError("すべてのWebページの取得に失敗しました")

    if state_changed:
        save_json(STATE_FILE, state)
        print("通知済み情報を保存しました")


if __name__ == "__main__":
    main()
