def fetch_section_text(source: dict, heading_text: str) -> str:
    """
    requests を使ってページを取得し、デバッグとして HTML を保存し、
    取得HTMLの先頭をログに出力してから指定見出しのセクションを抜き出して返す。

    注意: JavaScriptで後から描画されるコンテンツは requests では取得できません。
    """
    url = source.get("url", "").strip()
    if not url:
        raise RuntimeError("source.url が空です")

    headers = {"User-Agent": USER_AGENT}

    # GET リクエスト
    response = requests.get(url, headers=headers, timeout=30)

    # デバッグログ：HTTPステータス / 最終URL（リダイレクト後）
    try:
        print("DEBUG: HTTP status:", getattr(response, "status_code", None))
        print("DEBUG: Response final URL:", getattr(response, "url", ""))
    except Exception:
        pass

    # デバッグ保存（data/debug/page_<safe_name>.html）
    try:
        debug_dir = ROOT_DIR / "data" / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^0-9A-Za-z_-]", "_", source.get("name", "source"))
        debug_path = debug_dir / f"page_{safe_name}.html"
        with open(debug_path, "w", encoding="utf-8") as f:
            f.write(response.text)
        print("Saved debug HTML to:", str(debug_path))
    except Exception as e:
        print("Failed to save debug HTML:", e)

    # ログに HTML の先頭を出力（プレビュー）
    try:
        preview = response.text[:1000]  # 先頭1000文字を出力
        print("DEBUG HTML preview (first 1000 chars):")
        print(preview)
    except Exception as e:
        print("Failed to print HTML preview:", e)

    # HTTP エラーがあればここで例外として上げる（必要に応じて）
    response.raise_for_status()

    html = response.text
    # 既存の extract_section_by_heading を使って該当セクションを抽出
    return extract_section_by_heading(html, heading_text)
