#!/usr/bin/env python3
"""
OD Research Dashboard Auto-Update Script
=========================================
J-STAGE API + CiNii Research API + HBR RSSから実際の論文・記事を取得し、
Anthropic Claude APIで日本語要約・OD実務示唆を生成して
ダッシュボード (index.html) を更新します。

使い方:
  pip install anthropic requests
  export ANTHROPIC_API_KEY="sk-ant-..."
  python update_dashboard.py

GitHub Actionsで自動実行する場合はこのスクリプトをそのまま使用。
"""

import os
import json
import time
import hashlib
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
import anthropic

# ──────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OUTPUT_FILE = "index.html"
CACHE_FILE = "paper_cache.json"
MAX_PAPERS = 40  # ダッシュボードに表示する最大件数

# 検索キーワード定義
SEARCH_QUERIES = {
    "OD": [
        "組織開発", "organization development", "OD intervention",
        "dialogic OD", "psychological safety organization"
    ],
    "CAP": [
        "組織能力開発", "organizational capability", "learning agility",
        "dynamic capabilities", "workforce skills"
    ],
    "SUC": [
        "後継者育成", "succession planning", "leadership pipeline",
        "talent management succession", "high potential development"
    ],
    "REP": [
        "HR trends", "human capital", "talent trends report",
        "人材開発", "organizational performance report"
    ]
}

# J-STAGE 検索対象ジャーナル (ISSN)
JSTAGE_JOURNALS = [
    "jodp",    # 組織開発実践ジャーナル
    "hrmj",    # 人材教育・労働研究
    "sanro",   # 産業・組織心理学研究
]

# HBR RSSフィード（OD/HR関連トピック）
HBR_RSS_FEEDS = {
    "OD": [
        "https://hbr.org/topic/organizational-transformation/feed",
        "https://hbr.org/topic/change-management/feed",
    ],
    "CAP": [
        "https://hbr.org/topic/human-resource-management/feed",
        "https://hbr.org/topic/talent-management/feed",
        "https://hbr.org/topic/continuous-learning/feed",
    ],
    "SUC": [
        "https://hbr.org/topic/succession-planning/feed",
        "https://hbr.org/topic/leadership-development/feed",
    ],
    "REP": [
        "https://hbr.org/topic/organizational-culture/feed",
        "https://hbr.org/topic/leadership/feed",
    ],
}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ──────────────────────────────────────────────
# J-STAGE API
# ──────────────────────────────────────────────
def fetch_jstage(keyword: str, count: int = 10) -> list[dict]:
    """J-STAGE APIから論文を取得"""
    url = "https://api.jstage.jst.go.jp/searchapi/do"
    params = {
        "service": "3",
        "text": keyword,
        "count": count,
        "lang": "ja",
        "sortorder": "2",  # 新着順
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        return parse_jstage_xml(resp.content)
    except Exception as e:
        print(f"  J-STAGE fetch error ({keyword}): {e}")
        return []


def parse_jstage_xml(xml_bytes: bytes) -> list[dict]:
    """J-STAGE APIのAtom/XMLを解析"""
    NS = {
        "atom":  "http://www.w3.org/2005/Atom",
        "dc":    "http://purl.org/dc/elements/1.1/",
        "prism": "http://prismstandard.org/namespaces/basic/2.0/",
        "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
    }
    papers = []
    try:
        root = ET.fromstring(xml_bytes)
        for entry in root.findall("atom:entry", NS):
            def get(tag, ns_key):
                el = entry.find(f"{ns_key}:{tag}", NS)
                return el.text.strip() if el is not None and el.text else ""

            title = get("title", "dc") or get("title", "atom")
            if not title:
                continue

            doi = get("doi", "prism")
            link_el = entry.find("atom:link[@rel='alternate']", NS)
            url = link_el.get("href") if link_el is not None else (
                f"https://doi.org/{doi}" if doi else "https://www.jstage.jst.go.jp/"
            )

            papers.append({
                "source_db": "J-STAGE",
                "title": title,
                "authors": get("creator", "dc"),
                "year": (get("publicationDate", "prism") or "")[:4],
                "abstract": get("description", "dc"),
                "doi": doi,
                "url": url,
            })
    except Exception as e:
        print(f"  XML parse error: {e}")
    return papers


# ──────────────────────────────────────────────
# CiNii Research API
# ──────────────────────────────────────────────
def fetch_cinii(keyword: str, count: int = 10) -> list[dict]:
    """CiNii Research APIから論文を取得"""
    url = "https://cir.nii.ac.jp/opensearch/articles"
    params = {
        "q": keyword,
        "format": "json",
        "count": count,
        "sortorder": "2",
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        papers = []
        for item in data.get("items", []):
            title = item.get("title", "")
            if not title:
                continue
            creators = item.get("creator", [])
            authors = ", ".join(
                c.get("familyName", "") + c.get("givenName", "")
                if isinstance(c, dict) else str(c)
                for c in creators
            )
            link = item.get("@id", "")
            papers.append({
                "source_db": "CiNii",
                "title": title,
                "authors": authors,
                "year": (item.get("dateCreated", "") or "")[:4],
                "abstract": item.get("description", ""),
                "doi": item.get("doi", ""),
                "url": link or "https://cir.nii.ac.jp/",
            })
        return papers
    except Exception as e:
        print(f"  CiNii fetch error ({keyword}): {e}")
        return []


# ──────────────────────────────────────────────
# Claude API: 日本語要約 & OD実務示唆生成
# ──────────────────────────────────────────────
def generate_insight(paper: dict, theme: str) -> dict:
    """Claudeで日本語要約とOD実務示唆を生成"""
    prompt = f"""以下の論文情報をもとに、OD（組織開発）実践者向けの情報を日本語で生成してください。

【論文情報】
タイトル: {paper['title']}
著者: {paper.get('authors', '不明')}
年: {paper.get('year', '不明')}
要旨（英語/日本語）: {paper.get('abstract', 'なし')}
テーマ分類: {theme}

以下のJSONフォーマットで回答してください（他の文字は不要）:
{{
  "titleJa": "日本語タイトル（なければ英語から翻訳、30文字以内）",
  "abstractJa": "日本語要旨（100〜150字。内容・方法・主要知見を簡潔に）",
  "insight": "【要点】〜（1〜2文）。\\n【実務適用】〜（組織開発実践者が明日から使える具体的行動、2〜3文）。\\n【注意点】〜（落とし穴や日本企業への適用上の注意、1〜2文）。",
  "importance": 重要度1〜5の整数（OD実践への重要性）,
  "novelty": 新規性1〜5の整数（知見の新しさ）
}}"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        # JSONブロックを抽出
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        result = json.loads(text)
        return result
    except Exception as e:
        print(f"  Claude API error: {e}")
        return {
            "titleJa": paper["title"][:40],
            "abstractJa": paper.get("abstract", "")[:150],
            "insight": "【要点】要約生成に失敗しました。\n【実務適用】原文をご参照ください。\n【注意点】なし。",
            "importance": 3,
            "novelty": 3,
        }


# ──────────────────────────────────────────────
# HBR RSS フィード
# ──────────────────────────────────────────────
def fetch_hbr(theme: str, count: int = 8) -> list[dict]:
    """HBR RSSフィードからOD関連記事を取得"""
    feeds = HBR_RSS_FEEDS.get(theme, [])
    papers = []
    seen_urls = set()

    for feed_url in feeds:
        try:
            resp = requests.get(feed_url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; OD-Dashboard/1.0)"
            })
            resp.raise_for_status()

            # RSS/Atom XML解析
            root = ET.fromstring(resp.content)

            # RSS 2.0 形式
            for item in root.findall(".//item"):
                def get_text(tag):
                    el = item.find(tag)
                    return el.text.strip() if el is not None and el.text else ""

                title = get_text("title")
                link = get_text("link")
                if not title or link in seen_urls:
                    continue
                seen_urls.add(link)

                # 日付から年を取得
                pub_date = get_text("pubDate")
                year = ""
                for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z"]:
                    try:
                        year = str(datetime.strptime(pub_date[:25].strip(), fmt).year)
                        break
                    except:
                        pass
                if not year:
                    year = str(datetime.now().year)

                description = get_text("description")
                # HTMLタグを除去
                import re
                description = re.sub(r"<[^>]+>", "", description).strip()

                papers.append({
                    "source_db": "HBR",
                    "title": title,
                    "authors": get_text("dc:creator") or "HBR Editors",
                    "year": year,
                    "abstract": description[:300] if description else "",
                    "doi": "",
                    "url": link,
                })

                if len(papers) >= count:
                    return papers

        except Exception as e:
            print(f"  HBR RSS fetch error ({feed_url}): {e}")

    return papers


# ──────────────────────────────────────────────
# 重複排除
# ──────────────────────────────────────────────
def paper_id(paper: dict) -> str:
    """論文の一意IDを生成（タイトルベース）"""
    key = paper.get("doi") or paper.get("title", "")
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ──────────────────────────────────────────────
# メイン: 論文取得 + AI処理
# ──────────────────────────────────────────────
def collect_papers() -> list[dict]:
    """全データソースから論文・記事を収集（J-STAGE / CiNii / HBR）"""
    seen_ids = set()
    all_raw = []

    for theme, keywords in SEARCH_QUERIES.items():
        print(f"\n[{theme}] 論文収集中...")

        # ── J-STAGE ──
        for kw in keywords:
            print(f"  J-STAGE: {kw}")
            for p in fetch_jstage(kw, count=5):
                pid = paper_id(p)
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    p["theme"] = theme
                    p["_pid"] = pid
                    all_raw.append(p)
            time.sleep(0.5)

        # ── CiNii ──
        for kw in keywords:
            print(f"  CiNii: {kw}")
            for p in fetch_cinii(kw, count=5):
                pid = paper_id(p)
                if pid not in seen_ids:
                    seen_ids.add(pid)
                    p["theme"] = theme
                    p["_pid"] = pid
                    all_raw.append(p)
            time.sleep(0.5)

        # ── HBR RSS ──
        print(f"  HBR RSS: {theme}")
        for p in fetch_hbr(theme, count=6):
            pid = paper_id(p)
            if pid not in seen_ids:
                seen_ids.add(pid)
                p["theme"] = theme
                p["_pid"] = pid
                all_raw.append(p)
        time.sleep(0.5)

    print(f"\n収集: {len(all_raw)}件（重複排除後）")
    return all_raw


def process_papers(raw_papers: list[dict]) -> list[dict]:
    """Claudeで各論文の日本語化・示唆生成"""
    # キャッシュ読み込み（API節約）
    cache = {}
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)

    processed = []
    source_map = {
        "J-STAGE": "J-STAGE",
        "CiNii": "CiNii",
        "HBR": "HBR",
    }

    for i, p in enumerate(raw_papers[:MAX_PAPERS]):
        pid = p["_pid"]
        print(f"  [{i+1}/{min(len(raw_papers), MAX_PAPERS)}] {p['title'][:50]}...")

        if pid in cache:
            print("    (キャッシュ使用)")
            enriched = cache[pid]
        else:
            enriched = generate_insight(p, p["theme"])
            cache[pid] = enriched
            time.sleep(0.3)  # API負荷軽減

        year = int(p.get("year") or 0) or datetime.now().year
        processed.append({
            "id": pid,
            "theme": p["theme"],
            "title": p["title"],
            "titleJa": enriched.get("titleJa", p["title"][:40]),
            "authors": p.get("authors", "不明"),
            "year": year,
            "source": source_map.get(p["source_db"], p["source_db"]),
            "importance": enriched.get("importance", 3),
            "novelty": enriched.get("novelty", 3),
            "url": p.get("url", ""),
            "abstract": enriched.get("abstractJa", p.get("abstract", "")),
            "insight": enriched.get("insight", ""),
        })

    # キャッシュ保存
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)

    return processed


# ──────────────────────────────────────────────
# HTML生成
# ──────────────────────────────────────────────
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>論文・学術研究専門ダッシュボード | GCS OD Intelligence</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Hiragino Sans','Yu Gothic UI',sans-serif;background:#f0f4f8;color:#1e293b;min-height:100vh}}
.header{{background:linear-gradient(135deg,#1e3a5f 0%,#1a56db 100%);padding:14px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100;box-shadow:0 2px 12px rgba(0,0,0,.2)}}
.header-left h1{{font-size:17px;font-weight:800;color:#fff}}
.header-left .subtitle{{font-size:11px;color:rgba(255,255,255,.7);margin-top:2px}}
.update-badge{{background:rgba(255,255,255,.15);color:#fff;border:1.5px solid rgba(255,255,255,.4);border-radius:8px;padding:6px 14px;font-size:11px;font-weight:700}}
.filterbar{{background:#fff;border-bottom:1px solid #e2e8f0;padding:10px 24px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}}
.fl{{font-size:11px;color:#64748b;font-weight:600;white-space:nowrap}}
.search-wrap{{position:relative;display:flex;align-items:center}}
.search-input{{border:1.5px solid #e2e8f0;border-radius:8px;padding:7px 10px 7px 28px;font-size:12px;width:190px;outline:none}}
.search-input:focus{{border-color:#1a56db}}
.filter-select{{border:1.5px solid #e2e8f0;border-radius:8px;padding:7px 10px;font-size:12px;background:#fff;cursor:pointer;outline:none}}
.sort-btns{{margin-left:auto;display:flex;gap:6px}}
.sort-btn{{border:1.5px solid #e2e8f0;border-radius:8px;padding:7px 14px;font-size:12px;background:#fff;cursor:pointer;color:#475569;font-weight:600}}
.sort-btn.active{{background:#1a56db;color:#fff;border-color:#1a56db}}
.cat-strip{{background:#fff;border-bottom:1px solid #e2e8f0;padding:0 24px;display:flex;overflow-x:auto}}
.cat-item{{padding:12px 20px;font-size:13px;font-weight:500;color:#64748b;cursor:pointer;border-bottom:3px solid transparent;white-space:nowrap;display:flex;align-items:center;gap:7px}}
.cat-item:hover{{color:#1a56db}}
.cat-item.active{{color:#1a56db;border-bottom-color:#1a56db;font-weight:700}}
.cat-cnt{{background:#e8f0fe;color:#1a56db;border-radius:12px;padding:2px 8px;font-size:10px;font-weight:700}}
.cat-item.active .cat-cnt{{background:#1a56db;color:#fff}}
.content{{padding:16px 24px 32px}}
.result-info{{font-size:12px;color:#64748b;margin-bottom:14px}}
.papers-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}}
.paper-card{{background:#fff;border:1px solid #e8edf5;border-radius:14px;padding:16px;transition:box-shadow .2s,transform .15s;display:flex;flex-direction:column}}
.paper-card:hover{{box-shadow:0 8px 28px rgba(0,0,0,.1);transform:translateY(-2px)}}
.card-top{{display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:10px}}
.theme-badge{{font-size:10px;font-weight:700;padding:4px 10px;border-radius:20px;white-space:nowrap}}
.badge-OD{{background:#dbeafe;color:#1d4ed8}}.badge-CAP{{background:#dcfce7;color:#15803d}}
.badge-SUC{{background:#fef3c7;color:#92400e}}.badge-REP{{background:#f3e8ff;color:#7c3aed}}
.scores{{display:flex;flex-direction:column;gap:4px;align-items:flex-end}}
.score-row{{display:flex;align-items:center;gap:3px;font-size:10px;color:#94a3b8}}
.score-num{{font-weight:700;color:#475569}}
.paper-title{{font-size:13px;font-weight:700;line-height:1.5;color:#1e293b;margin-bottom:5px}}
.paper-title-ja{{font-size:11px;color:#1a56db;background:#eff6ff;border-radius:5px;padding:3px 8px;margin-bottom:10px;line-height:1.5;display:inline-block;border-left:3px solid #bfdbfe}}
.paper-meta{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px;align-items:center;font-size:11px;color:#64748b}}
.source-pill{{background:#f1f5f9;border-radius:6px;padding:2px 8px;font-size:10px;font-weight:700;color:#475569;border:1px solid #e2e8f0}}
.abstract-text{{font-size:11.5px;color:#475569;line-height:1.75;margin-bottom:12px;padding:8px 10px;background:#f8fafc;border-radius:7px;border-left:3px solid #cbd5e1;flex-grow:1}}
.insight-section{{background:linear-gradient(135deg,#fffbeb,#fef9ee);border:1px solid #fde68a;border-radius:9px;overflow:hidden;margin-top:auto}}
.insight-header{{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;cursor:pointer;user-select:none}}
.insight-label{{font-size:11px;font-weight:700;color:#92400e}}
.insight-arrow{{font-size:10px;color:#92400e;transition:transform .25s;display:inline-block}}
.insight-arrow.open{{transform:rotate(180deg)}}
.insight-body{{padding:0 12px;max-height:0;overflow:hidden;transition:max-height .3s ease,padding .3s ease}}
.insight-body.open{{max-height:500px;padding:2px 12px 12px}}
.insight-body p{{font-size:11.5px;color:#78350f;line-height:1.8;white-space:pre-wrap}}
.card-footer{{display:flex;justify-content:flex-end;margin-top:12px;padding-top:10px;border-top:1px solid #f1f5f9}}
.url-link{{font-size:11px;color:#1a56db;text-decoration:none;font-weight:600}}
.url-link:hover{{text-decoration:underline}}
.empty-state{{text-align:center;padding:60px 20px;color:#94a3b8;grid-column:1/-1;font-size:14px}}
.footer-bar{{text-align:center;padding:16px;font-size:11px;color:#94a3b8;border-top:1px solid #e2e8f0;background:#fff}}
.db-badges{{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-left:auto}}
.db-badge{{font-size:9px;font-weight:700;padding:2px 7px;border-radius:10px;border:1px solid}}
.db-jstage{{background:#fff3e0;color:#e65100;border-color:#ffcc02}}
.db-cinii{{background:#e3f2fd;color:#1565c0;border-color:#90caf9}}
.db-hbr{{background:#fce4ec;color:#b71c1c;border-color:#f48fb1}}
@media(max-width:640px){{.papers-grid{{grid-template-columns:1fr}}.sort-btns{{margin-left:0}}}}
</style>
</head>
<body>
<div class="header">
  <div class="header-left">
    <h1>📚 論文・学術研究専門ダッシュボード</h1>
    <div class="subtitle">OD / 組織能力開発 / サクセッション — J-STAGE・CiNii・HBR実論文 + Claude AI要約 | GCS</div>
  </div>
  <div class="update-badge">🔄 最終更新: {updated_at}</div>
</div>
<div class="filterbar">
  <div class="search-wrap">
    <svg style="position:absolute;left:9px;color:#94a3b8" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
    <input class="search-input" id="searchInput" type="text" placeholder="キーワード検索..." oninput="applyFilters()">
  </div>
  <span class="fl">テーマ：</span>
  <select class="filter-select" id="filterTheme" onchange="applyFilters()">
    <option value="">すべて</option>
    <option value="OD">OD（組織開発）</option>
    <option value="CAP">組織能力開発</option>
    <option value="SUC">サクセッション</option>
    <option value="REP">実務レポート</option>
  </select>
  <span class="fl">情報源：</span>
  <select class="filter-select" id="filterSource" onchange="applyFilters()">
    <option value="">すべて</option>
    <option value="J-STAGE">J-STAGE</option>
    <option value="CiNii">CiNii Research</option>
    <option value="HBR">HBR</option>
  </select>
  <span class="fl">年：</span>
  <select class="filter-select" id="filterYear" onchange="applyFilters()">
    <option value="">すべて</option>
    <option value="2026">2026年</option>
    <option value="2025">2025年</option>
    <option value="2024">2024年</option>
    <option value="2023">2023年以前</option>
  </select>
  <div class="sort-btns">
    <button class="sort-btn active" id="btnScore" onclick="setSort('score')">⭐ スコア順</button>
    <button class="sort-btn" id="btnNew" onclick="setSort('new')">🆕 新着順</button>
  </div>
</div>
<div class="cat-strip">
  <div class="cat-item active" data-cat="" onclick="setCat('')">すべて <span class="cat-cnt" id="cnt-all">0</span></div>
  <div class="cat-item" data-cat="OD" onclick="setCat('OD')">🔵 OD（組織開発）<span class="cat-cnt" id="cnt-OD">0</span></div>
  <div class="cat-item" data-cat="CAP" onclick="setCat('CAP')">🟢 組織能力開発 <span class="cat-cnt" id="cnt-CAP">0</span></div>
  <div class="cat-item" data-cat="SUC" onclick="setCat('SUC')">🟡 サクセッション <span class="cat-cnt" id="cnt-SUC">0</span></div>
  <div class="cat-item" data-cat="REP" onclick="setCat('REP')">🟣 実務レポート <span class="cat-cnt" id="cnt-REP">0</span></div>
</div>
<div class="content">
  <div class="result-info" id="resultInfo"></div>
  <div class="papers-grid" id="papersGrid"></div>
</div>
<div class="footer-bar" id="footerBar">J-STAGE・CiNii Research・HBR 実論文データ | Claude AI日本語要約 | GCS OD Intelligence Dashboard</div>
<script>
var PAPERS = {papers_json};
var THEME = {{OD:"OD（組織開発）",CAP:"組織能力開発",SUC:"サクセッション",REP:"実務レポート"}};
var state = {{search:"",cat:"",src:"",yr:"",sort:"score"}};
function esc(s){{return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}}
function stars(n,ch,col){{var h="";for(var i=1;i<=5;i++) h+="<span style=\\"color:"+(i<=n?col:"#d1d5db")+";font-size:12px\\">"+ch+"</span>";return h;}}
function cardHtml(p){{
  var showJa=(p.title!==p.titleJa);
  var insId="ins-"+p.id, togId="tog-"+p.id;
  var srcBadge = p.source==="J-STAGE" ? "<span class=\\"db-badge db-jstage\\">J-STAGE</span>" : p.source==="CiNii" ? "<span class=\\"db-badge db-cinii\\">CiNii</span>" : p.source==="HBR" ? "<span class=\\"db-badge db-hbr\\">HBR</span>" : "<span class=\\"source-pill\\">"+esc(p.source)+"</span>";
  return [
    "<div class=\\"paper-card\\">",
    "<div class=\\"card-top\\"><span class=\\"theme-badge badge-"+esc(p.theme)+"\\">"+esc(THEME[p.theme]||p.theme)+"</span>",
    "<div class=\\"scores\\"><div class=\\"score-row\\"><span style=\\"font-size:10px\\">重要</span> "+stars(p.importance,"★","#f59e0b")+" <span class=\\"score-num\\">"+p.importance+"</span></div>",
    "<div class=\\"score-row\\"><span style=\\"font-size:10px\\">新規</span> "+stars(p.novelty,"✦","#10b981")+" <span class=\\"score-num\\">"+p.novelty+"</span></div></div></div>",
    "<div class=\\"paper-title\\">"+esc(p.title)+"</div>",
    (showJa?"<div class=\\"paper-title-ja\\">🇯🇵 "+esc(p.titleJa)+"</div>":""),
    "<div class=\\"paper-meta\\"><span>✍️ "+esc(p.authors)+"</span><span>📅 "+p.year+"</span>"+srcBadge+"</div>",
    "<div class=\\"abstract-text\\">"+esc(p.abstract||"")+"</div>",
    "<div class=\\"insight-section\\"><div class=\\"insight-header\\" onclick=\\"toggleIns('"+insId+"','"+togId+"')\\">",
    "<span class=\\"insight-label\\">💡 OD実務示唆</span><span class=\\"insight-arrow\\" id=\\""+togId+"\\">▼</span></div>",
    "<div class=\\"insight-body\\" id=\\""+insId+"\\"><p>"+esc(p.insight||"")+"</p></div></div>",
    "<div class=\\"card-footer\\"><a class=\\"url-link\\" href=\\""+esc(p.url)+"\\" target=\\"_blank\\" rel=\\"noopener\\">🔗 出典を開く →</a></div>",
    "</div>"
  ].join("");
}}
function filteredList(){{
  var list=PAPERS.slice();
  var q=state.search.toLowerCase();
  if(q) list=list.filter(function(p){{return(p.title+p.titleJa+p.authors+(p.abstract||"")+(p.insight||"")).toLowerCase().indexOf(q)>=0;}});
  if(state.cat) list=list.filter(function(p){{return p.theme===state.cat;}});
  if(state.src) list=list.filter(function(p){{return p.source===state.src;}});
  if(state.yr){{var yr=parseInt(state.yr);list=list.filter(function(p){{return yr===2023?p.year<=2023:p.year===yr;}});}}
  if(state.sort==="score") list.sort(function(a,b){{return(b.importance+b.novelty)-(a.importance+a.novelty);}});
  else list.sort(function(a,b){{return b.year-a.year||(b.importance+b.novelty)-(a.importance+a.novelty);}});
  return list;
}}
function renderCards(){{
  var list=filteredList();
  document.getElementById("resultInfo").textContent=list.length+"件表示中";
  var grid=document.getElementById("papersGrid");
  if(!list.length){{grid.innerHTML="<div class=\\"empty-state\\">🔍 該当する論文がありません。検索条件を変更してください。</div>";return;}}
  grid.innerHTML=list.map(cardHtml).join("");
}}
function updateCounts(){{
  var c={{all:PAPERS.length,OD:0,CAP:0,SUC:0,REP:0}};
  PAPERS.forEach(function(p){{c[p.theme]=(c[p.theme]||0)+1;}});
  document.getElementById("cnt-all").textContent=c.all;
  document.getElementById("cnt-OD").textContent=c.OD;
  document.getElementById("cnt-CAP").textContent=c.CAP;
  document.getElementById("cnt-SUC").textContent=c.SUC;
  document.getElementById("cnt-REP").textContent=c.REP;
}}
function toggleIns(insId,togId){{
  var body=document.getElementById(insId),tog=document.getElementById(togId);
  if(!body)return;
  var o=body.classList.toggle("open");
  if(tog) tog.classList.toggle("open",o);
}}
function setCat(c){{state.cat=c;document.querySelectorAll(".cat-item").forEach(function(el){{el.classList.toggle("active",el.getAttribute("data-cat")===c);}});document.getElementById("filterTheme").value=c;renderCards();}}
function setSort(s){{state.sort=s;document.getElementById("btnScore").classList.toggle("active",s==="score");document.getElementById("btnNew").classList.toggle("active",s==="new");renderCards();}}
function applyFilters(){{state.search=document.getElementById("searchInput").value;var th=document.getElementById("filterTheme").value;if(th!==state.cat){{state.cat=th;document.querySelectorAll(".cat-item").forEach(function(el){{el.classList.toggle("active",el.getAttribute("data-cat")===th);}});}}state.src=document.getElementById("filterSource").value;state.yr=document.getElementById("filterYear").value;renderCards();}}
updateCounts();renderCards();
</script>
</body>
</html>"""


def generate_html(papers: list[dict]) -> str:
    papers_json = json.dumps(papers, ensure_ascii=False)
    updated_at = datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M UTC")
    return HTML_TEMPLATE.format(
        papers_json=papers_json,
        updated_at=updated_at,
    )


# ──────────────────────────────────────────────
# エントリーポイント
# ──────────────────────────────────────────────
def main():
    if not ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY が設定されていません。")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        return

    print("=" * 50)
    print("OD Research Dashboard 自動更新スクリプト")
    print("=" * 50)

    print("\n1. 論文を収集中...")
    raw_papers = collect_papers()

    if not raw_papers:
        print("論文を取得できませんでした。ネットワーク環境を確認してください。")
        return

    print(f"\n2. Claude AIで日本語要約・OD示唆を生成中 ({min(len(raw_papers), MAX_PAPERS)}件)...")
    processed = process_papers(raw_papers)

    print(f"\n3. HTML生成中...")
    html = generate_html(processed)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ 完了！ {OUTPUT_FILE} を更新しました（{len(processed)}件）")
    print("   GitHubにpushしてGitHub Pagesを更新してください。")


if __name__ == "__main__":
    main()
