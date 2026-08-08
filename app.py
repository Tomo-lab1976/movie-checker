from flask import Flask, request, jsonify, send_from_directory
from bs4 import BeautifulSoup
import requests, re
from datetime import datetime
from zoneinfo import ZoneInfo

app=Flask(__name__, static_folder="static")
UA={"User-Agent":"Mozilla/5.0 (compatible; personal-movie-checker/1.0)"}
TIMEOUT=20

def clean_lines(html):
    soup=BeautifulSoup(html,"html.parser")
    return [re.sub(r"\s+"," ",x).strip() for x in soup.get_text("\n").splitlines() if x.strip()]

def fetch(url):
    r=requests.get(url,headers=UA,timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def parse_generic(lines):
    """
    Conservative fallback parser.
    Finds time pairs near a preceding text line that looks like a movie title.
    This is intentionally easy to adjust if a cinema changes its HTML.
    """
    out=[]
    skip=re.compile(r"(上映スケジュール|上映時間|スクリーン|チケット|販売|劇場|料金|会員|ログイン|メニュー|お知らせ)")
    title=None
    for line in lines:
        tm=re.search(r"(?<!\d)([0-2]?\d:\d{2})\s*[~〜\-]\s*([0-2]?\d:\d{2})(?!\d)",line)
        if tm and title:
            out.append({"title":title,"start":tm.group(1).zfill(5),"end":tm.group(2).zfill(5),"status":None})
            continue
        single=re.fullmatch(r"([0-2]?\d:\d{2})",line)
        if single and title:
            out.append({"title":title,"start":single.group(1).zfill(5),"end":None,"status":None})
            continue
        if len(line)<=90 and not skip.search(line) and not re.fullmatch(r"[\d:〜~\-（）()年月日/・ ]+",line):
            title=line
    # de-duplicate
    seen=set(); dedup=[]
    for x in out:
        k=(x["title"],x["start"],x["end"])
        if k not in seen:
            seen.add(k); dedup.append(x)
    return dedup

def toho(date):
    # TOHO Cinemas Yachiyo Midorigaoka, theater code 028
    url="https://hlo.tohotheater.jp/net/schedule/028/TNPI2000J01.do?frm=mw"
    # Main schedule page generally carries the currently published week.
    lines=clean_lines(fetch(url))
    return parse_generic(lines)

def aeon(date):
    compact=date.replace("-","")
    url=f"https://theater.aeoncinema.com/theaters/makuhari/?date={compact}"
    lines=clean_lines(fetch(url))
    return parse_generic(lines)

@app.get("/api/schedule")
def api_schedule():
    date=request.args.get("date") or datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d")
    result={"date":date,"toho":[],"aeon":[],"warnings":[]}
    for name,fn in (("toho",toho),("aeon",aeon)):
        try:
            result[name]=fn(date)
        except Exception as e:
            result["warnings"].append(f"{name}: {type(e).__name__}")
    result["updated_at"]=datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%H:%M")
    return jsonify(result)

@app.get("/")
def home():
    return send_from_directory("static","index.html")

@app.get("/health")
def health():
    return {"ok":True}

if __name__=="__main__":
    app.run(host="0.0.0.0",port=8000)
