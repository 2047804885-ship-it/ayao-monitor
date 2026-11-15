# -*- coding: utf-8 -*-
"""第二轮探测 trade-history：大写 timeType / POST / 其他参数组合"""
import json
import ssl
import urllib.request

BAPI = 'https://www.binance.com/bapi/futures/v1'
EP = BAPI + '/public/future/copy-trade/lead-portfolio/trade-history'
UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36',
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
    try:
        r = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(r, timeout=12, context=CTX) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        data = d.get('data')
        size = len(data) if isinstance(data, list) else ('obj:' + json.dumps(data, ensure_ascii=False)[:120] if data else str(data)[:80])
        print('OK  code=%s data=%s' % (d.get('code'), size))
        if isinstance(data, list) and data:
            print('    raw[0]:', json.dumps(data[0], ensure_ascii=False)[:400])
        return d
    except Exception as e:
        print('ERR', str(e)[:130])
        return None


def post(url, body):
    try:
        r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
        with urllib.request.urlopen(r, timeout=12, context=CTX) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        data = d.get('data')
        size = len(data) if isinstance(data, list) else ('obj:' + json.dumps(data, ensure_ascii=False)[:120] if data else str(data)[:80])
        print('OK  code=%s data=%s' % (d.get('code'), size))
        if isinstance(data, list) and data:
            print('    raw[0]:', json.dumps(data[0], ensure_ascii=False)[:400])
        elif isinstance(data, dict):
            keys = list(data.keys())[:10]
            print('    data.keys:', keys)
            lst = data.get('list') or data.get('records') or []
            if lst:
                print('    list[0]:', json.dumps(lst[0], ensure_ascii=False)[:400])
        return d
    except Exception as e:
        print('ERR', str(e)[:130])
        return None


print('A) GET 大写 7D')
get(EP + '?portfolioId=%s&timeType=7D&pageNo=1&pageSize=20' % PID)
print('B) GET timeType=30')
get(EP + '?portfolioId=%s&timeType=30&pageNo=1&pageSize=20' % PID)
print('C) POST portfolioId+timeType')
post(EP, {'portfolioId': PID, 'timeType': '7D', 'pageNo': 1, 'pageSize': 20})
print('D) POST leadPortfolioId')
post(EP, {'leadPortfolioId': PID, 'timeType': '7D', 'pageNo': 1, 'pageSize': 20})
print('E) GET 无 timeType 有 page')
get(EP + '?portfolioId=%s&pageNo=1&pageSize=20' % PID)
print('F) GET timeType=ALL')
get(EP + '?portfolioId=%s&timeType=ALL&pageNo=1&pageSize=20' % PID)
