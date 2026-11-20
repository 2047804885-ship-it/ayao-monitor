# -*- coding: utf-8 -*-
"""
聪明钱共识 v3 —— 只盯主流币 + 真正会推送

数据源：Gate.io USDT 永续 contract_stats（与 market.html 页面同源，CORS 开放、runner 可直连）
  top_lsr_size    大户持仓量多空比（多单占比，0.5 = 多空平衡，<0.5 偏空）
  top_lsr_account 大户账户多空比
  lsr_account     全市场(散户)账户多空比
  lsr_taker       主动成交买卖比
  long_liq_usd / short_liq_usd  24h 多空爆仓额

⚠️ v2 为什么从来没推送过（两个致命 bug）：
   1. 用 fapi.binance.com —— GitHub Actions 的 IP 被币安 451 地区封锁，**所有请求全失败**，
      所以 hitCount 恒为 0，永远没有信号，也就永远没有推送。
   2. 即使能拿到数据，门槛也是「绝对分 >= 4」，只有 top>=1.5 且 12h 变化>=0.25 这种
      极端组合才够分；而真实强度中位数只有 0.16，实测触发率 ≈ 0%。
   3. poll_market.py 里根本没有推送代码，只写 consensus.json。

v3 的做法：
   - 换回 Gate.io（和页面同源，runner 可达）
   - 门槛改成「横截面百分位」：每次扫 22 个主流币，只取强度最高的前 15%（约 3 个）
     再加散户背离 / 位置极端做确认，天然自适应市场冷热
   - 真正接上微信推送 + 6 小时去重
"""
import json
import os
import time
import ssl
import urllib.request

GATE = 'https://api.gateio.ws/api/v4/futures/usdt'
WXPUSH_API = 'https://wxpusher.zjiecode.com/api/send/message'

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0'}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ===== 只看主流币（与 market.html 的 CORE + 部分 MAJOR 对齐）=====
MAJORS = [
    'BTC_USDT', 'ETH_USDT', 'SOL_USDT', 'BNB_USDT', 'XRP_USDT', 'DOGE_USDT',
    'ADA_USDT', 'TRX_USDT', 'LINK_USDT', 'AVAX_USDT', 'LTC_USDT', 'BCH_USDT',
    'DOT_USDT', 'UNI_USDT', 'SUI_USDT', 'APT_USDT', 'NEAR_USDT', 'ARB_USDT',
    'OP_USDT', 'ATOM_USDT', 'FIL_USDT', 'ETC_USDT',
]
HEAVY = {'BTC_USDT', 'ETH_USDT', 'SOL_USDT'}   # 三大权重，信号优先展示

STATE_FILE = 'market_state.json'
DEDUP_HOURS = 6
TOP_PCT = 0.15          # 只取强度最高的前 15%
MAX_PUSH = 4            # 单次最多推送几条


def get(url, timeout=12):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def stat(sym):
    """单币结构数据；失败返回 None"""
    try:
        st = get('%s/contract_stats?contract=%s&interval=1h&limit=30' % (GATE, sym), timeout=15)
    except Exception as e:
        print('  %s 拉取失败: %s' % (sym, e))
        return None
    if not isinstance(st, list) or len(st) < 24:
        return None

    ser = [float(x['top_lsr_size']) for x in st]
    now = ser[-1]
    prev = ser[-13] if len(ser) >= 13 else ser[0]
    chg = now - prev

    # 用「小时级平均波动 × 6」做标准化单位，得到 12h 变化的相对强度
    diffs = [abs(ser[i] - ser[i - 1]) for i in range(1, len(ser))]
    unit = max((sum(diffs) / len(diffs)) * 6, 0.02)
    strength = abs(chg) / unit

    # 位置：离 0.5（多空平衡）多远
    dist = abs(now - 0.5)

    # 散户多空比（>1 散户偏多）
    try:
        glob = float(st[-1]['lsr_account'])
    except Exception:
        glob = None
    # 主动买卖比用 6 小时均值压噪（单点噪音极大）
    try:
        tk = sum(float(x['lsr_taker']) for x in st[-6:]) / 6
    except Exception:
        tk = None
    # 24h 爆仓结构：>0 空头被爆为主，<0 多头被爆为主
    ll = sum(float(x.get('long_liq_usd') or 0) for x in st[-24:])
    sl = sum(float(x.get('short_liq_usd') or 0) for x in st[-24:])
    liq_bias = ((sl - ll) / (sl + ll)) if (sl + ll) > 0 else 0.0

    try:
        px = float(st[-1]['mark_price'])
    except Exception:
        px = None

    return {
        'coin': sym, 'now': now, 'chg': chg, 'strength': round(strength, 3),
        'dist': round(dist, 3), 'glob': glob, 'taker': round(tk, 3) if tk else None,
        'liqBias': round(liq_bias, 2), 'price': px,
        'side': 'LONG' if chg > 0 else ('SHORT' if chg < 0 else None),
    }


def judge(rows):
    """横截面判定：挑出强度最高的前 15%，再用背离/位置/爆仓做确认"""
    valid = [r for r in rows if r and r['side']]
    if not valid:
        return []
    ranked = sorted(valid, key=lambda r: -r['strength'])
    n_top = max(2, int(round(len(ranked) * TOP_PCT)))
    out = []
    for r in ranked[:n_top]:
        score = 0
        reasons = []

        # 1) 相对强度（横截面）
        score += 2
        reasons.append('12h 大户持仓变动 %+.3f，在 %d 个主流币里排前 %d（%.2f 倍常态波动）'
                       % (r['chg'], len(ranked), n_top, r['strength']))

        # 2) 方向与位置一致性
        if r['side'] == 'SHORT' and r['now'] < 0.85:
            score += 1
            reasons.append('大户多单占比 %.1f%%，本身已偏空且还在减多' % (r['now'] * 100))
        elif r['side'] == 'LONG' and r['now'] > 0.6:
            score += 1
            reasons.append('大户多单占比 %.1f%%，本身偏多且还在加多' % (r['now'] * 100))

        # 3) 散户背离（最有价值的确认）
        if r['glob'] is not None:
            if r['side'] == 'SHORT' and r['glob'] >= 2.0:
                score += 2
                reasons.append('散户多空比 %.2f（散户重仓看多），大户却在撤退——典型派发' % r['glob'])
            elif r['side'] == 'LONG' and r['glob'] <= 1.3:
                score += 2
                reasons.append('散户多空比 %.2f（散户不看好），大户却在加多——筹码转移' % r['glob'])

        # 4) 爆仓结构印证
        if r['side'] == 'SHORT' and r['liqBias'] > 0.3:
            score += 1
            reasons.append('24h 爆仓以空头为主（%.0f%%），下跌中存在逼空风险'
                           % ((r['liqBias'] + 1) / 2 * 100))
        elif r['side'] == 'LONG' and r['liqBias'] < -0.3:
            score += 1
            reasons.append('24h 爆仓以多头为主（%.0f%%），反弹前多头已被清洗'
                           % ((1 - r['liqBias']) / 2 * 100))

        if score < 3:
            continue
        r['score'] = score
        r['reasons'] = reasons[:3]
        out.append(r)
    out.sort(key=lambda r: (0 if r['coin'] in HEAVY else 1, -r['score'], -r['strength']))
    return out[:MAX_PUSH]


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
    return sym.replace('_USDT', '')


def fmt_signal(s):
    color = '#0ecb81' if s['side'] == 'LONG' else '#f6465d'
    star = '⭐ ' if s['coin'] in HEAVY else ''
    side_cn = '大户在加多 🟢' if s['side'] == 'LONG' else '大户在减多/加空 🔴'
    h = ['<div style="margin:10px 0;padding:9px 11px;border-left:3px solid %s;background:#f8f9fa">'
         % color,
         '<b>%s%s</b> <span style="color:#888;font-size:12px">强度 %d</span><br>'
         % (star, cn(s['coin']), s['score']),
         '<span style="font-size:13px;color:#333">%s</span><br>' % side_cn]
    for r in s['reasons']:
        h.append('<span style="font-size:12px;color:#555">· %s</span><br>' % r)
    h.append('</div>')
    return ''.join(h)


def wxpush(signals):
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        return 'skipped (no WXPUSHER_TOKEN/WXPUSHER_UID)'
    if not signals:
        return 'nothing to push'

    body = ''.join(fmt_signal(s) for s in signals)
    hd = signals[0]
    summary = '主流币信号 %d 条：%s %s' % (
        len(signals), cn(hd['coin']), '加多' if hd['side'] == 'LONG' else '减多/加空')
    payload = {
        'appToken': token,
        'content': "<h3>📡 熬鹰 · 主流币聪明钱</h3>" + body +
                   "<p style='color:#888;font-size:12px'>监控 %d 个主流币 · %s 北京时间</p>"
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


def main():
    t0 = time.time()
    rows = []
    for sym in MAJORS:
        rows.append(stat(sym))

    okn = len([r for r in rows if r])
    signals = judge(rows)

    out = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)) + ' 北京时间',
        'ts': int(time.time()),
        'scanned': len(MAJORS),
        'ok': okn,
        'hitCount': len(signals),
        'source': 'gateio',
        'signals': signals,
        'cost': round(time.time() - t0, 1),
    }
    with open('consensus.json', 'w', encoding='utf-8') as f_:
        json.dump(out, f_, ensure_ascii=False, indent=1)

    # ---- 去重后推送 ----
    state = load_state()
    now_ts = int(time.time())
    win = DEDUP_HOURS * 3600
    fresh = [s for s in signals if now_ts - state.get(s['coin'] + '|' + s['side'], 0) > win]
    res = 'none'
    if fresh:
        res = wxpush(fresh)
        if res == 'ok':
            for s in fresh:
                state[s['coin'] + '|' + s['side']] = now_ts
            save_state(state)

    print('consensus done: coins=%d ok=%d hit=%d fresh=%d cost=%.1fs push=%s'
          % (len(MAJORS), okn, len(signals), len(fresh), time.time() - t0, res))
    for s in signals:
        print('  %s %s score=%d top=%.3f(%+.3f,%.2fx) glob=%s'
              % (s['coin'], s['side'], s['score'], s['now'], s['chg'], s['strength'], s['glob']))


if __name__ == '__main__':
    main()
