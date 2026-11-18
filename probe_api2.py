# -*- coding: utf-8 -*-
"""第二轮探测：确认交易记录字段含义 + 测试 Cloudflare Worker 能否直连币安"""
import json
import ssl
import time
import urllib.request
import urllib.error

PID = '5075281354358777856'
BAPI = 'https://www.binance.com/bapi/futures/v1'
WORKER = 'https://ayao-monitor.jikui-ayao.workers.dev'
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
out = {}


def raw(url, body=None, method=None, timeout=20, ua=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=(ua or UA), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode('utf-8', 'ignore')
        except Exception:
            return e.code, ''
    except Exception as e:
        return 'ERR', str(e)[:120]


# ---------- 1) 交易记录：拉 30 条看规律 ----------
st, txt = raw(BAPI + '/friendly/future/copy-trade/lead-portfolio/trade-history',
              {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 30}, method='POST')
print('trade-history:', st, len(txt))
try:
    j = json.loads(txt)
    d = j.get('data') or {}
    lst = d.get('list') or []
    out['trade'] = {'status': st, 'total': d.get('total'), 'n': len(lst), 'items': lst[:30]}
    print('total=%s n=%s' % (d.get('total'), len(lst)))
    for it in lst[:6]:
        print('  ', time.strftime('%m-%d %H:%M:%S', time.localtime(it['time'] / 1000)),
              it.get('symbol'), it.get('side'), it.get('positionSide'),
              'price=%s qty=%s val=%s pnl=%s' % (it.get('price'), it.get('qty'),
                                                 it.get('quantity'), it.get('realizedProfit')))
except Exception as e:
    out['trade'] = {'status': st, 'raw': txt[:400]}
    print('parse fail', e)

# ---------- 2) Worker 能否直连币安 ----------
st, txt = raw(WORKER + '/api/test', timeout=30)
out['worker_test'] = {'status': st, 'body': txt[:1200]}
print('worker /api/test:', st, txt[:300])

# ---------- 3) Worker /api/data 是否活着 ----------
st, txt = raw(WORKER + '/api/data', timeout=30)
out['worker_data'] = {'status': st, 'len': len(txt), 'head': txt[:300]}
print('worker /api/data:', st, len(txt))

with open('probe_out.json', 'w', encoding='utf-8') as f:
    json.dump({'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'pid': PID, 'results': out},
              f, ensure_ascii=False, indent=1)
print('written')
