# -*- coding: utf-8 -*-
"""第三轮：确认 trade-history 可用参数 + 找出真实持仓非空的交易员"""
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
EP_TRADE = BAPI + '/public/future/copy-trade/lead-portfolio/trade-history'
EP_POS = BAPI + '/friendly/future/copy-trade/lead-data/positions?portfolioId='
EP_QLIST = BAPI + '/friendly/future/copy-trade/home-page/query-list'


def req(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(r, timeout=15, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


print('=== 1) 熬鹰 trade-history 不同 timeType ===')
for tt in ('30D', '90D', '1D', '7'):
    try:
        d = req(EP_TRADE, {'portfolioId': PID_AYAO, 'timeType': tt, 'pageNo': 1, 'pageSize': 10})
        data = d.get('data') or {}
        lst = data.get('list') or []
        print('  timeType=%s total=%s list=%d' % (tt, data.get('total'), len(lst)))
        if lst:
            print('    raw[0]:', json.dumps(lst[0], ensure_ascii=False)[:400])
            break
    except Exception as e:
        print('  timeType=%s ERR %s' % (tt, str(e)[:100]))

print('=== 2) 排行榜前 12 人：真实持仓(positionAmount!=0)统计 + trade-history ===')
d = req(EP_QLIST, {'pageNumber': 1, 'pageSize': 20, 'timeRange': '90D', 'dataType': 'ROI',
                   'favoriteOnly': False, 'hideFull': False, 'nickname': '', 'order': 'DESC', 'apiKeyOnly': False})
lst = (d.get('data') or {}).get('list') or []
with_pos = 0
for t in lst[:12]:
    pid = str(t.get('leadPortfolioId') or '')
    name = t.get('nickname')
    try:
        p = req(EP_POS + pid)
        arr = p.get('data') or []
        real = [x for x in arr if x.get('positionAmount') not in (None, '0', 0, '0.0', '0.000')]
        sides = ['%s %s x%s' % (x['symbol'], x.get('positionSide'), x.get('leverage')) for x in real[:4]]
        if real:
            with_pos += 1
        print('  %s 持仓%d个 %s' % (name, len(real), sides))
        if with_pos == 1 and real:
            th = req(EP_TRADE, {'portfolioId': pid, 'timeType': '7D', 'pageNo': 1, 'pageSize': 10})
            tdata = th.get('data') or {}
            tl = tdata.get('list') or []
            print('    trade-history 7D total=%s list=%d' % (tdata.get('total'), len(tl)))
            if tl:
                print('    th raw[0]:', json.dumps(tl[0], ensure_ascii=False)[:420])
    except Exception as e:
        print('  %s ERR %s' % (name, str(e)[:90]))
print('前12人中真实持仓非空: %d 人' % with_pos)
