# -*- coding: utf-8 -*-
"""
宏观经济 / 汇率 / 资本流向 / 公司控股 数据模块
数据源（全部公开免费，国内可直连）：
  · 世界银行 API   api.worldbank.org        宏观统计（年度）
  · open.er-api    open.er-api.com          实时汇率
  · Wikidata       query.wikidata.org       公司控股关系
"""
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CST = timezone(timedelta(hours=8))
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0'}
# 世界银行 / Wikidata 都直连（无需代理）
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPSHandler(context=CTX))

LOCK = threading.Lock()
CACHE = {}


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with DIRECT.open(req, timeout=timeout) as r:
        return r.read()


def _getj(url, timeout=25):
    return json.loads(_get(url, timeout).decode('utf-8', 'ignore'))


def _cache(key, ttl, fn):
    """内存 + 磁盘双层缓存"""
    now = time.time()
    with LOCK:
        c = CACHE.get(key)
        if c and now - c['ts'] < ttl:
            return c['v']
    path = os.path.join(HERE, 'cache_%s.json' % key)
    try:
        if os.path.exists(path) and now - os.path.getmtime(path) < ttl:
            with open(path, encoding='utf-8') as f:
                v = json.load(f)
            with LOCK:
                CACHE[key] = {'v': v, 'ts': now}
            return v
    except Exception:
        pass
    v = fn()
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(v, f, ensure_ascii=False)
    except Exception:
        pass
    with LOCK:
        CACHE[key] = {'v': v, 'ts': now}
    return v


# ---------------- 世界银行宏观指标 ----------------
WB_INDICATORS = [
    ('gdp',       'NY.GDP.MKTP.CD',      'GDP 总量',        '美元'),
    ('gdp_growth', 'NY.GDP.MKTP.KD.ZG',  'GDP 增速',        '%'),
    ('gdp_pc',    'NY.GDP.PCAP.CD',      '人均 GDP',        '美元'),
    ('cpi',       'FP.CPI.TOTL.ZG',      '通胀率 CPI',      '%'),
    ('unemp',     'SL.UEM.TOTL.ZS',      '失业率',          '%'),
    ('exports',   'NE.EXP.GNFS.CD',      '出口总额',        '美元'),
    ('imports',   'NE.IMP.GNFS.CD',      '进口总额',        '美元'),
    ('debt',      'GC.DOD.TOTL.GD.ZS',   '政府债务/GDP',    '%'),
    ('reserves',  'FI.RES.TOTL.CD',      '外汇储备',        '美元'),
    ('fdi',       'BX.KLT.DINV.CD.WD',   'FDI 净流入',      '美元'),
    ('ca',        'BN.CAB.XOKA.CD',      '经常账户',        '美元'),
    ('pop',       'SP.POP.TOTL',         '人口',            '人'),
]

# 世界银行国家名 → ECharts 世界地图名（差异项）
NAME_FIX = {
    'Russian Federation': 'Russia',
    'Korea, Rep.': 'South Korea',
    "Korea, Dem. People's Rep.": 'North Korea',
    'Iran, Islamic Rep.': 'Iran',
    'Egypt, Arab Rep.': 'Egypt',
    'Venezuela, RB': 'Venezuela',
    'Syrian Arab Republic': 'Syria',
    'Yemen, Rep.': 'Yemen',
    'Congo, Dem. Rep.': 'Dem. Rep. Congo',
    'Congo, Rep.': 'Congo',
    'Lao PDR': 'Laos',
    'Viet Nam': 'Vietnam',
    'Brunei Darussalam': 'Brunei',
    'Slovak Republic': 'Slovakia',
    'Czechia': 'Czech Rep.',
    'Turkiye': 'Turkey',
    'Türkiye': 'Turkey',
    'Hong Kong SAR, China': 'Hong Kong',
    'Macao SAR, China': 'Macao',
    'Taiwan, China': 'Taiwan',
    "Cote d'Ivoire": "Côte d'Ivoire",
    'Kyrgyz Republic': 'Kyrgyzstan',
    'Macedonia, FYR': 'Macedonia',
    'West Bank and Gaza': 'Palestine',
    'Gambia, The': 'Gambia',
    'Bahamas, The': 'Bahamas',
    'St. Lucia': 'Saint Lucia',
    'St. Vincent and the Grenadines': 'Saint Vincent and the Grenadines',
    'St. Kitts and Nevis': 'Saint Kitts and Nevis',
    'Antigua and Barbuda': 'Antigua and Barb.',
    'Trinidad and Tobago': 'Trinidad and Tob.',
    'Equatorial Guinea': 'Eq. Guinea',
    'Solomon Islands': 'Solomon Is.',
    'Timor-Leste': 'East Timor',
    'Cabo Verde': 'Cape Verde',
    'Eswatini': 'eSwatini',
    'Sint Maarten (Dutch part)': 'Sint Maarten',
    'St. Martin (French part)': 'Saint Martin',
    'British Virgin Islands': 'British Virgin Is.',
    'United States': 'United States',
    'United Kingdom': 'United Kingdom',
    'South Sudan': 'S. Sudan',
    'Central African Republic': 'Central African Rep.',
    'Dominican Republic': 'Dominican Rep.',
    'Bosnia and Herzegovina': 'Bosnia and Herz.',
    'Sao Tome and Principe': 'São Tomé and Principe',
    'Marshall Islands': 'Marshall Is.',
    'Micronesia, Fed. Sts.': 'Micronesia',
    'Northern Mariana Islands': 'N. Mariana Is.',
    'French Polynesia': 'Fr. Polynesia',
    'New Caledonia': 'New Caledonia',
    'Falkland Islands (Malvinas)': 'Falkland Is.',
    'Turks and Caicos Islands': 'Turks and Caicos Is.',
    'Cayman Islands': 'Cayman Is.',
    'Faroe Islands': 'Faeroe Is.',
}

# 需要剔除的非主权实体（聚合体）
SKIP_NAMES = {
    'World', 'High income', 'Low income', 'Lower middle income', 'Upper middle income',
    'Middle income', 'OECD members', 'Euro area', 'European Union', 'Arab World',
    'IDA & IBRD total', 'IBRD only', 'IDA only', 'IDA blend', 'IDA total',
    'Early-demographic dividend', 'Late-demographic dividend', 'Pre-demographic dividend',
    'Post-demographic dividend', 'East Asia & Pacific', 'East Asia & Pacific (excluding high income)',
    'East Asia & Pacific (IDA & IBRD countries)', 'Europe & Central Asia',
    'Europe & Central Asia (excluding high income)', 'Europe & Central Asia (IDA & IBRD countries)',
    'Latin America & Caribbean', 'Latin America & Caribbean (excluding high income)',
    'Latin America & the Caribbean (IDA & IBRD countries)',
    'Middle East & North Africa', 'Middle East & North Africa (excluding high income)',
    'Middle East & North Africa (IDA & IBRD countries)', 'North America',
    'South Asia', 'South Asia (IDA & IBRD)', 'Sub-Saharan Africa',
    'Sub-Saharan Africa (excluding high income)', 'Sub-Saharan Africa (IDA & IBRD countries)',
    'Central Europe and the Baltics', 'Caribbean small states', 'Pacific island small states',
    'Other small states', 'Small states', 'Least developed countries: UN classification',
    'Heavily indebted poor countries (HIPC)', 'Fragile and conflict affected situations',
    'Africa Eastern and Southern', 'Africa Western and Central', 'Euro area',
    'Not classified', 'Arabic', 'British', 'Chinese',
}


# 主要经济体（ISO3，覆盖全球 90% 以上 GDP）——指定国家批量查，避免 all-countries 分页漏数据
MAIN_COUNTRIES = [
    'CHN', 'USA', 'JPN', 'DEU', 'IND', 'GBR', 'FRA', 'ITA', 'BRA', 'CAN',
    'RUS', 'KOR', 'AUS', 'MEX', 'ESP', 'IDN', 'NLD', 'SAU', 'TUR', 'CHE',
    'POL', 'TWN', 'ARG', 'SWE', 'NOR', 'AUT', 'IRL', 'ISR', 'ARE', 'THA',
    'SGP', 'PHL', 'VNM', 'MYS', 'EGY', 'ZAF', 'DNK', 'FIN', 'CZE', 'PRT',
    'ROU', 'CHL', 'PER', 'COL', 'NZL', 'GRC', 'HUN', 'UKR', 'PAK', 'BGD',
    'NGA', 'KEN', 'ETH', 'MAR', 'DZA', 'KAZ', 'QAT', 'KWT', 'IRQ', 'LKA',
]


def _wb_one(ind):
    code = ind[1]
    # 多个国家用 %3B 编码的分号连接（未编码的 ; 会被世界银行当成参数分隔，导致数据错乱）
    cs = '%3B'.join(MAIN_COUNTRIES)
    base = ('https://api.worldbank.org/v2/country/%s/indicator/%s'
            '?format=json&per_page=300&date=2021:2024&page=%d')
    rows = []
    # 世界银行接口有限流，失败重试最多 3 次（每次间隔递增）
    for attempt in range(3):
        rows = []
        try:
            d = _getj(base % (cs, code, 1), timeout=30)
            if not isinstance(d, list) or len(d) < 2:
                raise ValueError('empty')
            rows.extend(d[1] or [])
            pages = int((d[0] or {}).get('pages') or 1)
            for p in range(2, min(pages, 6) + 1):
                d2 = _getj(base % (cs, code, p), timeout=30)
                if isinstance(d2, list) and len(d2) > 1:
                    rows.extend(d2[1] or [])
            if rows:
                break
        except Exception:
            rows = []
        time.sleep(1.0 + attempt * 0.8)
    if not rows:
        return ind[0], {}
    d = [None, rows]
    if not isinstance(d, list) or len(d) < 2 or not d[1]:
        return ind[0], {}
    best = {}
    for r in d[1]:
        nm = ((r.get('country') or {}).get('value') or '').strip()
        v = r.get('value')
        if not nm or v is None or nm in SKIP_NAMES:
            continue
        try:
            yr = int(r.get('date') or 0)
        except Exception:
            continue
        if nm not in best or yr > best[nm][0]:
            best[nm] = (yr, float(v))
    return ind[0], best


def build_macro():
    rows = {}
    years = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(_wb_one, WB_INDICATORS))
    # 补漏：并发时被限流失败的指标，串行再查一次（世界银行对并发敏感）
    for i, (ind, (k, best)) in enumerate(zip(WB_INDICATORS, results)):
        if best:
            continue
        time.sleep(1.5)
        k2, best2 = _wb_one(ind)
        if best2:
            results[i] = (k2, best2)
            print('retry ok:', k2, len(best2), 'countries')
    for key, best in results:
        for nm, (yr, v) in best.items():
            rows.setdefault(nm, {'name': nm})[key] = v
            years[nm] = max(years.get(nm, 0), yr)
    # 名称对齐地图
    out = {}
    for nm, d in rows.items():
        mapname = NAME_FIX.get(nm, nm)
        d['map'] = mapname
        d['year'] = years.get(nm, 0)
        out[nm] = d
    # 派生：贸易差额、贸易总额、FDI/GA 比
    for d in out.values():
        e, i = d.get('exports'), d.get('imports')
        if e is not None and i is not None:
            d['trade_balance'] = e - i
            d['trade_total'] = e + i
        g, p = d.get('gdp'), d.get('pop')
        if g and p:
            d['gdp_pc'] = d.get('gdp_pc') or (g / p)
    meta = {
        'updated': datetime.now(CST).strftime('%Y-%m-%d %H:%M'),
        'source': '世界银行公开数据（年度）',
        'indicators': [{'key': k, 'label': l, 'unit': u} for k, c, l, u in WB_INDICATORS],
        'count': len(out),
    }
    return {'meta': meta, 'countries': out}


def get_macro():
    return _cache('macro', 6 * 3600, build_macro)


# ---------------- 实时汇率 ----------------
FX_MAJORS = [
    ('CNY', '人民币', '中国'), ('EUR', '欧元', '欧元区'), ('JPY', '日元', '日本'),
    ('GBP', '英镑', '英国'), ('KRW', '韩元', '韩国'), ('INR', '卢比', '印度'),
    ('BRL', '雷亚尔', '巴西'), ('RUB', '卢布', '俄罗斯'), ('AUD', '澳元', '澳大利亚'),
    ('CAD', '加元', '加拿大'), ('CHF', '瑞郎', '瑞士'), ('HKD', '港币', '中国香港'),
    ('SGD', '新元', '新加坡'), ('THB', '泰铢', '泰国'), ('MXN', '比索', '墨西哥'),
    ('ZAR', '兰特', '南非'), ('TRY', '里拉', '土耳其'), ('SAR', '里亚尔', '沙特'),
    ('AED', '迪拉姆', '阿联酋'), ('IDR', '印尼盾', '印尼'), ('MYR', '林吉特', '马来西亚'),
    ('PHP', '比索', '菲律宾'), ('VND', '越南盾', '越南'), ('NGN', '奈拉', '尼日利亚'),
    ('ARS', '比索', '阿根廷'), ('EGP', '埃及镑', '埃及'), ('PLN', '兹罗提', '波兰'),
    ('SEK', '瑞典克朗', '瑞典'), ('NOK', '挪威克朗', '挪威'), ('NZD', '纽元', '新西兰'),
]


def build_fx():
    cur = _getj('https://open.er-api.com/v6/latest/USD', timeout=20)
    rates = cur.get('rates') or {}
    prev = {}
    # 前一交易日（frankfurter，免费无 key）
    try:
        d0 = (datetime.now(CST) - timedelta(days=4)).strftime('%Y-%m-%d')
        h = _getj('https://api.frankfurter.app/%s?from=USD' % d0, timeout=20)
        prev = h.get('rates') or {}
    except Exception:
        prev = {}
    items = []
    for code, cn, country in FX_MAJORS:
        r = rates.get(code)
        if not r:
            continue
        p = prev.get(code)
        chg = ((r - p) / p * 100) if p else None
        items.append({'code': code, 'cn': cn, 'country': country,
                      'rate': round(r, 6), 'chg': round(chg, 3) if chg is not None else None})
    return {
        'updated': datetime.now(CST).strftime('%Y-%m-%d %H:%M'),
        'base': 'USD',
        'updateUtc': cur.get('time_last_update_utc'),
        'items': items,
        'source': 'open.er-api.com（日更）',
    }


def get_fx():
    return _cache('fx', 1800, build_fx)


# ---------------- 公司控股关系（Wikidata） ----------------
COMPANIES = [
    ('Q312', 'Apple', '苹果'), ('Q2283', 'Microsoft', '微软'),
    ('Q20800404', 'Alphabet (Google)', '谷歌母公司'), ('Q3884', 'Amazon', '亚马逊'),
    ('Q380', 'Meta', 'Meta'), ('Q478214', 'Tesla', '特斯拉'),
    ('Q182477', 'NVIDIA', '英伟达'), ('Q713367', 'TSMC', '台积电'),
    ('Q20716', 'Samsung Electronics', '三星电子'), ('Q860580', 'Tencent', '腾讯'),
    ('Q1359568', 'Alibaba', '阿里巴巴'), ('Q217583', 'Berkshire Hathaway', '伯克希尔'),
    ('Q192314', 'JPMorgan Chase', '摩根大通'), ('Q219635', 'BlackRock', '贝莱德'),
    ('Q849077', 'Vanguard', '先锋领航'), ('Q193326', 'Walmart', '沃尔玛'),
    ('Q3295867', 'ByteDance', '字节跳动'), ('Q1594362', 'Saudi Aramco', '沙特阿美'),
    ('Q842100', 'Toyota', '丰田'), ('Q1637004', 'Volkswagen', '大众'),
    ('Q1162536', 'ASML', '阿斯麦'), ('Q2018388', 'Broadcom', '博通'),
    ('Q1131200', 'Pfizer', '辉瑞'), ('Q2102723', 'ExxonMobil', '埃克森美孚'),
]

SPARQL_PARENT = """
SELECT ?c ?cLabel ?p ?pLabel WHERE {
  VALUES ?c { %s }
  ?c wdt:P127 ?p .
  SERVICE wikibase:label { bd:serviceParam wikibase:language "zh,en". }
} LIMIT 200
"""

SPARQL_OWNER = """
SELECT ?c ?cLabel ?o ?oLabel ?share WHERE {
  VALUES ?c { %s }
  ?c p:P1830 ?st .
  ?st ps:P1830 ?o .
  OPTIONAL { ?st pq:P1107 ?share . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "zh,en". }
} LIMIT 200
"""


def _sparql(q):
    url = 'https://query.wikidata.org/sparql?format=json&query=' + urllib.parse.quote(q)
    d = _getj(url, timeout=35)
    return ((d.get('results') or {}).get('bindings') or [])


def build_holdings():
    vals = ' '.join('wd:' + q for q, en, cn in COMPANIES)
    nodes = {}
    links = []
    for q, en, cn in COMPANIES:
        nodes[q] = {'id': q, 'name': cn, 'en': en, 'type': 'company'}
    try:
        for b in _sparql(SPARQL_PARENT % vals):
            cid = (b.get('c') or {}).get('value', '').rsplit('/', 1)[-1]
            pid = (b.get('p') or {}).get('value', '').rsplit('/', 1)[-1]
            if not cid or not pid:
                continue
            if pid not in nodes:
                nodes[pid] = {'id': pid, 'name': (b.get('pLabel') or {}).get('value', pid), 'type': 'parent'}
            links.append({'source': pid, 'target': cid, 'rel': '母公司'})
    except Exception:
        pass
    try:
        for b in _sparql(SPARQL_OWNER % vals):
            cid = (b.get('c') or {}).get('value', '').rsplit('/', 1)[-1]
            oid = (b.get('o') or {}).get('value', '').rsplit('/', 1)[-1]
            if not cid or not oid:
                continue
            if oid not in nodes:
                nodes[oid] = {'id': oid, 'name': (b.get('oLabel') or {}).get('value', oid), 'type': 'owner'}
            share = (b.get('share') or {}).get('value')
            links.append({'source': oid, 'target': cid, 'rel': '股东' + ((' ' + share) if share else '')})
    except Exception:
        pass
    # 去重
    seen = set()
    uniq = []
    for l in links:
        k = (l['source'], l['target'], l['rel'])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(l)
    return {
        'updated': datetime.now(CST).strftime('%Y-%m-%d %H:%M'),
        'source': 'Wikidata（维基数据）',
        'nodes': list(nodes.values()),
        'links': uniq,
    }


def get_holdings():
    return _cache('holdings', 12 * 3600, build_holdings)


if __name__ == '__main__':
    t = time.time()
    m = get_macro()
    print('macro: %d 国家, %.1fs' % (m['meta']['count'], time.time() - t))
    for k in ('China', 'United States', 'Germany'):
        d = m['countries'].get(k) or {}
        print(' ', k, 'GDP', d.get('gdp'), '出口', d.get('exports'), 'FDI', d.get('fdi'))
    f = get_fx()
    print('fx: %d 货币' % len(f['items']))
    h = get_holdings()
    print('holdings: %d 节点 %d 关系' % (len(h['nodes']), len(h['links'])))
