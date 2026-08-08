from flask import Flask, request, jsonify, send_from_directory
from bs4 import BeautifulSoup, Tag
import requests, re, time
from datetime import datetime
from zoneinfo import ZoneInfo
app = Flask(__name__, static_folder='static')
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'}
TIMEOUT = 20
THEATERS = {'toho': {'name': 'TOHOシネマズ 八千代緑が丘', 'url': 'https://eiga.com/theater/12/120106/3153/'}, 'aeon': {'name': 'イオンシネマ幕張新都心', 'url': 'https://eiga.com/theater/12/120102/3257/'}}
IGNORE_H2 = {'上映スケジュール', '映画館情報・割引情報', '近くの映画館', '近くのエリア', '都道府県別', '劇場情報', 'アクセス・地図'}

def fetch(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or 'utf-8'
    return r.text

def norm(s):
    return re.sub('\\s+', ' ', s or '').strip()

def looks_like_movie_title(title):
    if not title or title in IGNORE_H2:
        return False
    if len(title) > 100:
        return False
    bad_words = ('上映スケジュール', '映画館情報', '割引情報', '近くの映画館', '都道府県別', 'アクセス', '地図', '劇場情報')
    return not any((x in title for x in bad_words))

def movie_sections(html):
    """
    作品の h2 から次の h2 直前までを作品ブロックとして読む。
    /movie/ リンクの有無は条件にしない。
    """
    soup = BeautifulSoup(html, 'html.parser')
    for bad in soup(['script', 'style', 'noscript']):
        bad.decompose()
    h2s = soup.find_all('h2')
    sections = []
    for h2 in h2s:
        title = norm(h2.get_text(' ', strip=True))
        if not looks_like_movie_title(title):
            continue
        parts = []
        for el in h2.find_all_next():
            if el is h2:
                continue
            if isinstance(el, Tag) and el.name == 'h2':
                break
            if isinstance(el, Tag) and el.name in ('div', 'p', 'li', 'span', 'time', 'strong', 'em'):
                txt = norm(el.get_text(' ', strip=True))
                if txt:
                    parts.append(txt)
        text = ' '.join(parts)
        if re.search('\\d{1,2}/\\d{1,2}\\s*[（(][月火水木金土日][）)]', text):
            sections.append((title, text))
    seen = set()
    out = []
    for title, text in sections:
        if title not in seen:
            seen.add(title)
            out.append((title, text))
    return out

def extract_date_block(text, date):
    dt = datetime.strptime(date, '%Y-%m-%d')
    md = f'{dt.month}/{dt.day}'
    start_pat = f'{re.escape(md)}\\s*[（(][月火水木金土日][）)]'
    m = re.search(start_pat, text)
    if not m:
        return ''
    rest = text[m.end():]
    n = re.search('\\d{1,2}/\\d{1,2}\\s*[（(][月火水木金土日][）)]', rest)
    if n:
        rest = rest[:n.start()]
    return rest

def extract_showings(block):
    if not block:
        return []
    token_re = re.compile('(?<!\\d)([0-2]?\\d:\\d{2})(?:\\s*[～〜~-]\\s*([0-2]?\\d:\\d{2}))?(?!\\d)')
    out = []
    for m in token_re.finditer(block):
        sh, sm = map(int, m.group(1).split(':'))
        if not (0 <= sh <= 29 and 0 <= sm <= 59):
            continue
        start = f'{sh:02d}:{sm:02d}'
        end = None
        if m.group(2):
            eh, em = map(int, m.group(2).split(':'))
            if 0 <= eh <= 29 and 0 <= em <= 59:
                end = f'{eh:02d}:{em:02d}'
        out.append({'start': start, 'end': end})
    return out

def extract_runtime_minutes(text):
    """作品ブロックから上映時間（例: 128分）を取得する。"""
    candidates = [int(x) for x in re.findall('(?<!\\d)(\\d{2,3})\\s*分', text)]
    for n in candidates:
        if 40 <= n <= 300:
            return n
    return None

def add_minutes_to_time(start, runtime):
    if not start or not runtime:
        return None
    h, m = map(int, start.split(':'))
    total = h * 60 + m + runtime
    return f'{total // 60:02d}:{total % 60:02d}'

def scrape_theater(key, date):
    html = fetch(THEATERS[key]['url'])
    result = []
    for title, text in movie_sections(html):
        block = extract_date_block(text, date)
        runtime = extract_runtime_minutes(text)
        for showing in extract_showings(block):
            end = showing['end'] or add_minutes_to_time(showing['start'], runtime)
            result.append({'title': title, 'start': showing['start'], 'end': end, 'runtime': runtime, 'end_source': 'published' if showing['end'] else 'runtime' if runtime else None, 'status': None})
    seen = set()
    out = []
    for x in result:
        k = (x['title'], x['start'])
        if k not in seen:
            seen.add(k)
            out.append(x)
    return sorted(out, key=lambda x: (x['start'], x['title']))
import time
TOHO_STATUS_MAP = {'A': ('◎', '余裕あり'), 'B': ('○', '空席あり'), 'C': ('△', '残りわずか'), 'D': ('×', '満席'), 'G': (None, '販売対象外')}

def _toho_time(t):
    if not t:
        return None
    try:
        h, m = map(int, str(t).split(':')[:2])
        return f'{h:02d}:{m:02d}'
    except Exception:
        return str(t)

def fetch_toho_seat_entries(date):
    compact = datetime.strptime(date, '%Y-%m-%d').strftime('%Y%m%d')
    url = 'https://api2.tohotheater.jp/api/schedule/v2/schedule/028/TNPI3050J05'
    params = {'__type__': 'json', 'vg_cd': '028', 'show_day': compact, 'isMember': 'false', 'enter_kbn': '0', '_dc': str(int(time.time() * 1000))}
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    entries = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get('showingStart') and isinstance(obj.get('unsoldSeatInfo'), dict):
                seat = obj.get('unsoldSeatInfo') or {}
                screen = obj.get('screen') or {}
                code = seat.get('unsoldSeatStatus')
                symbol, label = TOHO_STATUS_MAP.get(code, (None, None))
                entries.append({'start': _toho_time(obj.get('showingStart')), 'end': _toho_time(obj.get('showingEnd')), 'status': symbol, 'status_text': label, 'status_code': code, 'screen': screen.get('name'), 'seat_count': screen.get('allSeatNum')})
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
        by_start.setdefault(e['start'], []).append(e)
    matched = 0
    for x in showings:
        candidates = by_start.get(x.get('start'), [])
        if not candidates:
            continue
        chosen = None
        if x.get('end'):
            exact = [e for e in candidates if e.get('end') == x.get('end')]
            if len(exact) == 1:
                chosen = exact[0]
        if chosen is None and len(candidates) == 1:
            chosen = candidates[0]
        if chosen is None and x.get('end'):

            def mins(t):
                try:
                    h, m = map(int, t.split(':'))
                    return h * 60 + m
                except Exception:
                    return None
            target = mins(x['end'])
            ranked = []
            if target is not None:
                for e in candidates:
                    em = mins(e.get('end') or '')
                    if em is not None:
                        ranked.append((abs(em - target), e))
                ranked.sort(key=lambda z: z[0])
                if ranked and (len(ranked) == 1 or ranked[0][0] < ranked[1][0]):
                    chosen = ranked[0][1]
        if chosen is None:
            continue
        x['status'] = chosen.get('status')
        x['status_text'] = chosen.get('status_text')
        x['status_code'] = chosen.get('status_code')
        x['screen'] = chosen.get('screen')
        x['seat_count'] = chosen.get('seat_count')
        if chosen.get('end'):
            x['end'] = chosen['end']
        matched += 1
    return {'matched': matched, 'official_entries': len(entries)}
AEON_STATUS_LABELS = {'○': '空席あり', '△': '残りわずか', '×': '満席'}

def _parse_aeon_iso_to_jst_hm(value):
    if not value:
        return None
    try:
        s = str(value)
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        dt = dt.astimezone(ZoneInfo('Asia/Tokyo'))
        return dt.strftime('%H:%M')
    except Exception:
        return None

def _parse_aeon_iso_to_jst_date(value):
    if not value:
        return None
    try:
        s = str(value)
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        dt = dt.astimezone(ZoneInfo('Asia/Tokyo'))
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return None

def _aeon_status(maximum, remaining):
    try:
        maximum = int(maximum)
        remaining = int(remaining)
    except Exception:
        return (None, None)
    if maximum <= 0:
        return (None, None)
    if remaining <= 0:
        return ('×', '満席')
    ratio = remaining / maximum
    if ratio < 0.3:
        return ('△', '残りわずか')
    return ('○', '空席あり')

def fetch_aeon_seat_entries(date):
    """
    イオンシネマ幕張新都心 公式schedule.jsonから上映回を取得。
    公式JSと同じデータを使い、remainingAttendeeCapacity /
    maximumAttendeeCapacity から ○△× を判定する。
    """
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    stamp = now.strftime('%Y%m%d%H%M')
    url = f'https://theater.aeoncinema.com/schedule/v2/data/makuhari/schedule.json?v={stamp}'
    headers = dict(UA)
    headers.update({'Referer': 'https://theater.aeoncinema.com/theaters/makuhari/', 'Accept': 'application/json,text/plain,*/*'})
    r = requests.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    data = r.json()
    entries = []

    def walk(obj):
        if isinstance(obj, dict):
            if obj.get('startDate') and obj.get('endDate') and (obj.get('remainingAttendeeCapacity') is not None or obj.get('maximumAttendeeCapacity') is not None):
                show_date = _parse_aeon_iso_to_jst_date(obj.get('startDate'))
                if show_date == date:
                    super_event = obj.get('superEvent') or {}
                    location = obj.get('location') or {}
                    movie = None
                    nm = super_event.get('name') if isinstance(super_event, dict) else None
                    if isinstance(nm, dict):
                        movie = nm.get('ja') or nm.get('en')
                    elif nm:
                        movie = str(nm)
                    screen = None
                    lnm = location.get('name') if isinstance(location, dict) else None
                    if isinstance(lnm, dict):
                        screen = lnm.get('ja') or lnm.get('en')
                    elif lnm:
                        screen = str(lnm)
                    max_cap = obj.get('maximumAttendeeCapacity')
                    remain_cap = obj.get('remainingAttendeeCapacity')
                    symbol, label = _aeon_status(max_cap, remain_cap)
                    entries.append({'title': movie, 'start': _parse_aeon_iso_to_jst_hm(obj.get('startDate')), 'end': _parse_aeon_iso_to_jst_hm(obj.get('endDate')), 'status': symbol, 'status_text': label, 'screen': screen, 'seat_count': max_cap, 'remaining_seats': remain_cap, 'official_id': obj.get('id')})
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)
    walk(data)
    return entries

def _norm_title_loose(s):
    if not s:
        return ''
    s = str(s)
    s = re.sub('\\s+', '', s)
    s = s.replace('：', ':').replace('・', '').replace('\u3000', '')
    s = s.lower()
    return s

def merge_aeon_seat_status(showings, date):
    """
    映画.comの表示用上映情報にイオン公式空席データを重ねる。
    主照合は開始時刻。複数候補時はタイトルと終了時刻を補助に使う。
    """
    entries = fetch_aeon_seat_entries(date)
    by_start = {}
    for e in entries:
        by_start.setdefault(e.get('start'), []).append(e)
    matched = 0
    for x in showings:
        candidates = by_start.get(x.get('start'), [])
        if not candidates:
            continue
        chosen = None
        xt = _norm_title_loose(x.get('title'))
        title_matches = [e for e in candidates if xt and (xt in _norm_title_loose(e.get('title')) or _norm_title_loose(e.get('title')) in xt)]
        if len(title_matches) == 1:
            chosen = title_matches[0]
        if chosen is None and x.get('end'):
            end_matches = [e for e in candidates if e.get('end') == x.get('end')]
            if len(end_matches) == 1:
                chosen = end_matches[0]
        if chosen is None and len(candidates) == 1:
            chosen = candidates[0]
        if chosen is None:
            continue
        x['status'] = chosen.get('status')
        x['status_text'] = chosen.get('status_text')
        x['screen'] = chosen.get('screen')
        x['seat_count'] = chosen.get('seat_count')
        x['remaining_seats'] = chosen.get('remaining_seats')
        x['seat_source'] = 'AEON公式'
        if chosen.get('end'):
            x['end'] = chosen['end']
        matched += 1
    return {'matched': matched, 'official_entries': len(entries)}

@app.get('/api/schedule')
def api_schedule():
    date = request.args.get('date') or datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%Y-%m-%d')
    result = {'date': date, 'toho': [], 'aeon': [], 'warnings': [], 'source': '映画.com', 'version': 'v1.0-clean'}
    for key in ('toho', 'aeon'):
        try:
            result[key] = scrape_theater(key, date)
            if not result[key]:
                result['warnings'].append(f'{key}: この日の上映情報を解析できませんでした')
        except Exception as e:
            result['warnings'].append(f'{key}: {type(e).__name__}: {str(e)[:160]}')
    result['seat_sources'] = {'toho': 'TOHO公式API', 'aeon': 'AEON公式schedule.json'}
    try:
        merge_info = merge_toho_seat_status(result['toho'], date)
        result['toho_seat_merge'] = merge_info
    except Exception as e:
        result['warnings'].append(f'toho seats: {type(e).__name__}: {str(e)[:180]}')
    try:
        merge_info = merge_aeon_seat_status(result['aeon'], date)
        result['aeon_seat_merge'] = merge_info
    except Exception as e:
        result['warnings'].append(f'aeon seats: {type(e).__name__}: {str(e)[:180]}')
    result['updated_at'] = datetime.now(ZoneInfo('Asia/Tokyo')).strftime('%H:%M')
    return jsonify(result)

@app.get('/')
def home():
    return send_from_directory('static', 'index.html')

@app.get('/health')
def health():
    return {'ok': True, 'version': 'v1.0-clean'}
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
