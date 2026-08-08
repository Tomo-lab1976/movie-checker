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

def fetch(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    return r.text

def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()

def movie_sections(html):
    """
    h2 の直後から次の h2 までを、その作品のスケジュール領域として読む。
    ページ全体の find() は使わないので、同じ作品名が別場所に出ても影響しにくい。
    """
    soup = BeautifulSoup(html, "html.parser")

    for bad in soup(["script", "style", "noscript"]):
        bad.decompose()

    sections = []

    for h2 in soup.find_all("h2"):
        title = norm(h2.get_text(" ", strip=True))
        if not title:
            continue

        # 映画タイトルの h2 には、多くの場合 /movie/ へのリンクが含まれる。
        movie_link = h2.find("a", href=re.compile(r"/movie/"))
        if not movie_link:
            continue

        title = norm(movie_link.get_text(" ", strip=True)) or title

        chunks = []
        node = h2.next_sibling

        while node is not None:
            # 次の h2 に来たら、その作品の領域は終了
            if isinstance(node, Tag) and node.name == "h2":
                break

            if isinstance(node, Tag):
                txt = norm(node.get_text(" ", strip=True))
            else:
                txt = norm(str(node))

            if txt:
                chunks.append(txt)

            node = node.next_sibling

        text = " ".join(chunks)

        # 上映日がある領域だけ採用
        if re.search(r"\d{1,2}/\d{1,2}[（(][月火水木金土日][）)]", text):
            sections.append((title, text))

    return sections

def date_block(text, date):
    dt = datetime.strptime(date, "%Y-%m-%d")
    md = f"{dt.month}/{dt.day}"

    # 対象日から、次の日付までを切り出す
    m = re.search(
        rf"{re.escape(md)}[（(][月火水木金土日][）)]\s*(.*?)"
        rf"(?=\s*\|\s*\d{{1,2}}/\d{{1,2}}[（(][月火水木金土日][）)]"
        rf"|\s+\d{{1,2}}/\d{{1,2}}[（(][月火水木金土日][）)]"
        rf"|$)",
        text
    )
    return m.group(1) if m else ""

def start_times(block):
    if not block:
        return []

    # 20:50～22:35 のような表記は開始時刻 20:50 だけ残す
    cleaned = re.sub(
        r"([0-2]?\d:\d{2})\s*[～〜~-]\s*([0-2]?\d:\d{2})",
        r"\1",
        block
    )

    times = re.findall(r"(?<!\d)([0-2]?\d:\d{2})(?!\d)", cleaned)

    out = []
    for t in times:
        h, m = t.split(":")
        hh = int(h)
        mm = int(m)
        if 0 <= hh <= 29 and 0 <= mm <= 59:
            t2 = f"{hh:02d}:{mm:02d}"
            if t2 not in out:
                out.append(t2)
    return out

def scrape_theater(key, date):
    html = fetch(THEATERS[key]["url"])
    result = []

    for title, text in movie_sections(html):
        block = date_block(text, date)
        for start in start_times(block):
            result.append({
                "title": title,
                "start": start,
                "end": None,
                "status": None,
            })

    # 重複除去
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
    """
    次回のトラブル時に、各館で何作品の見出しを拾えているか確認する診断用。
    """
    out = {}
    for key in ("toho", "aeon"):
        try:
            html = fetch(THEATERS[key]["url"])
            secs = movie_sections(html)
            out[key] = {
                "sections": len(secs),
                "sample_titles": [x[0] for x in secs[:5]],
            }
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    return jsonify(out)

@app.get("/")
def home():
    return send_from_directory("static", "index.html")

@app.get("/health")
def health():
    return {"ok": True}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
