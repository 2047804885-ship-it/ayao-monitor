# -*- coding: utf-8 -*-
"""在 GitHub Actions（可访问 binance）里穷举探测「最新交易记录」接口"""
import json
import ssl
import time
import urllib.request
import urllib.error

PID = '5075281354358777856'
BAPI = 'https://www.binance.com/bapi/futures/v1'
UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Content-Type': 'application/json',
    'clienttype': 'web',
    'lang': 'zh-CN',
    'Origin': 'https://www.binance.com',
    'Referer': 'https://www.binance.com/zh-CN/copy-trading/lead-details/' + PID,
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

now_ms = int(time.time() * 1000)
day_ms = 30 * 24 * 3600 * 1000

# (名称, 方法, 路径, 查询串, body)
V = [
    # ---- trade-history 各种变体 ----
    ('th_get_basic', 'GET', '/public/future/copy-trade/lead-portfolio/trade-history',
     '?portfolioId=%s&pageNumber=1&pageSize=20' % PID, None),
    ('th_get_timerange', 'GET', '/public/future/copy-trade/lead-portfolio/trade-history',
     '?portfolioId=%s&pageNumber=1&pageSize=20&timeRange=90D' % PID, None),
    ('th_get_pageNo', 'GET', '/public/future/copy-trade/lead-portfolio/trade-history',
     '?portfolioId=%s&pageNo=1&pageSize=20' % PID, None),
    ('th_post_basic', 'POST', '/public/future/copy-trade/lead-portfolio/trade-history', '',
     {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 20}),
    ('th_post_time', 'POST', '/public/future/copy-trade/lead-portfolio/trade-history', '',
     {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 20,
      'startTime': now_ms - day_ms, 'endTime': now_ms}),
    ('th_post_timeRange', 'POST', '/public/future/copy-trade/lead-portfolio/trade-history', '',
     {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 20, 'timeRange': '90D'}),
    ('th_friendly_get', 'GET', '/friendly/future/copy-trade/lead-portfolio/trade-history',
     '?portfolioId=%s&pageNumber=1&pageSize=20' % PID, None),
    ('th_friendly_post', 'POST', '/friendly/future/copy-trade/lead-portfolio/trade-history', '',
     {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 20}),
    ('th_v2', 'GET', '/public/future/copy-trade/lead-portfolio/trade-history/v2',
     '?portfolioId=%s&pageNumber=1&pageSize=20' % PID, None),
    ('th_lead_data', 'GET', '/friendly/future/copy-trade/lead-data/trade-history',
     '?portfolioId=%s&pageNumber=1&pageSize=20' % PID, None),
    # ---- 持仓 / 仓位历史 ----
    ('pos_friendly', 'GET', '/friendly/future/copy-trade/lead-data/positions',
     '?portfolioId=' + PID, None),
    ('pos_public', 'GET', '/public/future/copy-trade/lead-data/positions',
     '?portfolioId=' + PID, None),
    ('poshistory', 'GET', '/public/future/copy-trade/lead-portfolio/position-history',
     '?portfolioId=%s&pageNumber=1&pageSize=20' % PID, None),
    ('poshistory_f', 'GET', '/friendly/future/copy-trade/lead-portfolio/position-history',
     '?portfolioId=%s&pageNumber=1&pageSize=20' % PID, None),
    # ---- 其他可能入口 ----
    ('detail', 'GET', '/friendly/future/copy-trade/lead-portfolio/detail',
     '?portfolioId=' + PID, None),
    ('perf', 'GET', '/friendly/future/copy-trade/lead-portfolio/performance',
     '?portfolioId=' + PID, None),
]


def call(method, path, query, body):
    url = BAPI + path + query
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=UA, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as resp:
            txt = resp.read().decode('utf-8', 'ignore')
            status = resp.status
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode('utf-8', 'ignore')
        except Exception:
            txt = ''
        status = e.code
    except Exception as e:
        return {'status': 'ERR', 'err': str(e)[:100], 'ms': int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    out = {'status': status, 'ms': ms, 'len': len(txt)}
    try:
        j = json.loads(txt)
        out['code'] = j.get('code')
        out['msg'] = str(j.get('message') or j.get('msg') or '')[:80]
        data = j.get('data')
        if isinstance(data, list):
            out['dataType'] = 'list'
            out['n'] = len(data)
            if data:
                out['first'] = data[0]
        elif isinstance(data, dict):
            out['dataType'] = 'dict'
            out['keys'] = list(data.keys())[:20]
            for k in ('list', 'rows', 'total', 'totalCount'):
                if k in data:
                    out['k_' + k] = data[k] if k != 'list' else len(data[k] or [])
            if data.get('list'):
                out['first'] = data['list'][0]
        else:
            out['dataType'] = type(data).__name__
            out['data'] = str(data)[:100]
    except Exception:
        out['raw'] = txt[:200]
    return out


res = {}
for name, method, path, query, body in V:
    r = call(method, path, query, body)
    res[name] = r
    print('%-18s %-4s %s n=%s code=%s' % (name, method, r.get('status'), r.get('n'), r.get('code')))

with open('probe_out.json', 'w', encoding='utf-8') as f:
    json.dump({'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'pid': PID, 'results': res},
              f, ensure_ascii=False, indent=1)
print('written probe_out.json')
