from flask import Flask, jsonify, send_from_directory
import requests
from bs4 import BeautifulSoup
import re

app = Flask(__name__, static_folder="static")

THEATERS = {
    "toho": "https://eiga.com/theater/12/120106/3153/",
    "aeon": "https://eiga.com/theater/12/120102/3257/",
}

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    )
}

def norm(s):
    return re.sub(r"\s+", " ", s or "").strip()

def diagnose(key):
    url = THEATERS[key]
    r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"

    html = r.text
    soup = BeautifulSoup(html, "html.parser")
    text = norm(soup.get_text(" ", strip=True))

    needles = [
        "8/8",
        "上映スケジュール",
        "作品情報を見る",
        "ブルーロック",
        "キングダム",
        "__NEXT_DATA__",
        "__NUXT__",
        "captcha",
        "Access Denied",
    ]

    return {
        "version": "v5-diagnostic",
        "requested_url": url,
        "final_url": r.url,
        "status_code": r.status_code,
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "page_title": norm(soup.title.get_text(" ", strip=True)) if soup.title else None,
        "tag_counts": {
            "h1": len(soup.find_all("h1")),
            "h2": len(soup.find_all("h2")),
            "h3": len(soup.find_all("h3")),
            "script": len(soup.find_all("script")),
            "a": len(soup.find_all("a")),
        },
        "contains": {
            n: {
                "html": n.lower() in html.lower(),
                "text": n.lower() in text.lower(),
                "count_html": html.lower().count(n.lower()),
            }
            for n in needles
        },
        "h1": [norm(x.get_text(" ", strip=True)) for x in soup.find_all("h1")[:10]],
        "h2": [norm(x.get_text(" ", strip=True)) for x in soup.find_all("h2")[:20]],
        "h3": [norm(x.get_text(" ", strip=True)) for x in soup.find_all("h3")[:20]],
        "text_prefix": text[:2000],
    }

@app.get("/")
def home():
    return send_from_directory("static", "index.html")

@app.get("/health")
def health():
    return {"ok": True, "version": "v5-diagnostic"}

@app.get("/debug_raw/toho")
def debug_toho():
    return jsonify(diagnose("toho"))

@app.get("/debug_raw/aeon")
def debug_aeon():
    return jsonify(diagnose("aeon"))

@app.get("/api/schedule")
def api_schedule():
    return jsonify({
        "toho": [],
        "aeon": [],
        "warnings": ["v5診断版です。/debug_raw/toho を確認してください。"],
        "version": "v5-diagnostic"
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
