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
        "version": "v9-starttime-dim",
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
    return {"ok": True, "version": "v9-starttime-dim"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
