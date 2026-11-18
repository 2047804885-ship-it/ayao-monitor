# -*- coding: utf-8 -*-
"""第三轮：找聚合的开/平仓接口（对应截图「最新交易记录」）"""
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


def post(path, body):
    url = BAPI + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            return r.status, r.read().decode('utf-8', 'ignore')
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode('utf-8', 'ignore')
        except Exception:
            return e.code, ''
    except Exception as e:
        return 'ERR', str(e)[:100]


res = {}
CAND = [
    ('pos_hist_friendly', '/friendly/future/copy-trade/lead-portfolio/position-history'),
    ('pos_hist_public', '/public/future/copy-trade/lead-portfolio/position-history'),
    ('order_hist_friendly', '/friendly/future/copy-trade/lead-portfolio/order-history'),
    ('order_hist_public', '/public/future/copy-trade/lead-portfolio/order-history'),
    ('pos_hist_data', '/friendly/future/copy-trade/lead-data/position-history'),
    ('trade_hist_friendly', '/friendly/future/copy-trade/lead-portfolio/trade-history'),
]
BODY = {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 10}

for name, path in CAND:
    st, txt = post(path, BODY)
    try:
        j = json.loads(txt)
        d = j.get('data') or {}
        lst = d.get('list') if isinstance(d, dict) else (d if isinstance(d, list) else [])
        n = len(lst or [])
        item = (lst or [None])[0]
        res[name] = {'status': st, 'code': j.get('code'), 'total': d.get('total') if isinstance(d, dict) else None,
                     'n': n, 'first': item}
        print('%-20s %s code=%s total=%s n=%s' % (name, st, j.get('code'),
              d.get('total') if isinstance(d, dict) else '-', n))
        if item:
            print('     keys:', list(item.keys())[:18])
            print('     item:', json.dumps(item, ensure_ascii=False)[:400])
    except Exception as e:
        res[name] = {'status': st, 'raw': txt[:200]}
        print('%-20s %s parse-fail %s' % (name, st, txt[:120]))

# 验证：把 trade-history 的成交按「同一秒」聚合，看能否还原 8.011BTC 这种大单
st, txt = post('/friendly/future/copy-trade/lead-portfolio/trade-history',
               {'portfolioId': PID, 'pageNumber': 1, 'pageSize': 200})
try:
    j = json.loads(txt)
    lst = (j.get('data') or {}).get('list') or []
    from collections import defaultdict
    g = defaultdict(lambda: {'qty': 0.0, 'val': 0.0, 'pnl': 0.0, 'n': 0})
    for it in lst:
        k = (it['symbol'], it['side'], it['positionSide'], it['time'] // 1000)
        g[k]['qty'] += float(it.get('qty') or 0)
        g[k]['val'] += float(it.get('quantity') or 0)
        g[k]['pnl'] += float(it.get('realizedProfit') or 0)
        g[k]['n'] += 1
    keys = sorted(g.keys(), key=lambda x: -x[3])[:8]
    agg = []
    for k in keys:
        v = g[k]
        agg.append({'symbol': k[0], 'side': k[1], 'ps': k[2],
                    'ts': time.strftime('%m-%d %H:%M:%S', time.localtime(k[3])),
                    'fills': v['n'], 'qty': round(v['qty'], 6),
                    'val': round(v['val'], 2),
                    'avg': round(v['val'] / v['qty'], 4) if v['qty'] else 0,
                    'pnl': round(v['pnl'], 2)})
    res['agg_1s'] = agg
    print('\n按秒聚合（前 8 组）:')
    for a in agg:
        print('  ', a['ts'], a['symbol'], a['side'], a['ps'], 'fills=%s qty=%s avg=%s val=%s pnl=%s'
              % (a['fills'], a['qty'], a['avg'], a['val'], a['pnl']))
    print('trade-history 200 条总成交额:', round(sum(float(i.get('quantity') or 0) for i in lst), 2))
except Exception as e:
    res['agg_1s'] = {'err': str(e)[:120]}
    print('agg fail', e)

with open('probe_out.json', 'w', encoding='utf-8') as f:
    json.dump({'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'results': res}, f, ensure_ascii=False, indent=1)
print('written')
