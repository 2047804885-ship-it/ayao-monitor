# -*- coding: utf-8 -*-
"""一次性探测 trade-history 接口的正确参数"""
import json
import ssl
import urllib.request

BAPI = 'https://www.binance.com/bapi/futures/v1'
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

PID_AYAO = '5075281354358777856'


def probe(url):
    try:
        r = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(r, timeout=12, context=CTX) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        code = d.get('code')
        data = d.get('data')
        size = len(data) if isinstance(data, list) else ('obj' if data else str(data)[:60])
        print('OK  code=%s data=%s' % (code, size))
        if isinstance(data, list) and data:
            print('    raw[0]:', json.dumps(data[0], ensure_ascii=False)[:420])
        elif data and not isinstance(data, list):
            print('    data obj:', json.dumps(data, ensure_ascii=False)[:300])
        return d
    except Exception as e:
        print('ERR', str(e)[:150])
        return None


print('=== trade-history 参数探测 ===')
print('1) GET 无参')
probe(BAPI + '/public/future/copy-trade/lead-portfolio/trade-history?portfolioId=' + PID_AYAO)
print('2) GET timeType+page')
probe(BAPI + '/public/future/copy-trade/lead-portfolio/trade-history?portfolioId=%s&timeType=7d&pageNo=1&pageSize=20' % PID_AYAO)
print('3) GET timestamp 变体')
probe(BAPI + '/public/future/copy-trade/lead-portfolio/trade-history?portfolioId=%s&timeStamp=7d&pageNo=1&pageSize=20' % PID_AYAO)
print('4) GET v2 变体')
probe(BAPI + '/public/future/copy-trade/lead-portfolio/trade-history/v2?portfolioId=%s&pageNo=1&pageSize=20' % PID_AYAO)
print('5) GET friendly 变体')
probe(BAPI + '/friendly/future/copy-trade/lead-portfolio/trade-history?portfolioId=%s&pageNo=1&pageSize=20' % PID_AYAO)

print('=== 排行榜里公开持仓的验证（positionAmount>0 的）===')
r = urllib.request.Request(BAPI + '/friendly/future/copy-trade/home-page/query-list',
                           data=json.dumps({'pageNumber': 1, 'pageSize': 20, 'timeRange': '90D',
                                            'dataType': 'ROI', 'favoriteOnly': False, 'hideFull': False,
                                            'nickname': '', 'order': 'DESC', 'apiKeyOnly': False}).encode(),
                           headers=UA)
with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
    d = json.loads(resp.read().decode('utf-8'))
lst = (d.get('data') or {}).get('list') or []
print('排行榜 %d 人，字段: %s' % (len(lst), [k for k in (lst[0].keys() if lst else []) if 'osition' in k or 'Show' in k or 'show' in k]))
for t in lst[:6]:
    print('  %s positionShow=%s roi=%s winRate=%s copiers=%s' % (
        t.get('nickname'), t.get('positionShow'), t.get('roi'), t.get('winRate'), t.get('currentCopyCount')))
