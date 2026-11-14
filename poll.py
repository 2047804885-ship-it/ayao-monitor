# -*- coding: utf-8 -*-
"""Ayao copy-trade poller for GitHub Actions.
Reads previous snapshot from snap.json, polls Binance public bapi,
computes events (position diff + stat jumps), writes updated snap.json.
"""
import json, os, ssl, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta

PID_FILE = 'config.json'
SNAP_FILE = 'snap.json'
CST = timezone(timedelta(hours=8))

BAPI = 'https://www.binance.com/bapi/futures/v1'
EP_DETAIL = BAPI + '/friendly/future/copy-trade/lead-portfolio/detail?portfolioId='
EP_QLIST = BAPI + '/friendly/future/copy-trade/home-page/query-list'
EP_POS = BAPI + '/friendly/future/copy-trade/lead-data/positions?portfolioId='
EP_TRADE = BAPI + '/public/future/copy-trade/lead-portfolio/trade-history'
UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36',
      'Referer': 'https://www.binance.com/', 'Origin': 'https://www.binance.com',
      'Content-Type': 'application/json'}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def getJSON(url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=UA)
    raw = urllib.request.urlopen(req, timeout=20, context=CTX).read().decode('utf-8', 'ignore')
    return json.loads(raw)


def parsePID(text):
    import re
    m = re.search(r'lead-portfolio/(\d+)', text) or re.search(r'lead-details/(\d+)', text) or re.search(r'(\d{5,})', text)
    return m.group(1) if m else text


def searchQList(nickname):
    return getJSON(EP_QLIST, {'pageNumber': 1, 'pageSize': 10, 'timeRange': '90D', 'dataType': 'ROI',
                              'favoriteOnly': False, 'hideFull': False, 'nickname': nickname,
                              'order': 'DESC', 'apiKeyOnly': False})


def normQList(raw, pid):
    lst = ((raw or {}).get('data') or {}).get('list') or []
    hit = None
    for it in lst:
        if str(it.get('leadPortfolioId')) == pid:
            hit = it
            break
        if not hit and '熬鹰' in str(it.get('nickname') or ''):
            hit = it
    if not hit:
        return None
    return {'pid': str(hit['leadPortfolioId']), 'name': hit.get('nickname'),
            'roi': hit.get('roi'), 'pnl': hit.get('pnl'), 'aum': hit.get('aum'),
            'mdd': hit.get('mdd'), 'winRate': hit.get('winRate'), 'sharpe': hit.get('sharpRatio'),
            'copierPnl': hit.get('copierPnl'), 'chartItems': hit.get('chartItems') or []}


def normPositions(raw):
    d = (raw or {}).get('data')
    if not isinstance(d, list):
        return []
    return [{'symbol': p.get('symbol') or '?',
             'side': str(p.get('positionSide') or p.get('side') or '?'),
             'lever': p.get('leverage') or '',
             'qty': float(p.get('positionAmount') or p.get('positionAmt') or 0),
             'entry': p.get('entryPrice'), 'mark': p.get('markPrice'),
             'upnl': p.get('unrealizedProfit') or p.get('unRealizedProfit'),
             'upnlPct': p.get('unrealizedRoeRate') or p.get('roeRate')} for p in d]


def diffPositions(oldL, newL):
    evs = []
    key = lambda p: p['symbol'] + '|' + p['side']
    o = {key(p): p for p in oldL}
    n = {key(p): p for p in newL}
    now = datetime.now(CST).strftime('%m-%d %H:%M:%S')
    for k, p in n.items():
        if k not in o:
            evs.append({'time': now, 'type': '开仓', 'symbol': p['symbol'], 'side': p['side'],
                        'lever': p['lever'], 'qty': p['qty'], 'price': p['mark']})
        elif abs(p['qty'] - o[k]['qty']) > 1e-9:
            evs.append({'time': now, 'type': '加仓' if p['qty'] > o[k]['qty'] else '减仓',
                        'symbol': p['symbol'], 'side': p['side'], 'lever': p['lever'],
                        'qty': p['qty'], 'price': p['mark']})
    for k, p in o.items():
        if k not in n:
            evs.append({'time': now, 'type': '平仓', 'symbol': p['symbol'], 'side': p['side'],
                        'lever': p['lever'], 'qty': 0, 'price': p['mark']})
    return evs


STAT_NOISE = ('盈亏变动', '收益率变动', '保证金变动')  # filtered out per user request

WXPUSH_API = 'https://wxpusher.zjiecode.com/api/send/message'
ICON = {'开仓': '🟢', '平仓': '🔴', '加仓': '🔵', '减仓': '🟠', '最新交易': '⏱'}


def fmt_event(e):
    icon = ICON.get(e['type'], '📣')
    t = e.get('type', '')
    if t == '最新交易':
        return f"{icon} <b>[{t}]</b> {e.get('time','')} 熬鹰有新的成交记录，详情见监控页"
    return (f"{icon} <b>[{t}]</b> {e.get('symbol','')} {e.get('side','')}"
            + (f" {e.get('lever','')}x" if e.get('lever') else '')
            + (f" 数量:{e.get('qty')}" if e.get('qty') not in (None, '', 0) else '')
            + (f" 价格:{e.get('price')}" if e.get('price') else '')
            + f"　({e.get('time','')})")


def wxpush(evs, name='熬鹰资本'):
    """Push new trade events to WeChat via WxPusher service account."""
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        return 'wxpush skipped (no WXPUSHER_TOKEN/WXPUSHER_UID)'
    body = ''.join(fmt_event(e) for e in evs[:10])
    payload = {
        'appToken': token,
        'content': f"<h3>📣 {name} 交易动态</h3>{body}<p style='color:#888'>共 {len(evs)} 条新事件 · 5 分钟内检测到</p>",
        'summary': f"{name} 新交易动态 {len(evs)} 条: " + ' / '.join(
            e.get('type', '') + ' ' + e.get('symbol', '') for e in evs[:3])[:95],
        'contentType': 3,  # html
        'uids': [uid],
        'url': 'https://ayao-monitor.jikui-ayao.workers.dev',
    }
    req = urllib.request.Request(WXPUSH_API, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15, context=CTX).read().decode())
        ok = resp.get('code') == 1000
        print('wxpush:', resp.get('code'), resp.get('msg'))
        return 'ok' if ok else 'fail:' + str(resp.get('msg'))
    except Exception as e:
        print('wxpush error:', e)
        return 'err:' + str(e)[:80]


def main():
    cfg = {}
    if os.path.exists(PID_FILE):
        try:
            cfg = json.load(open(PID_FILE, encoding='utf-8'))
        except Exception:
            cfg = {}
    pid = str(cfg.get('portfolioId') or '5075281354358777856')
    name = cfg.get('name') or 'allincrypto 熬鹰资本'

    try:
        snap = json.load(open(SNAP_FILE, encoding='utf-8'))
    except Exception:
        snap = {}
    errs = []

    detail = {}
    try:
        raw = getJSON(EP_DETAIL + pid)
        d = (raw or {}).get('data') or {}
        detail = {'marginBalance': d.get('marginBalance'), 'aumAmount': d.get('aumAmount'),
                  'lastTradeTime': d.get('lastTradeTime'), 'status': d.get('status'),
                  'positionShow': d.get('positionShow'), 'badgeName': d.get('badgeName'),
                  'currentCopyCount': d.get('currentCopyCount'), 'maxCopyCount': d.get('maxCopyCount')}
        if d.get('leadPortfolioId'):
            pid = str(d['leadPortfolioId'])
    except Exception as e:
        errs.append('detail:' + str(e)[:80])

    q = None
    for kw in [name, '熬鹰']:
        if not kw:
            continue
        try:
            q = normQList(searchQList(kw), pid)
            if q:
                break
        except Exception as e:
            errs.append('qlist:' + str(e)[:80])
    if q:
        pid = q['pid']
        name = q['name'] or name
        chart = q.pop('chartItems')
    else:
        chart = snap.get('curve') or []

    pos = []
    try:
        pos = normPositions(getJSON(EP_POS + pid))
    except Exception as e:
        errs.append('pos:' + str(e)[:80])

    newprof = dict(detail)
    if q:
        newprof.update({k: v for k, v in q.items() if k != 'chartItems' and v is not None})

    evs = diffPositions(snap.get('positions') or [], pos)
    # also diff lastTradeTime: if it advanced, note a "有新交易" hint
    old_lt = (snap.get('profile') or {}).get('lastTradeTime')
    new_lt = newprof.get('lastTradeTime')
    if isinstance(old_lt, (int, float)) and isinstance(new_lt, (int, float)) and new_lt > old_lt:
        t = datetime.fromtimestamp(new_lt / 1000, CST).strftime('%m-%d %H:%M')
        evs.append({'time': t, 'type': '最新交易', 'symbol': '', 'side': '', 'qty': ''})

    newsnap = {
        'portfolioId': pid, 'name': name,
        'profile': newprof,
        'curve': chart,
        'positions': pos,
        'events': [e for e in (snap.get('events') or []) if e.get('type') not in STAT_NOISE]
                  + evs,
        'lastError': ' | '.join(errs)[:200],
        'lastOk': '' if errs else datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
        'serverTime': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
    }
    # keep events list capped
    newsnap['events'] = newsnap['events'][:200]
    with open(SNAP_FILE, 'w', encoding='utf-8') as f:
        json.dump(newsnap, f, ensure_ascii=False)
    cfg = {'portfolioId': pid, 'name': name}
    with open(PID_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)
    print('poll done. errors:', newsnap['lastError'] or 'none')
    print('events added:', len(evs))
    if evs:  # only trade events are in evs now -> push each new event to WeChat
        print('wxpush result:', wxpush(evs, name))


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--test-push':
        r = wxpush([{'time': datetime.now(CST).strftime('%m-%d %H:%M'), 'type': '最新交易',
                     'symbol': 'TEST', 'side': 'LONG', 'lever': '20', 'qty': '0.5'}], '推送测试')
        print('test push:', r)
        sys.exit(0)
    main()
