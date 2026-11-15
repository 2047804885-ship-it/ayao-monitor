# -*- coding: utf-8 -*-
"""第四轮：查清熬鹰账户能拿到的全部信息（detail 全字段 + trade-history 各参数）"""
import json
import ssl
import urllib.request

BAPI = 'https://www.binance.com/bapi/futures/v1'
UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0',
    'Referer': 'https://www.binance.com/',
    'Origin': 'https://www.binance.com',
    'Content-Type': 'application/json',
    'clienttype': 'web',
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
PID = '5075281354358777856'


def get(url):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def post(url, body):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


print('=== A) detail 接口全字段 ===')
try:
    d = get(BAPI + '/friendly/future/copy-trade/lead-portfolio/detail?portfolioId=' + PID)
    data = d.get('data') or {}
    for k, v in data.items():
        sv = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
        print('  %-24s = %s' % (k, sv[:120]))
except Exception as e:
    print('  ERR', str(e)[:150])

print('=== B) trade-history 各参数（POST）===')
EP = BAPI + '/public/future/copy-trade/lead-portfolio/trade-history'
for tt in ('7D', '30D', '90D', '1D', 'ALL'):
    for ps in (10, 50):
        try:
            d = post(EP, {'portfolioId': PID, 'timeType': tt, 'pageNo': 1, 'pageSize': ps})
            data = d.get('data') or {}
            lst = data.get('list') or []
            print('  timeType=%-4s pageSize=%-3s total=%-4s list=%d' % (tt, ps, data.get('total'), len(lst)))
            if lst:
                print('    raw[0]:', json.dumps(lst[0], ensure_ascii=False)[:500])
        except Exception as e:
            print('  timeType=%-4s pageSize=%-3s ERR %s' % (tt, ps, str(e)[:90]))

print('=== C) positions 是否有真实持仓 ===')
try:
    d = get(BAPI + '/friendly/future/copy-trade/lead-data/positions?portfolioId=' + PID)
    arr = d.get('data') or []
    real = [x for x in arr if str(x.get('positionAmount')) not in ('0', '0.0', '0.000', 'None')]
    print('  返回 %d 条，非零持仓 %d 条' % (len(arr), len(real)))
    if real:
        print('  raw[0]:', json.dumps(real[0], ensure_ascii=False)[:500])
except Exception as e:
    print('  ERR', str(e)[:150])

print('=== D) 其他可能含成交的端点 ===')
for path in ('/friendly/future/copy-trade/lead-data/trade-history?portfolioId=',
             '/public/future/copy-trade/lead-portfolio/position?portfolioId=',
             '/friendly/future/copy-trade/lead-portfolio/position?portfolioId='):
    try:
        d = get(BAPI + path + PID)
        data = d.get('data')
        n = len(data) if isinstance(data, list) else ('obj' if data else 0)
        print('  OK %-70s -> %s' % (path.split('?')[0], n))
        if isinstance(data, list) and data:
            print('     raw[0]:', json.dumps(data[0], ensure_ascii=False)[:300])
        elif isinstance(data, dict):
            print('     keys:', list(data.keys())[:8])
    except Exception as e:
        print('  ERR %-70s %s' % (path.split('?')[0], str(e)[:60]))
