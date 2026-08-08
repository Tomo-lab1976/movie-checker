from flask import Flask, request, jsonify, send_from_directory
from bs4 import BeautifulSoup, Tag
import requests, re
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__, static_folder="static")

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}
TIMEOUT = 20

THEATERS = {
    "toho": {
        "name": "TOHOシネマズ 八千代緑が丘",
        "url": "https://eiga.com/theater/12/120106/3153/",
    },
    "aeon": {
        "name": "イオンシネマ幕張新都心",
        "url": "https://eiga.com/theater/12/120102/3257/",
    },
}

IGNORE_H2 = {
    "上映スケジュール",
    "映画館情報・割引情報",
    "近くの映画館",
    "近くのエリア",
    "都道府県別",
    "劇場情報",
    "アクセス・地図",
}

def fetch(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()

def looks_like_movie_title(title):
    if not title or title in IGNORE_H2:
        return False
    if len(title) > 100:
        return False
    bad_words = ("上映スケジュール", "映画館情報", "割引情報", "近くの映画館",
                 "都道府県別", "アクセス", "地図", "劇場情報")
    return not any(x in title for x in bad_words)

def movie_sections(html):
    """
    作品の h2 から次の h2 直前までを作品ブロックとして読む。
    /movie/ リンクの有無は条件にしない。
    """
    soup = BeautifulSoup(html, "html.parser")

    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()

    h2s = soup.find_all("h2")
    sections = []

    for h2 in h2s:
        title = norm(h2.get_text(" ", strip=True))
        if not looks_like_movie_title(title):
            continue

        # h2の後ろにある要素を、次のh2まで連結
        parts = []
        for el in h2.find_all_next():
            if el is h2:
                continue
            if isinstance(el, Tag) and el.name == "h2":
                break

            # 重複を減らすため、主に小さめのブロックのみ拾う
            if isinstance(el, Tag) and el.name in (
                "div", "p", "li", "span", "time", "strong", "em"
            ):
                txt = norm(el.get_text(" ", strip=True))
                if txt:
                    parts.append(txt)

        text = " ".join(parts)

        # 上映日が存在するものだけ作品として採用
        if re.search(r"\d{1,2}/\d{1,2}\s*[（(][月火水木金土日][）)]", text):
            sections.append((title, text))

    # 同名タイトルの重複除去
    seen = set()
    out = []
    for title, text in sections:
        if title not in seen:
            seen.add(title)
            out.append((title, text))
    return out

def extract_date_block(text, date):
    dt = datetime.strptime(date, "%Y-%m-%d")
    md = f"{dt.month}/{dt.day}"

    # 映画.comでは「8/8 （土）」のように日付と曜日の間に空白が入ることがある
    start_pat = rf"{re.escape(md)}\s*[（(][月火水木金土日][）)]"
    m = re.search(start_pat, text)
    if not m:
        return ""

    rest = text[m.end():]

    # 次の日付が始まる直前までを対象日の上映ブロックとする
    n = re.search(
        r"\d{1,2}/\d{1,2}\s*[（(][月火水木金土日][）)]",
        rest
    )
    if n:
        rest = rest[:n.start()]

    return rest

def extract_showings(block):
    if not block:
        return []

    # 映画.comの表記例:
    #   9:10 12:10 15:00 17:50 20:40 ～23:00
    # 通常は各回の開始時刻のみ。終了時刻が明記される回は「～終了時刻」を保持する。
    token_re = re.compile(
        r"(?<!\d)([0-2]?\d:\d{2})(?:\s*[～〜~-]\s*([0-2]?\d:\d{2}))?(?!\d)"
    )

    out = []
    for m in token_re.finditer(block):
        sh, sm = map(int, m.group(1).split(":"))
        if not (0 <= sh <= 29 and 0 <= sm <= 59):
            continue

        start = f"{sh:02d}:{sm:02d}"
        end = None

        if m.group(2):
            eh, em = map(int, m.group(2).split(":"))
            if 0 <= eh <= 29 and 0 <= em <= 59:
                end = f"{eh:02d}:{em:02d}"

        out.append({"start": start, "end": end})

    return out


def extract_runtime_minutes(text):
    """作品ブロックから上映時間（例: 128分）を取得する。"""
    candidates = [int(x) for x in re.findall(r"(?<!\d)(\d{2,3})\s*分", text)]
    for n in candidates:
        if 40 <= n <= 300:
            return n
    return None

def add_minutes_to_time(start, runtime):
    if not start or not runtime:
        return None
    h, m = map(int, start.split(":"))
    total = h * 60 + m + runtime
    return f"{total // 60:02d}:{total % 60:02d}"

def scrape_theater(key, date):
    html = fetch(THEATERS[key]["url"])
    result = []

    for title, text in movie_sections(html):
        block = extract_date_block(text, date)
        runtime = extract_runtime_minutes(text)
        for showing in extract_showings(block):
            end = showing["end"] or add_minutes_to_time(showing["start"], runtime)
            result.append({
                "title": title,
                "start": showing["start"],
                "end": end,
                "runtime": runtime,
                "end_source": "published" if showing["end"] else ("runtime" if runtime else None),
                "status": None,
            })

    # 重複除去
    seen = set()
    out = []
    for x in result:
        k = (x["title"], x["start"])
        if k not in seen:
            seen.add(k)
            out.append(x)

    return sorted(out, key=lambda x: (x["start"], x["title"]))


OFFICIAL_SEAT_SOURCES = {
    "toho": {
        "name": "TOHOシネマズ 八千代緑が丘",
        "urls": [
            "https://hlo.tohotheater.jp/net/schedule/028/TNPI2000J01.do?frm=mw",
            "https://hlo.tohotheater.jp/net/schedule/028/TNPI2160J01.do?site_cd=028",
        ],
    },
    "aeon": {
        "name": "イオンシネマ幕張新都心",
        "urls": [
            "https://theater.aeoncinema.com/theaters/makuhari/",
        ],
    },
}

def _seat_source_diagnostic(url):
    r = requests.get(
        url,
        headers=UA,
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    text = norm(soup.get_text(" ", strip=True))

    seat_words = [
        "余裕あり",
        "残席あり",
        "残りわずか",
        "残席わずか",
        "残席なし",
        "完売",
        "空席あり",
        "販売終了",
        "販売期間外",
        "◎",
        "○",
        "△",
        "×",
    ]

    time_matches = re.findall(
        r"(?<!\d)([0-2]?\d:\d{2})(?!\d)",
        text
    )

    # 空席に関係しそうな語の前後を少量だけ返す
    snippets = {}
    lower_text = text.lower()
    for word in seat_words:
        pos = lower_text.find(word.lower())
        if pos >= 0:
            snippets[word] = text[max(0, pos-180):pos+len(word)+260]

    # HTML上の class / aria-label / title に seat / vacant / availability らしき語があるか
    attr_hits = []
    patterns = ("seat", "vacan", "avail", "remain", "stock", "sold", "status")
    for tag in soup.find_all(True):
        attrs = " ".join(
            f"{k}={v}" for k, v in tag.attrs.items()
        )
        hay = (tag.name + " " + attrs).lower()
        if any(p in hay for p in patterns):
            sample = norm(tag.get_text(" ", strip=True))
            attr_hits.append({
                "tag": tag.name,
                "attrs": attrs[:300],
                "text": sample[:220],
            })
        if len(attr_hits) >= 25:
            break

    return {
        "requested_url": url,
        "final_url": r.url,
        "status_code": r.status_code,
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "page_title": norm(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "seat_word_counts": {
            w: text.count(w) + html.count(w)
            for w in seat_words
        },
        "sample_times": time_matches[:40],
        "seat_snippets": snippets,
        "attribute_hits": attr_hits,
        "script_count": len(soup.find_all("script")),
        "form_count": len(soup.find_all("form")),
        "link_count": len(soup.find_all("a")),
    }

@app.get("/seat_debug")
def seat_debug():
    result = {
        "version": "v11-seat-endpoint-diagnostic",
        "toho": [],
        "aeon": [],
    }

    for key, meta in OFFICIAL_SEAT_SOURCES.items():
        for url in meta["urls"]:
            try:
                result[key].append(_seat_source_diagnostic(url))
            except Exception as e:
                result[key].append({
                    "requested_url": url,
                    "error": f"{type(e).__name__}: {str(e)[:300]}",
                })

    return jsonify(result)

@app.get("/seat_debug/<key>")
def seat_debug_one(key):
    if key not in OFFICIAL_SEAT_SOURCES:
        return jsonify({"error": "key must be toho or aeon"}), 400

    result = {
        "version": "v11-seat-endpoint-diagnostic",
        "theater": key,
        "sources": [],
    }

    for url in OFFICIAL_SEAT_SOURCES[key]["urls"]:
        try:
            result["sources"].append(_seat_source_diagnostic(url))
        except Exception as e:
            result["sources"].append({
                "requested_url": url,
                "error": f"{type(e).__name__}: {str(e)[:300]}",
            })

    return jsonify(result)



from urllib.parse import urljoin

def _endpoint_candidates(url):
    r = requests.get(
        url,
        headers=UA,
        timeout=TIMEOUT,
        allow_redirects=True,
    )
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    keywords = [
        "schedule", "seat", "vacan", "avail", "remain", "stock",
        "ticket", "vit", "reserve", "reservation", "ajax", "api",
        "screening", "showtime", "performance", "purchase"
    ]

    script_srcs = []
    for s in soup.find_all("script", src=True):
        src = urljoin(r.url, s.get("src"))
        if src not in script_srcs:
            script_srcs.append(src)

    inline_candidates = []
    inline_snippets = []
    url_re = re.compile(r'["\']((?:https?:)?//[^"\' ]+|/[A-Za-z0-9_./?=&%:-]+)["\']')

    for s in soup.find_all("script"):
        body = s.string or s.get_text(" ", strip=True) or ""
        low = body.lower()

        if any(k in low for k in keywords):
            inline_snippets.append(norm(body)[:1600])

        for m in url_re.finditer(body):
            cand = m.group(1)
            lowc = cand.lower()
            if any(k in lowc for k in keywords):
                full = urljoin(r.url, cand)
                if full not in inline_candidates:
                    inline_candidates.append(full)

    html_candidates = []
    attr_snippets = []
    for tag in soup.find_all(True):
        attrs = tag.attrs or {}
        for k, v in attrs.items():
            vals = v if isinstance(v, list) else [v]
            for val in vals:
                sval = str(val)
                low = sval.lower()
                if any(key in low for key in keywords):
                    attr_snippets.append({
                        "tag": tag.name,
                        "attr": k,
                        "value": sval[:500],
                        "text": norm(tag.get_text(" ", strip=True))[:220],
                    })
                    if sval.startswith("/") or sval.startswith("http"):
                        full = urljoin(r.url, sval)
                        if full not in html_candidates:
                            html_candidates.append(full)
        if len(attr_snippets) >= 80:
            break

    js_findings = []
    for src in script_srcs[:30]:
        try:
            jr = requests.get(src, headers=UA, timeout=TIMEOUT)
            if jr.status_code != 200:
                continue
            body = jr.text
            low = body.lower()
            if not any(k in low for k in keywords):
                continue

            urls = []
            for m in url_re.finditer(body):
                cand = m.group(1)
                if any(k in cand.lower() for k in keywords):
                    full = urljoin(src, cand)
                    if full not in urls:
                        urls.append(full)
                if len(urls) >= 30:
                    break

            snippets = []
            for key in keywords:
                pos = low.find(key)
                if pos >= 0:
                    snippets.append(body[max(0, pos-300):pos+900])
                if len(snippets) >= 8:
                    break

            js_findings.append({
                "src": src,
                "bytes": len(body.encode("utf-8", errors="ignore")),
                "candidate_urls": urls,
                "snippets": snippets,
            })
        except Exception as e:
            js_findings.append({
                "src": src,
                "error": f"{type(e).__name__}: {str(e)[:200]}",
            })

    return {
        "requested_url": url,
        "final_url": r.url,
        "status_code": r.status_code,
        "script_srcs": script_srcs,
        "inline_url_candidates": inline_candidates,
        "html_url_candidates": html_candidates,
        "attribute_snippets": attr_snippets,
        "inline_script_snippets": inline_snippets[:20],
        "js_findings": js_findings[:20],
    }

@app.get("/seat_debug2/<key>")
def seat_debug2_one(key):
    if key not in OFFICIAL_SEAT_SOURCES:
        return jsonify({"error": "key must be toho or aeon"}), 400

    result = {
        "version": "v11-seat-endpoint-diagnostic",
        "theater": key,
        "sources": [],
    }

    for url in OFFICIAL_SEAT_SOURCES[key]["urls"]:
        try:
            result["sources"].append(_endpoint_candidates(url))
        except Exception as e:
            result["sources"].append({
                "requested_url": url,
                "error": f"{type(e).__name__}: {str(e)[:400]}",
            })

    return jsonify(result)


@app.get("/api/schedule")
def api_schedule():
    date = request.args.get("date") or datetime.now(
        ZoneInfo("Asia/Tokyo")
    ).strftime("%Y-%m-%d")

    result = {
        "date": date,
        "toho": [],
        "aeon": [],
        "warnings": [],
        "source": "映画.com",
        "version": "v11-seat-endpoint-diagnostic",
    }

    for key in ("toho", "aeon"):
        try:
            result[key] = scrape_theater(key, date)
            if not result[key]:
                result["warnings"].append(
                    f"{key}: この日の上映情報を解析できませんでした"
                )
        except Exception as e:
            result["warnings"].append(
                f"{key}: {type(e).__name__}: {str(e)[:160]}"
            )

    result["updated_at"] = datetime.now(
        ZoneInfo("Asia/Tokyo")
    ).strftime("%H:%M")

    return jsonify(result)

@app.get("/debug")
def debug():
    out = {}
    for key in ("toho", "aeon"):
        try:
            html = fetch(THEATERS[key]["url"])
            secs = movie_sections(html)
            out[key] = {
                "sections": len(secs),
                "sample_titles": [x[0] for x in secs[:8]],
                "html_bytes": len(html.encode("utf-8")),
            }
        except Exception as e:
            out[key] = {
                "error": f"{type(e).__name__}: {e}"
            }
    return jsonify(out)

@app.get("/")
def home():
    return send_from_directory("static", "index.html")

@app.get("/health")
def health():
    return {"ok": True, "version": "v11-seat-endpoint-diagnostic", "seat_debug": "/seat_debug", "seat_debug2": "/seat_debug2/toho"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
