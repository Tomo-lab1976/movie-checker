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
        "version": "v13-toho-seats",
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
        "version": "v13-toho-seats",
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
        "version": "v13-toho-seats",
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



import time

def _summarize_response(url, params=None):
    try:
        r = requests.get(
            url,
            params=params,
            headers=UA,
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        content_type = r.headers.get("content-type", "")
        text = r.text

        info = {
            "url": r.url,
            "status_code": r.status_code,
            "content_type": content_type,
            "bytes": len(r.content),
            "text_prefix": text[:700],
        }

        # JSONなら構造と残席関連語を探索
        if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
            try:
                data = r.json()
                info["json_type"] = type(data).__name__

                hits = []
                def walk(obj, path="$", depth=0):
                    if depth > 12 or len(hits) >= 100:
                        return
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            kl = str(k).lower()
                            if any(word in kl for word in [
                                "seat", "remain", "attendee", "capacity",
                                "vacan", "status", "showingstart", "showingend",
                                "movie", "screen"
                            ]):
                                hits.append({
                                    "path": f"{path}.{k}",
                                    "value": str(v)[:500]
                                })
                            walk(v, f"{path}.{k}", depth+1)
                    elif isinstance(obj, list):
                        for i, v in enumerate(obj[:100]):
                            walk(v, f"{path}[{i}]", depth+1)

                walk(data)
                info["interesting_json_hits"] = hits[:100]

                # 81070 / makuhari の出現箇所も探す
                target_hits = []
                def find_target(obj, path="$", depth=0):
                    if depth > 12 or len(target_hits) >= 50:
                        return
                    if isinstance(obj, dict):
                        for k, v in obj.items():
                            s = str(v)
                            if "81070" in s or "makuhari" in s.lower():
                                target_hits.append({
                                    "path": f"{path}.{k}",
                                    "value": s[:700]
                                })
                            find_target(v, f"{path}.{k}", depth+1)
                    elif isinstance(obj, list):
                        for i, v in enumerate(obj[:300]):
                            find_target(v, f"{path}[{i}]", depth+1)
                    else:
                        s = str(obj)
                        if "81070" in s or "makuhari" in s.lower():
                            target_hits.append({"path": path, "value": s[:700]})

                find_target(data)
                info["target_81070_makuhari_hits"] = target_hits[:50]
            except Exception as e:
                info["json_error"] = f"{type(e).__name__}: {str(e)[:200]}"

        # HTMLでも残席語を探索
        low = text.lower()
        keywords = [
            "unsoldseatstatus", "unsoldseatinfo",
            "remainingattendeecapacity", "maximumattendeecapacity",
            "remainingseats", "vacant", "soldout",
            "showingstart", "showingend"
        ]
        info["keyword_counts"] = {k: low.count(k) for k in keywords}

        return info
    except Exception as e:
        return {
            "url": url,
            "error": f"{type(e).__name__}: {str(e)[:300]}"
        }

@app.get("/seat_data_debug/toho")
def seat_data_debug_toho():
    date = request.args.get("date") or datetime.now(
        ZoneInfo("Asia/Tokyo")
    ).strftime("%Y%m%d")

    now_ms = str(int(time.time() * 1000))
    base_url = "https://api2.tohotheater.jp/api/schedule/v2/schedule/028/TNPI3050J05"

    variants = [
        {
            "__type__": "html",
            "vg_cd": "028",
            "show_day": date,
            "isMember": "false",
            "enter_kbn": "0",
            "_dc": now_ms,
        },
        {
            "__type__": "html",
            "vg_cd": "028",
            "show_day": date,
            "isMember": "false",
            "enter_kbn": "",
            "_dc": now_ms,
        },
        {
            "__type__": "json",
            "vg_cd": "028",
            "show_day": date,
            "isMember": "false",
            "enter_kbn": "0",
            "_dc": now_ms,
        },
    ]

    return jsonify({
        "version": "v13-toho-seats",
        "theater": "toho",
        "date": date,
        "results": [
            _summarize_response(base_url, p)
            for p in variants
        ]
    })

@app.get("/seat_data_debug/aeon")
def seat_data_debug_aeon():
    date_compact = request.args.get("date") or datetime.now(
        ZoneInfo("Asia/Tokyo")
    ).strftime("%Y%m%d")

    dt = datetime.strptime(date_compact, "%Y%m%d")
    date_dash = dt.strftime("%Y-%m-%d")
    stamp = str(int(time.time()))

    base = "https://theater.aeoncinema.com"

    # JSで見つかった公式データパスと、その命名規則として可能性の高い候補。
    urls = [
        f"{base}/schedule.json?v={stamp}",
        f"{base}/schedule/data/theaters.json?v={stamp}",
        f"{base}/schedule/v2/data/__master/movies.json?v={stamp}",
        f"{base}/schedule/v2/data/81070.json?v={stamp}",
        f"{base}/schedule/v2/data/81070/{date_compact}.json?v={stamp}",
        f"{base}/schedule/v2/data/81070/{date_dash}.json?v={stamp}",
        f"{base}/schedule/v2/data/makuhari.json?v={stamp}",
        f"{base}/schedule/v2/data/makuhari/{date_compact}.json?v={stamp}",
        f"{base}/schedule/data/__aeon/81070.json?v={stamp}",
        f"{base}/schedule/data/__aeon/81070/{date_compact}.json?v={stamp}",
    ]

    return jsonify({
        "version": "v13-toho-seats",
        "theater": "aeon",
        "date": date_compact,
        "results": [
            _summarize_response(url)
            for url in urls
        ]
    })



TOHO_STATUS_MAP = {
    "A": ("◎", "余裕あり"),
    "B": ("○", "空席あり"),
    "C": ("△", "残りわずか"),
    "D": ("×", "満席"),
    # G は販売期間外・販売対象外など。混雑表示としては出さない。
    "G": (None, "販売対象外"),
}

def _toho_time(t):
    if not t:
        return None
    try:
        h, m = map(int, str(t).split(":")[:2])
        return f"{h:02d}:{m:02d}"
    except Exception:
        return str(t)

def fetch_toho_seat_entries(date):
    compact = datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%d")
    url = "https://api2.tohotheater.jp/api/schedule/v2/schedule/028/TNPI3050J05"
    params = {
        "__type__": "json",
        "vg_cd": "028",
        "show_day": compact,
        "isMember": "false",
        "enter_kbn": "0",
        "_dc": str(int(time.time() * 1000)),
    }

    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()

    entries = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get("showingStart") and isinstance(obj.get("unsoldSeatInfo"), dict):
                seat = obj.get("unsoldSeatInfo") or {}
                screen = obj.get("screen") or {}
                code = seat.get("unsoldSeatStatus")
                symbol, label = TOHO_STATUS_MAP.get(code, (None, None))
                entries.append({
                    "start": _toho_time(obj.get("showingStart")),
                    "end": _toho_time(obj.get("showingEnd")),
                    "status": symbol,
                    "status_text": label,
                    "status_code": code,
                    "screen": screen.get("name"),
                    "seat_count": screen.get("allSeatNum"),
                })
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return entries

def merge_toho_seat_status(showings, date):
    """
    映画.comで取得した表示用上映情報に、TOHO公式APIの空席状況を重ねる。
    タイトルの表記差を避けるため、開始時刻＋終了時刻で照合する。
    """
    entries = fetch_toho_seat_entries(date)

    by_start = {}
    for e in entries:
        by_start.setdefault(e["start"], []).append(e)

    matched = 0
    for x in showings:
        candidates = by_start.get(x.get("start"), [])
        if not candidates:
            continue

        chosen = None

        # まず開始＋終了の完全一致
        if x.get("end"):
            exact = [e for e in candidates if e.get("end") == x.get("end")]
            if len(exact) == 1:
                chosen = exact[0]

        # 同じ開始時刻が1件だけなら確定
        if chosen is None and len(candidates) == 1:
            chosen = candidates[0]

        # 終了時刻が多少ずれていても最も近いものが一意なら採用
        if chosen is None and x.get("end"):
            def mins(t):
                try:
                    h, m = map(int, t.split(":"))
                    return h * 60 + m
                except Exception:
                    return None
            target = mins(x["end"])
            ranked = []
            if target is not None:
                for e in candidates:
                    em = mins(e.get("end") or "")
                    if em is not None:
                        ranked.append((abs(em-target), e))
                ranked.sort(key=lambda z: z[0])
                if ranked and (len(ranked) == 1 or ranked[0][0] < ranked[1][0]):
                    chosen = ranked[0][1]

        if chosen is None:
            continue

        x["status"] = chosen.get("status")
        x["status_text"] = chosen.get("status_text")
        x["status_code"] = chosen.get("status_code")
        x["screen"] = chosen.get("screen")
        x["seat_count"] = chosen.get("seat_count")

        # 終了時刻はTOHO公式の値を優先
        if chosen.get("end"):
            x["end"] = chosen["end"]

        matched += 1

    return {
        "matched": matched,
        "official_entries": len(entries),
    }


def _aeon_bundle_url():
    r = requests.get(
        "https://theater.aeoncinema.com/theaters/makuhari/",
        headers=UA, timeout=TIMEOUT, allow_redirects=True
    )
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    for s in soup.find_all("script", src=True):
        src = s.get("src")
        if src and "theaters_makuhari" in src and "bundle.js" in src:
            from urllib.parse import urljoin
            return urljoin(r.url, src)
    return None

@app.get("/aeon_path_debug")
def aeon_path_debug():
    bundle_url = _aeon_bundle_url()
    if not bundle_url:
        return jsonify({
            "version": "v13-toho-seats",
            "error": "幕張新都心のbundle.jsを見つけられませんでした"
        }), 500

    r = requests.get(bundle_url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    text = r.text

    needles = [
        "schedule/v2/data/",
        "schedule/data/__aeon/",
        "remainingAttendeeCapacity",
        "maximumAttendeeCapacity",
        "remainingseats",
    ]

    excerpts = {}
    for needle in needles:
        hits = []
        start = 0
        low = text.lower()
        nl = needle.lower()
        while len(hits) < 12:
            pos = low.find(nl, start)
            if pos < 0:
                break
            hits.append(text[max(0, pos-900):min(len(text), pos+len(needle)+1400)])
            start = pos + len(needle)
        excerpts[needle] = hits

    # スケジュール関連の文字列リテラルも抽出
    literal_re = re.compile(r'["\']([^"\']*schedule[^"\']*)["\']', re.I)
    literals = []
    for m in literal_re.finditer(text):
        val = m.group(1)
        if val not in literals:
            literals.append(val)
        if len(literals) >= 150:
            break

    return jsonify({
        "version": "v13-toho-seats",
        "bundle_url": bundle_url,
        "bundle_bytes": len(r.content),
        "excerpts": excerpts,
        "schedule_literals": literals,
    })


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
        "version": "v13-toho-seats",
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

    # TOHOのみ、公式APIの空席状況を本番表示へ反映
    result["seat_sources"] = {"toho": "TOHO公式API", "aeon": None}
    try:
        merge_info = merge_toho_seat_status(result["toho"], date)
        result["toho_seat_merge"] = merge_info
    except Exception as e:
        result["warnings"].append(
            f"toho seats: {type(e).__name__}: {str(e)[:180]}"
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
    return {"ok": True, "version": "v13-toho-seats", "seat_debug": "/seat_debug", "seat_debug2": "/seat_debug2/toho", "seat_data_debug": "/seat_data_debug/toho"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
