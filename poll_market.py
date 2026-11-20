# -*- coding: utf-8 -*-
"""
聪明钱共识 v3 —— 只盯主流币 + 真正会推送

数据源 1（主）：币安官方「大户持仓比」聚合数据（fapi.binance.com，runner 可直连）
  - topLongShortPositionRatio：大户持仓量多空比（聪明钱方向）
  - globalLongShortAccountRatio：散户账户多空比（反向指标）
  - takerlongshortRatio：主动成交买卖比（即时动能）
数据源 2（补充）：跟单排行榜高胜率交易员的最近成交（trade-history POST 接口）
输出 consensus.json + 微信推送（WxPusher）

v3 相比 v2 的关键修复：
1. 币种收窄到 22 个主流币（原来 30 个里混了 WIF/SEI/TIA/FET/RNDR 等二线，容易噪音）
2. 门槛从「score>=4」的绝对分制，改成「百分位 + 双条件确认」：
   v2 的问题是 top 多空比常年 1.2~1.6、12h 变化多为 ±0.1，
   只有「top>=1.5 且 chg>=0.25」这种极端组合才够 4 分，实测 hitCount 长期为 0。
3. 加上微信推送 + 6 小时去重（同一币同方向 6 小时内只推一次），
   避免刷屏，也避免推送完全发不出去。
"""
import json
import os
import time
import ssl
import urllib.request
from collections import defaultdict

FAPI = 'https://fapi.binance.com/futures/data'
BAPI = 'https://www.binance.com/bapi/futures/v1'
EP_QLIST = BAPI + '/friendly/future/copy-trade/home-page/query-list'
EP_TRADE = BAPI + '/public/future/copy-trade/lead-portfolio/trade-history'
WXPUSH_API = 'https://wxpusher.zjiecode.com/api/send/message'

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0',
    'Referer': 'https://www.binance.com/',
    'Content-Type': 'application/json',
    'clienttype': 'web',
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ===== 只看主流币 =====
# 挑选标准：币安 USDT 永续里流动性最好、共识最强的一批。
# 剔除：新上线小币、meme 二线、已经边缘化的老币。
MAJORS = [
    'BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'DOGEUSDT',
    'ADAUSDT', 'TRXUSDT', 'LINKUSDT', 'AVAXUSDT', 'LTCUSDT', 'BCHUSDT',
    'DOTUSDT', 'UNIUSDT', 'SUIUSDT', 'APTUSDT', 'NEARUSDT', 'ARBUSDT',
    'OPUSDT', 'ATOMUSDT', 'FILUSDT', 'ETCUSDT',
]
# 三大权重币：单独看，它们的信号优先级最高
HEAVY = {'BTCUSDT', 'ETHUSDT', 'SOLUSDT'}

STATE_FILE = 'market_state.json'   # 推送去重状态
DEDUP_HOURS = 6                    # 同币同方向 6 小时内不重复推


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
    """大户 vs 散户的结构判断，返回 None 或信号 dict

    v3 打分思路（相对分，不是绝对分）：
      - 大户多空比相对 26 小时前的变化幅度（标准化）= 主因子
      - 大户与散户的方向背离 = 加分项
      - 主动买卖盘 = 即时动能确认
      - 只有同时满足「方向一致 + 至少两项确认」才出信号
    """
    try:
        top = get('%s/topLongShortPositionRatio?symbol=%s&period=1h&limit=26' % (FAPI, sym))
        glob = get('%s/globalLongShortAccountRatio?symbol=%s&period=1h&limit=26' % (FAPI, sym))
        taker = get('%s/takerlongshortRatio?symbol=%s&period=1h&limit=8' % (FAPI, sym))
    except Exception as e:
        print('  %s 拉取失败: %s' % (sym, e))
        return None
    if not isinstance(top, list) or len(top) < 25:
        return None

    series = [f(x['longShortRatio']) for x in top if f(x['longShortRatio']) is not None]
    if len(series) < 24:
        return None

    top_now = series[-1]
    top_prev = series[-13]          # 12 小时前
    top_chg = round(top_now - top_prev, 3)

    # 相对变化率：用 26 小时的历史波动做标准化，避免"绝对阈值"卡死
    diffs = [abs(series[i] - series[i - 1]) for i in range(1, len(series))]
    avg_move = (sum(diffs) / len(diffs)) if diffs else 0
    # 12h 变化相当于多少倍的"小时级平均波动"
    vol_unit = max(avg_move * 6, 0.05)   # 下限 0.05，防止静市时过度敏感
    strength = abs(top_chg) / vol_unit

    glob_now = f(glob[-1]['longShortRatio']) if isinstance(glob, list) and glob else None
    tk_now = f(taker[-1]['buySellRatio']) if isinstance(taker, list) and taker else None

    side = None
    if top_chg > 0:
        side = 'LONG'
    elif top_chg < 0:
        side = 'SHORT'
    if side is None:
        return None

    score = 0
    reasons = []

    # ---- 1) 主因子：大户持仓结构的变化强度 ----
    if strength >= 3.0:
        score += 3
        reasons.append('12h 内大户多空比 %+.2f（%.1f 倍常态波动），主力明显加仓' % (top_chg, strength))
    elif strength >= 1.8:
        score += 2
        reasons.append('12h 内大户多空比 %+.2f（%.1f 倍常态波动），资金在动' % (top_chg, strength))
    elif strength >= 1.2:
        score += 1
        reasons.append('12h 内大户多空比 %+.2f，略有异动' % top_chg)
    else:
        return None      # 波动不够，直接不看作信号

    # ---- 2) 绝对位置：不要逆着大结构做 ----
    if side == 'LONG' and top_now >= 1.2:
        score += 1
        reasons.append('大户持仓量多空比 %.2f，本身偏多' % top_now)
    elif side == 'SHORT' and top_now <= 0.95:
        score += 1
        reasons.append('大户持仓量多空比 %.2f，本身偏空' % top_now)

    # ---- 3) 散户背离：大户加多但散户在做空（或反之）----
    if glob_now is not None:
        if side == 'LONG' and glob_now < 1.0:
            score += 2
            reasons.append('散户账户多空比 %.2f（散户偏空），与大户加多方向相反' % glob_now)
        elif side == 'SHORT' and glob_now > 1.3:
            score += 2
            reasons.append('散户账户多空比 %.2f（散户偏多），与大户加空方向相反' % glob_now)

    # ---- 4) 主动成交确认 ----
    if tk_now is not None:
        if side == 'LONG' and tk_now >= 1.05:
            score += 1
            reasons.append('主动买盘占优 %.2f' % tk_now)
        elif side == 'SHORT' and tk_now <= 0.95:
            score += 1
            reasons.append('主动卖盘占优 %.2f' % tk_now)

    # 门槛：至少 3 分（=强波动 或 中等波动+一项确认）
    if score < 3:
        return None

    return {
        'coin': sym, 'side': side, 'score': score,
        'topRatio': top_now, 'topChg': top_chg, 'strength': round(strength, 2),
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
            # 只留主流币的交易员动作
            if sym not in MAJORS:
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


# ================= 推送 =================
def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f_:
            return json.load(f_)
    except Exception:
        return {}


def save_state(st):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f_:
            json.dump(st, f_, ensure_ascii=False)
    except Exception as e:
        print('save_state err:', e)


def cn(sym):
    return sym.replace('USDT', '')


def fmt_market_signal(s):
    side_cn = '做多 🟢' if s['side'] == 'LONG' else '做空 🔴'
    star = '⭐' if s['coin'] in HEAVY else ''
    lines = [
        "<div style='margin:10px 0;padding:9px 11px;border-left:3px solid %s;background:#f8f9fa'>"
        % ('#0ecb81' if s['side'] == 'LONG' else '#f6465d'),
        "<b>%s%s %s</b> <span style='color:#888;font-size:12px'>强度 %d</span><br>"
        % (star, cn(s['coin']), side_cn, s['score']),
    ]
    for r in s['reasons'][:3]:
        lines.append("<span style='font-size:13px;color:#333'>· %s</span><br>" % r)
    lines.append("</div>")
    return ''.join(lines)


def wxpush_market(signals, trader_sigs):
    """把主流币机会信号推到微信"""
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        return 'skipped (no WXPUSHER_TOKEN/WXPUSHER_UID)'

    if not signals and not trader_sigs:
        return 'nothing to push'

    body = []
    if signals:
        body.append("<h3>📡 主流币机会信号</h3>")
        for s in signals[:5]:
            body.append(fmt_market_signal(s))
    if trader_sigs:
        body.append("<h3>👥 高胜率交易员动向</h3>")
        for t in trader_sigs[:3]:
            side_cn = '做多 🟢' if t['side'] == 'LONG' else '做空 🔴'
            body.append(
                "<div style='margin:8px 0;font-size:13px'>"
                "<b>%s %s</b> · %d 位交易员同向 · 平均胜率 %.0f%%</div>"
                % (cn(t['coin']), side_cn, t['count'], t['winAvg']))

    head = signals[0] if signals else trader_sigs[0]
    summary = '主流币信号 %d 条：%s %s' % (
        len(signals) + len(trader_sigs), cn(head['coin']),
        '多' if head['side'] == 'LONG' else '空')

    payload = {
        'appToken': token,
        'content': "<h3>📡 熬鹰 · 主流币机会</h3>" + ''.join(body) +
                   "<p style='color:#888;font-size:12px'>只监控 %d 个主流币 · %s</p>"
                   % (len(MAJORS), time.strftime('%m-%d %H:%M', time.localtime(time.time() + 8 * 3600))),
        'summary': summary[:95],
        'contentType': 3,
        'uids': [uid],
        'url': 'https://2047804885-ship-it.github.io/ayao-monitor/market.html',
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


def dedup(signals, state):
    """同一币同方向 DEDUP_HOURS 内只推一次；返回需要推送的新信号"""
    now = int(time.time())
    win = DEDUP_HOURS * 3600
    fresh = []
    for s in signals:
        key = s['coin'] + '|' + s['side']
        last = state.get(key, 0)
        if now - last > win:
            fresh.append(s)
            s['_key'] = key
    return fresh


def main():
    t0 = time.time()
    signals = []
    ok = 0
    for sym in MAJORS:
        s = whale_signal(sym)
        if s:
            signals.append(s)
            ok += 1
    # 核心币优先、强度高优先
    signals.sort(key=lambda s: (
        0 if s['coin'] in HEAVY else 1,
        -s['score'], -abs(s['topChg'])))

    trader_sigs = trader_recent_trades()

    out = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)) + ' 北京时间',
        'ts': int(time.time()),
        'scanned': len(MAJORS),
        'hitCount': ok,
        'signals': signals[:15],
        'traderSignals': trader_sigs[:10],
        'cost': round(time.time() - t0, 1),
    }
    with open('consensus.json', 'w', encoding='utf-8') as f_:
        json.dump(out, f_, ensure_ascii=False, indent=1)

    # ---- 推送 ----
    state = load_state()
    fresh = dedup(signals, state)
    fresh_trader = [t for t in trader_sigs if t['coin'] in HEAVY][:3]
    push_res = 'none'
    if fresh or fresh_trader:
        push_res = wxpush_market(fresh, fresh_trader)
        if push_res == 'ok':
            now = int(time.time())
            for s in fresh:
                state[s['_key']] = now
            save_state(state)

    print('consensus done: coins=%d hit=%d fresh=%d traderSignals=%d cost=%.1fs push=%s'
          % (len(MAJORS), ok, len(fresh), len(trader_sigs), time.time() - t0, push_res))
    for s in signals[:8]:
        print('  %s %s score=%d top=%.2f(%+.2f,%.1fx) glob=%s'
              % (s['coin'], s['side'], s['score'], s['topRatio'], s['topChg'], s['strength'], s['globRatio']))


if __name__ == '__main__':
    main()
