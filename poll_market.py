# -*- coding: utf-8 -*-
"""
聪明钱共识：聚合币安跟单市场高胜率带单员的实时持仓
跑在 GitHub Actions（runner 可直连币安），输出 consensus.json
共识定义：≥3 个「胜率≥55%、跟单人数≥50、90D ROI≥20%」的交易员
在同一币种、同一方向上同时持仓
"""
import json
import time
import urllib.request
import ssl
from collections import defaultdict

BAPI = 'https://www.binance.com/bapi/futures/v1'
EP_QLIST = BAPI + '/friendly/future/copy-trade/home-page/query-list'
EP_POS = BAPI + '/friendly/future/copy-trade/lead-data/positions?portfolioId='

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36',
    'Referer': 'https://www.binance.com/',
    'Origin': 'https://www.binance.com',
    'Content-Type': 'application/json',
    'clienttype': 'web',
}

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 筛选门槛：什么才算"值得跟随的交易员"
MIN_WINRATE = 55.0
MIN_COPIERS = 50
MIN_ROI = 20.0
MAX_TRADERS = 45          # 最多拉多少个交易员的持仓
CONSENSUS_MIN = 3         # 同币同向至少几个人才算共识


def req(url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def bj_time():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)) + ' 北京时间'


def fetch_leaderboard(pages=3):
    """拉跟单排行榜（90D ROI 降序）"""
    traders = []
    for page in range(1, pages + 1):
        try:
            r = req(EP_QLIST, {
                'pageNumber': page, 'pageSize': 20, 'timeRange': '90D',
                'dataType': 'ROI', 'favoriteOnly': False, 'hideFull': False,
                'nickname': '', 'order': 'DESC', 'apiKeyOnly': False,
            })
            lst = (r.get('data') or {}).get('list') or []
            traders += lst
            if not lst:
                break
        except Exception:
            continue
    return traders


def qualify(t):
    try:
        wr = float(t.get('winRate') or 0)
        cc = int(t.get('currentCopyCount') or 0)
        roi = float(t.get('roi') or 0)
    except Exception:
        return False
    return wr >= MIN_WINRATE and cc >= MIN_COPIERS and roi >= MIN_ROI


def fetch_positions(pid):
    """拉单个交易员的当前持仓；隐藏持仓返回 None"""
    try:
        r = req(EP_POS + str(pid), timeout=12)
        data = r.get('data')
        if not isinstance(data, list):
            return None
        return data
    except Exception:
        return None


def main():
    t0 = time.time()
    board = fetch_leaderboard()
    seen = {}
    for t in board:
        pid = str(t.get('leadPortfolioId') or '')
        if pid and pid not in seen:
            seen[pid] = t

    good = []
    for pid, t in seen.items():
        if qualify(t):
            good.append({
                'pid': pid,
                'name': t.get('nickname') or ('#' + pid[-4:]),
                'winRate': round(float(t.get('winRate') or 0), 1),
                'copiers': int(t.get('currentCopyCount') or 0),
                'roi': round(float(t.get('roi') or 0), 1),
                'pnl': t.get('pnl'),
                'positionShow': bool(t.get('positionShow', True)),
            })
    good.sort(key=lambda x: -x['winRate'])

    cons = defaultdict(list)
    public_count = 0
    for g in good[:MAX_TRADERS]:
        pos = fetch_positions(g['pid'])
        if pos is None:
            g['hidden'] = True
            continue
        if not pos:
            g['hidden'] = False
            g['empty'] = True
            continue
        public_count += 1
        g['hidden'] = False
        for p in pos:
            sym = p.get('symbol') or ''
            side = str(p.get('positionSide') or p.get('side') or '').upper()
            if not sym or side not in ('LONG', 'SHORT', 'BUY', 'SELL'):
                continue
            if side in ('BUY', 'SELL'):
                side = 'LONG' if side == 'BUY' else 'SHORT'
            cons[sym + '|' + side].append({
                'name': g['name'], 'winRate': g['winRate'], 'roi': g['roi'],
                'copiers': g['copiers'],
                'lever': p.get('lever'),
                'entry': p.get('entryPrice'),
                'mark': p.get('markPrice'),
            })

    signals = []
    for key, lst in cons.items():
        if len(lst) < CONSENSUS_MIN:
            continue
        sym, side = key.split('|')
        levers = [float(x.get('lever') or 0) for x in lst if x.get('lever')]
        entries = [float(x.get('entry') or 0) for x in lst if x.get('entry')]
        marks = [float(x.get('mark') or 0) for x in lst if x.get('mark')]
        signals.append({
            'coin': sym,
            'side': side,
            'count': len(lst),
            'winAvg': round(sum(x['winRate'] for x in lst) / len(lst), 1),
            'roiAvg': round(sum(x['roi'] for x in lst) / len(lst), 1),
            'leverAvg': round(sum(levers) / len(levers), 1) if levers else None,
            'entryAvg': round(sum(entries) / len(entries), 6) if entries else None,
            'markAvg': round(sum(marks) / len(marks), 6) if marks else None,
            'traders': lst[:10],
        })
    signals.sort(key=lambda s: (-s['count'], -s['winAvg']))

    out = {
        'time': bj_time(),
        'ts': int(time.time()),
        'scanned': len(seen),
        'qualified': len(good),
        'publicPos': public_count,
        'consensusMin': CONSENSUS_MIN,
        'signals': signals,
        'traders': good[:MAX_TRADERS],
        'cost': round(time.time() - t0, 1),
    }
    with open('consensus.json', 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('consensus done: scanned=%d qualified=%d public=%d signals=%d cost=%.1fs'
          % (len(seen), len(good), public_count, len(signals), time.time() - t0))
    for s in signals[:6]:
        print('  %s %s x%d 胜率均值%.1f%%' % (s['coin'], s['side'], s['count'], s['winAvg']))


if __name__ == '__main__':
    main()
