# -*- coding: utf-8 -*-
"""
聪明钱共识 v2
数据源 1（主）：币安官方「大户持仓比」聚合数据（fapi.binance.com，runner 可直连）
  - topLongShortPositionRatio：大户持仓量多空比（聪明钱方向）
  - globalLongShortAccountRatio：散户账户多空比（反向指标）
  - takerlongshortRatio：主动成交买卖比（即时动能）
数据源 2（补充）：跟单排行榜高胜率交易员的最近成交（trade-history POST 接口）
输出 consensus.json
"""
import json
import time
import ssl
import urllib.request
from collections import defaultdict

FAPI = 'https://fapi.binance.com/futures/data'
BAPI = 'https://www.binance.com/bapi/futures/v1'
EP_QLIST = BAPI + '/friendly/future/copy-trade/home-page/query-list'
EP_TRADE = BAPI + '/public/future/copy-trade/lead-portfolio/trade-history'

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0',
    'Referer': 'https://www.binance.com/',
    'Content-Type': 'application/json',
    'clienttype': 'web',
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

COINS = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT',
         'ADAUSDT', 'AVAXUSDT', 'LINKUSDT', 'TONUSDT', 'DOTUSDT', 'LTCUSDT',
         'NEARUSDT', 'SUIUSDT', 'APTUSDT', 'OPUSDT', 'ARBUSDT', 'PEPEUSDT',
         'WIFUSDT', 'FILUSDT', 'INJUSDT', 'SEIUSDT', 'TIAUSDT', 'ETCUSDT',
         'TRXUSDT', 'BCHUSDT', 'UNIUSDT', 'ATOMUSDT', 'FETUSDT', 'RNDRUSDT']


def get(url, timeout=12):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def post(url, body, timeout=12):
    r = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def f(v, d=3):
    try:
        return round(float(v), d)
    except Exception:
        return None


def whale_signal(sym):
    """大户 vs 散户的结构判断，返回 None 或信号 dict"""
    try:
        top = get('%s/topLongShortPositionRatio?symbol=%s&period=1h&limit=26' % (FAPI, sym))
        glob = get('%s/globalLongShortAccountRatio?symbol=%s&period=1h&limit=26' % (FAPI, sym))
        taker = get('%s/takerlongshortRatio?symbol=%s&period=1h&limit=8' % (FAPI, sym))
    except Exception:
        return None
    if not isinstance(top, list) or len(top) < 25:
        return None
    now = top[-1]
    prev = top[-13]   # 12 小时前
    top_now = f(now['longShortRatio'])
    top_prev = f(prev['longShortRatio'])
    if top_now is None or top_prev is None:
        return None
    top_chg = round(top_now - top_prev, 3)
    glob_now = f(glob[-1]['longShortRatio']) if isinstance(glob, list) and glob else None
    tk_now = f(taker[-1]['buySellRatio']) if isinstance(taker, list) and taker else None

    score = 0
    reasons = []
    side = None
    # 大户绝对水平
    if top_now >= 2.0:
        score += 3; reasons.append('大户持仓量多空比 %.2f（重度偏多）' % top_now)
        side = 'LONG'
    elif top_now >= 1.5:
        score += 2; reasons.append('大户持仓量多空比 %.2f（偏多）' % top_now)
        side = 'LONG'
    elif top_now <= 0.6:
        score += 3; reasons.append('大户持仓量多空比 %.2f（重度偏空）' % top_now)
        side = 'SHORT'
    elif top_now <= 1.0:
        score += 2; reasons.append('大户持仓量多空比 %.2f（偏空）' % top_now)
        side = 'SHORT'
    # 大户动向（12 小时变化）
    if top_chg >= 0.25 and top_now > 1.2:
        score += 2; reasons.append('12 小时内大户多空比上升 %+.2f，聪明钱在加多' % top_chg)
        side = side or 'LONG'
    elif top_chg <= -0.25 and top_now < 1.0:
        score += 2; reasons.append('12 小时内大户多空比下降 %+.2f，聪明钱在加空' % top_chg)
        side = side or 'SHORT'
    # 散户反向（大户与散户背离时胜率更高）
    if glob_now and side == 'LONG' and glob_now >= 1.8:
        score += 2; reasons.append('散户账户多空比 %.2f 极度看多，与大户相反' % glob_now)
    if glob_now and side == 'SHORT' and glob_now >= 1.8:
        score += 2; reasons.append('散户账户多空比 %.2f 极度看多，大户在对手盘' % glob_now)
    if glob_now and side == 'SHORT' and glob_now <= 0.7:
        score += 1; reasons.append('散户账户多空比 %.2f 极度看空' % glob_now)
    if glob_now and side == 'LONG' and glob_now <= 0.7:
        score += 1; reasons.append('散户账户多空比 %.2f 极度看空，与大户相反' % glob_now)
    # 主动成交
    if tk_now:
        if side == 'LONG' and tk_now >= 1.15:
            score += 1; reasons.append('主动买盘占优 %.2f' % tk_now)
        if side == 'SHORT' and tk_now <= 0.87:
            score += 1; reasons.append('主动卖盘占优 %.2f' % tk_now)

    if score < 4:
        return None
    return {
        'coin': sym, 'side': side, 'score': score,
        'topRatio': top_now, 'topChg': top_chg,
        'globRatio': glob_now, 'taker': tk_now,
        'reasons': reasons,
    }


def trader_recent_trades():
    """排行榜高胜率交易员的最近成交（尽力而为，可能为空）"""
    try:
        d = post(EP_QLIST, {'pageNumber': 1, 'pageSize': 20, 'timeRange': '90D', 'dataType': 'ROI',
                            'favoriteOnly': False, 'hideFull': False, 'nickname': '',
                            'order': 'DESC', 'apiKeyOnly': False}, timeout=15)
        lst = (d.get('data') or {}).get('list') or []
    except Exception:
        return []
    good = [t for t in lst
            if float(t.get('winRate') or 0) >= 55
            and int(t.get('currentCopyCount') or 0) >= 50
            and float(t.get('roi') or 0) >= 20]
    good.sort(key=lambda t: -float(t.get('winRate') or 0))
    agg = defaultdict(list)
    got = 0
    for t in good[:20]:
        pid = str(t.get('leadPortfolioId') or '')
        try:
            r = post(EP_TRADE, {'portfolioId': pid, 'timeType': '7D', 'pageNo': 1, 'pageSize': 15}, timeout=10)
            data = r.get('data') or {}
            tl = data.get('list') or []
        except Exception:
            continue
        if not tl:
            continue
        got += 1
        for x in tl[:10]:
            sym = x.get('symbol') or ''
            side = str(x.get('positionSide') or '').upper()
            if side in ('BUY', 'SELL'):
                side = 'LONG' if side == 'BUY' else 'SHORT'
            if not sym or side not in ('LONG', 'SHORT'):
                continue
            agg[sym + '|' + side].append({
                'name': t.get('nickname') or ('#' + pid[-4:]),
                'winRate': round(float(t.get('winRate') or 0), 1),
                'roi': round(float(t.get('roi') or 0), 1),
                'action': x.get('orderType') or x.get('side'),
                'price': x.get('price'),
                'qty': x.get('qty'),
                'time': x.get('time') or x.get('updateTime'),
                'symbol': sym, 'side': side,
            })
    signals = []
    for key, lst2 in agg.items():
        if len(lst2) < 2:
            continue
        sym, side = key.split('|')
        names = [x['name'] for x in lst2]
        signals.append({
            'coin': sym, 'side': side, 'count': len(set(names)),
            'winAvg': round(sum(x['winRate'] for x in lst2) / len(lst2), 1),
            'traders': lst2[:8],
            'source': 'trade-history',
        })
    signals.sort(key=lambda s: (-s['count'], -s['winAvg']))
    print('trade-history: %d 个交易员有成交记录' % got)
    return signals


def main():
    t0 = time.time()
    signals = []
    ok = 0
    for sym in COINS:
        s = whale_signal(sym)
        if s:
            signals.append(s)
            ok += 1
    signals.sort(key=lambda s: (-s['score'], s['coin']))
    trader_sigs = trader_recent_trades()

    out = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)) + ' 北京时间',
        'ts': int(time.time()),
        'scanned': len(COINS),
        'hitCount': ok,
        'signals': signals[:15],
        'traderSignals': trader_sigs[:10],
        'cost': round(time.time() - t0, 1),
    }
    with open('consensus.json', 'w', encoding='utf-8') as f_:
        json.dump(out, f_, ensure_ascii=False, indent=1)
    print('consensus done: coins=%d hit=%d traderSignals=%d cost=%.1fs'
          % (len(COINS), ok, len(trader_sigs), time.time() - t0))
    for s in signals[:8]:
        print('  %s %s score=%d top=%.2f(%+.2f) glob=%s' % (
            s['coin'], s['side'], s['score'], s['topRatio'], s['topChg'], s['globRatio']))


if __name__ == '__main__':
    main()
