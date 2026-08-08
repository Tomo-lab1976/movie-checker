from flask import Flask, request, jsonify, send_from_directory
from bs4 import BeautifulSoup
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

SKIP_TITLES = {
    "上映スケジュール",
    "映画館情報・割引情報",
    "近くの映画館",
    "近くのエリア",
    "都道府県別",
    "劇場情報",
}

def fetch(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def normalize(s):
    return re.sub(r"\s+", " ", s).strip()

def get_movie_sections(html):
    """
   映画.com の劇場ページから、
    h2見出しを映画タイトル候補として、次のh2までの本文を切り出す。
    """
    soup = BeautifulSoup(html, "html.parser")

    # script/styleを除去
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    body_text = soup.get_text("\n", strip=True)
    body_text = body_text.replace("\xa0", " ")

    headings = []
    for h in soup.find_all("h2"):
        title = normalize(h.get_text(" ", strip=True))
        if not title or title in SKIP_TITLES:
            continue
        if "上映" in title and "作品" not in title:
            continue
        pos = body_text.find(title)
        if pos >= 0:
            headings.append((pos, title))

    headings = sorted(set(headings))
    sections = []
    for i, (pos, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(body_text)
        chunk = body_text[pos:end]

        # 実際に上映日らしき文字列があるものだけ映画として扱う
        if re.search(r"\d{1,2}/\d{1,2}[（(][月火水木金土日][）)]", chunk):
            sections.append((title, chunk))
    return sections

def extract_for_date(chunk, date):
    """
    例:
      8/8（土） 9:20 11:45 14:10 16:25 18:40 20:50～22:35 |
    から対象日の開始時刻を抽出。
    """
    dt = datetime.strptime(date, "%Y-%m-%d")
    md = f"{dt.month}/{dt.day}"

    # 曜日表記を含む日付から、次の日付 or 区切りまでを取得
    pat = re.compile(
        rf"{re.escape(md)}[（(][月火水木金土日][）)]\s*(.*?)(?="
        r"\s*\|\s*\d{1,2}/\d{1,2}[（(][月火水木金土日][）)]"
        r"|\s*\d{1,2}/\d{1,2}[（(][月火水木金土日][）)]"
        r"|$)"
    )
    m = pat.search(chunk)
    if not m:
        return []

    block = m.group(1)

    # 開始時刻。20:50～22:35 は 20:50 を開始として扱う。
    times = re.findall(r"(?<!\d)([0-2]?\d:\d{2})(?!\d)", block)

    # 終了時刻を重複して拾うのを避ける
    # 「A～B」のBを除く
    ends = set(re.findall(r"[～〜~-]\s*([0-2]?\d:\d{2})", block))
    starts = []
    for t in times:
        t = t.zfill(5)
        if t in ends:
            continue
        if t not in starts:
            starts.append(t)
    return starts

def scrape_theater(key, date):
    meta = THEATERS[key]
    html = fetch(meta["url"])
    result = []

    for title, chunk in get_movie_sections(html):
        starts = extract_for_date(chunk, date)
        for start in starts:
            result.append({
                "title": title,
                "start": start,
                "end": None,
                "status": None,
            })

    # 同一作品・同一時刻の重複除去
    seen = set()
    dedup = []
    for x in result:
        k = (x["title"], x["start"])
        if k not in seen:
            seen.add(k)
            dedup.append(x)

    return sorted(dedup, key=lambda x: (x["start"], x["title"]))

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
    }

    for key in ("toho", "aeon"):
        try:
            result[key] = scrape_theater(key, date)
            if not result[key]:
                result["warnings"].append(
                    f"{key}: この日の上映情報を取得できませんでした"
                )
        except Exception as e:
            result["warnings"].append(
                f"{key}: {type(e).__name__}: {str(e)[:120]}"
            )

    result["updated_at"] = datetime.now(
        ZoneInfo("Asia/Tokyo")
    ).strftime("%H:%M")

    return jsonify(result)

@app.get("/")
def home():
    return send_from_directory("static", "index.html")

@app.get("/health")
def health():
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
