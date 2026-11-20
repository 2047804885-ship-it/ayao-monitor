# -*- coding: utf-8 -*-
"""
主流币机会信号 —— 三层技术分析 + 聪明钱，投票后才推

分析架构（每层独立计算，互不参考，最后投票）：
  第 1 层【趋势】  EMA20/50/200 排列 + ADX 趋势强度 + 价格位置
  第 2 层【动量】  MACD 柱状转折 + RSI 背离 + 量能确认
  第 3 层【结构】  布林带 %B + 摆动高低点聚类支撑阻力 + HH/HL 结构 + ATR
  第 4 票【聪明钱】Gate 大户持仓结构变化 + 散户背离 + 爆仓结构

投票规则：
  - 至少 2 层同向，且反方不超过正方
  - 综合置信度 >= 45
  - 必须有至少一项硬确认（有效趋势 / 放量 / 聪明钱）
  - 1d 大周期方向用来过滤反向信号（顺大势做）

数据源：Gate.io（与 market.html 同源，runner 可达）
⚠️ 币安 fapi 对 GitHub Actions IP 返回 HTTP 451 地区封锁，绝不能用。
"""
import json
import os
import time
import ssl
import urllib.request

import ta_engine as ta

GATE = 'https://api.gateio.ws/api/v4/futures/usdt'
WXPUSH_API = 'https://wxpusher.zjiecode.com/api/send/message'

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0'}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ===== 只看主流币 =====
MAJORS = [
    'BTC_USDT', 'ETH_USDT', 'SOL_USDT', 'BNB_USDT', 'XRP_USDT', 'DOGE_USDT',
    'ADA_USDT', 'TRX_USDT', 'LINK_USDT', 'AVAX_USDT', 'LTC_USDT', 'BCH_USDT',
    'DOT_USDT', 'UNI_USDT', 'SUI_USDT', 'APT_USDT', 'NEAR_USDT', 'ARB_USDT',
    'OP_USDT', 'ATOM_USDT', 'FIL_USDT', 'ETC_USDT',
]
HEAVY = {'BTC_USDT', 'ETH_USDT', 'SOL_USDT'}

STATE_FILE = 'market_state.json'
DEDUP_HOURS = 6
MAX_PUSH = 4


def get(url, timeout=15):
    r = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(r, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode('utf-8'))


def klines(sym, itv='4h', lim=260):
    d = get('%s/candlesticks?contract=%s&interval=%s&limit=%d' % (GATE, sym, itv, lim), timeout=20)
    return [{'t': int(x['t']), 'o': float(x['o']), 'h': float(x['h']),
             'l': float(x['l']), 'c': float(x['c']), 'v': float(x['v'])} for x in d]


def stat(sym):
    """持仓结构（聪明钱那一票）"""
    try:
        st = get('%s/contract_stats?contract=%s&interval=1h&limit=30' % (GATE, sym), timeout=15)
    except Exception as e:
        print('  %s 持仓数据失败: %s' % (sym, e))
        return None
    if not isinstance(st, list) or len(st) < 24:
        return None

    ser = [float(x['top_lsr_size']) for x in st]
    now = ser[-1]
    prev = ser[-13] if len(ser) >= 13 else ser[0]
    chg = now - prev
    diffs = [abs(ser[i] - ser[i - 1]) for i in range(1, len(ser))]
    unit = max((sum(diffs) / len(diffs)) * 6, 0.02)
    strength = abs(chg) / unit

    try:
        glob = float(st[-1]['lsr_account'])
    except Exception:
        glob = None
    try:
        tk = sum(float(x['lsr_taker']) for x in st[-6:]) / 6
    except Exception:
        tk = None
    ll = sum(float(x.get('long_liq_usd') or 0) for x in st[-24:])
    sl = sum(float(x.get('short_liq_usd') or 0) for x in st[-24:])
    liq_bias = ((sl - ll) / (sl + ll)) if (sl + ll) > 0 else 0.0

    return {
        'coin': sym, 'now': now, 'chg': chg, 'strength': round(strength, 3),
        'glob': glob, 'taker': round(tk, 3) if tk else None,
        'liqBias': round(liq_bias, 2),
        'side': 'LONG' if chg > 0 else ('SHORT' if chg < 0 else None),
        'raw': st,          # 原始 contract_stats，供庄家层使用
    }


def funding_rate(sym):
    """当前资金费率（小数）。负数=空头付钱给多头。"""
    try:
        d = get('%s/contracts/%s' % (GATE, sym), timeout=12)
        return float(d.get('funding_rate'))
    except Exception:
        return None


def judge_smart(rows):
    """横截面挑强度前 15%，再用背离/位置/爆仓确认 —— 作为第 4 票"""
    valid = [r for r in rows if r and r['side']]
    if not valid:
        return {}
    ranked = sorted(valid, key=lambda r: -r['strength'])
    n_top = max(2, int(round(len(ranked) * 0.15)))
    out = {}
    for r in ranked[:n_top]:
        score = 0
        reasons = []
        score += 2
        reasons.append('12h 大户持仓变动 %+.3f，在 %d 个主流币里排前 %d（%.2f 倍常态波动）'
                       % (r['chg'], len(ranked), n_top, r['strength']))
        if r['side'] == 'SHORT' and r['now'] < 0.85:
            score += 1
            reasons.append('大户多单占比 %.1f%%，本身已偏空且还在减多' % (r['now'] * 100))
        elif r['side'] == 'LONG' and r['now'] > 0.6:
            score += 1
            reasons.append('大户多单占比 %.1f%%，本身偏多且还在加多' % (r['now'] * 100))
        if r['glob'] is not None:
            if r['side'] == 'SHORT' and r['glob'] >= 2.0:
                score += 2
                reasons.append('散户多空比 %.2f（重仓看多），大户却在撤退——典型派发' % r['glob'])
            elif r['side'] == 'LONG' and r['glob'] <= 1.3:
                score += 2
                reasons.append('散户多空比 %.2f（不看好），大户却在加多——筹码转移' % r['glob'])
        if r['side'] == 'SHORT' and r['liqBias'] > 0.3:
            score += 1
            reasons.append('24h 爆仓以空头为主（%.0f%%），存在逼空风险'
                           % ((r['liqBias'] + 1) / 2 * 100))
        elif r['side'] == 'LONG' and r['liqBias'] < -0.3:
            score += 1
            reasons.append('24h 爆仓以多头为主（%.0f%%），反弹前多头已被清洗'
                           % ((1 - r['liqBias']) / 2 * 100))
        if score >= 3:
            r['score'] = score
            r['reasons'] = reasons[:3]
            out[r['coin']] = r
    return out


def analyze_coin(sym, smart, dealer_raw=None, funding=None):
    """对一个币做完整五层 TA + 右侧优先投票"""
    try:
        cs4 = klines(sym, '4h', 260)
    except Exception as e:
        print('  %s K线失败: %s' % (sym, e))
        return None
    if len(cs4) < 60:
        return None

    L1 = ta.layer_trend(cs4)
    L2 = ta.layer_momentum(cs4)
    L3 = ta.layer_structure(cs4)
    L4 = ta.layer_volprice(cs4)
    layers = [L1, L2, L3, L4]

    # 缠论层（分型/笔/中枢/背驰）——回测与趋势共振 73.4%，全场最高
    LC = None
    try:
        LC = ta.layer_chan(cs4)
    except Exception as e:
        print('  %s 缠论层失败: %s' % (sym, e))
    if LC:
        layers.append(LC)

    # 订单流层（1m K 线构造主动买卖压力）——实时资金行为
    LO = None
    try:
        m1 = klines(sym, '1m', 400)
        if len(m1) >= 120:
            LO = ta.layer_orderflow(m1)
    except Exception as e:
        print('  %s 订单流层失败: %s' % (sym, e))
    if LO:
        layers.append(LO)

    # 庄家层（资金面实时情报，历史无法回测但信息价值高）
    LD = None
    if dealer_raw:
        try:
            LD = ta.layer_dealer(dealer_raw, funding=funding)
        except Exception as e:
            print('  %s 庄家层失败: %s' % (sym, e))
    if LD:
        layers.append(LD)

    v = ta.vote(layers, smart=smart.get(sym))
    if not v:
        return None

    # ---- 日线大周期共振过滤（强化右侧交易）----
    trend_1d = None
    try:
        cs1d = klines(sym, '1d', 220)
        if len(cs1d) >= 60:
            t1 = ta.layer_trend(cs1d)
            if t1:
                trend_1d = t1['side']
    except Exception:
        pass

    # 右侧：日线方向必须一致或中性，逆日线一律不推
    if v['tradeType'] == '右侧' and trend_1d and trend_1d != 'NEUTRAL':
        if trend_1d != v['side']:
            print('  %s 右侧信号但逆日线(%s vs %s)，跳过' % (sym, v['side'], trend_1d))
            return None

    # 左侧：允许逆日线，但必须是极可靠的衰竭信号（已在 vote 里卡过）
    if trend_1d and trend_1d != 'NEUTRAL' and trend_1d != v['side']:
        v['counterTrend'] = True

    px = cs4[-1]['c']
    a14 = ta.atr([c['h'] for c in cs4], [c['l'] for c in cs4], [c['c'] for c in cs4], 14)
    atr_pct = a14 / px * 100 if px else 0

    return {
        'coin': sym,
        'side': v['side'],
        'confidence': v['confidence'],
        'votes': v['votes'],
        'tradeType': v.get('tradeType', '右侧'),
        'rightSide': v.get('rightSide', True),
        'grade': v.get('grade', 'C'),
        'chanAgree': v.get('chanAgree', False),
        'chanAgainst': v.get('chanAgainst', False),
        'agreeLayers': v['agreeLayers'],
        'opposeLayers': v['opposeLayers'],
        'neutralLayers': v.get('neutralLayers', []),
        'confirm': v['confirm'],
        'counterTrend': v.get('counterTrend', False),
        'trend1d': trend_1d,
        'price': px,
        'atrPct': round(atr_pct, 2),
        'layers': [x for x in layers if x],
        'smart': smart.get(sym),
        'dealer': LD,
        'chan': LC,
        'of': LO,
    }


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


def esc(t):
    """HTML 转义（保留 < > 的语义，避免被当成标签），同时压缩多余空格"""
    return (str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def fmt_signal(s):
    """大结论在前，分析小字在后"""
    is_long = s['side'] == 'LONG'
    color = '#0ecb81' if is_long else '#f6465d'
    bg = '#f0fdf6' if is_long else '#fef2f3'
    star = '⭐ ' if s['coin'] in HEAVY else ''
    side_cn = '看多' if is_long else '看空'
    arrow = '▲' if is_long else '▼'

    h = ['<div style="margin:12px 0;padding:11px 13px;border-left:4px solid %s;'
         'background:%s;border-radius:4px;word-break:break-word;overflow-wrap:anywhere">'
         % (color, bg)]

    # ---- 主观结论（大字）----
    ttype = s.get('tradeType', '右侧')
    if ttype == '右侧':
        ttag = ('<span style="font-size:11px;font-weight:600;color:#0b7a4b;'
                'background:#d9f7e8;padding:1px 5px;border-radius:3px;'
                'margin-left:6px;vertical-align:middle">右侧顺势</span>')
    else:
        ttag = ('<span style="font-size:11px;font-weight:600;color:#8a5a00;'
                'background:#fff3cd;padding:1px 5px;border-radius:3px;'
                'margin-left:6px;vertical-align:middle">左侧逆势·高风险</span>')

    # ---- 质量等级（回测标定：A/B 才有缠论共振，C 无）----
    gr = s.get('grade', 'C')
    GRD = {
        'A':  ('A 级', '#0b7a4b', '#d9f7e8'),   # 缠论共振 + ADX≥45  回测 76.5%
        'B':  ('B 级', '#0369a1', '#e0f2fe'),   # 缠论共振            回测 69.4%
        'C':  ('C 级', '#b45309', '#fef3c7'),   # 缠论明确反对
        'C-': ('C- 级', '#9a3412', '#ffedd5'),  # 缠论无表态（回测 52%，差）
        'L':  ('左侧特批', '#8a5a00', '#fff3cd'),
    }
    gtxt, gcol, gbg = GRD.get(gr, GRD['C'])
    gtag = ('<span style="font-size:11px;font-weight:600;color:%s;'
            'background:%s;padding:1px 5px;border-radius:3px;'
            'margin-left:6px;vertical-align:middle">%s</span>'
            % (gcol, gbg, gtxt))

    h.append('<div style="font-size:17px;font-weight:700;color:%s;margin-bottom:4px">'
             '%s%s  %s %s%s%s</div>'
             % (color, arrow, star, cn(s['coin']), side_cn, ttag, gtag))

    px_txt = ('%.6g' % s['price']) if s['price'] else '—'
    h.append('<div style="font-size:13px;color:#333;margin-bottom:6px">'
             '现价 %s · 置信度 <b>%d</b> · %d 层投票 <b>%s</b> 一致'
             % (px_txt, s['confidence'], len(s['layers']), s['votes']))
    if s.get('counterTrend'):
        h.append(' · <span style="color:#f59e0b">逆日线</span>')
    h.append('</div>')

    # ---- 一致层的核心结论（按权重从高到低，缠论/订单流优先露头）----
    ORDER = ['趋势', '缠论', '结构', '订单流', '庄家', '量价', '动量']
    h.append('<div style="font-size:13px;color:#111;margin-bottom:6px">')
    for name in ORDER:
        for L in s['layers']:
            if L['layer'] != name:
                continue
            if name not in s['agreeLayers']:
                continue
            # 缠论/订单流：取最有信息量的整段（中枢位置、背驰、主动买占比）
            if name in ('缠论', '订单流'):
                head = '；'.join(L['notes'][:3])
            else:
                head = L['notes'][0] if L['notes'] else ''
            h.append('<div style="margin:1px 0">• <b>%s</b>：%s</div>'
                     % (name, esc(head)))
    if s.get('smart') and '聪明钱' in s['agreeLayers']:
        h.append('<div style="margin:1px 0">• <b>聪明钱</b>：%s</div>'
                 % esc(s['smart']['reasons'][0]))
    h.append('</div>')

    # ---- 风险参考 ----
    if s['side'] == 'SHORT':
        h.append('<div style="font-size:12px;color:#666">止损参考：现价上方 %.1f%%（1.5×ATR）</div>'
                 % (s['atrPct'] * 1.5))
    else:
        h.append('<div style="font-size:12px;color:#666">止损参考：现价下方 %.1f%%（1.5×ATR）</div>'
                 % (s['atrPct'] * 1.5))

    # ---- 分析明细（小字，折叠在最后）----
    h.append('<div style="margin-top:9px;padding-top:7px;border-top:1px dashed #ddd;'
             'font-size:11px;color:#888;line-height:1.7;word-break:break-word;'
             'overflow-wrap:anywhere">')
    h.append('<b>分析明细（逐层）</b><br>')
    for L in s['layers']:
        tag = ('✓' if L['layer'] in s['agreeLayers']
               else ('✗' if L['layer'] in s['opposeLayers'] else '—'))
        col = ('#16a34a' if tag == '✓' else ('#dc2626' if tag == '✗' else '#9ca3af'))
        h.append('<span style="color:%s"><b>%s %s层</b></span>：' % (col, tag, L['layer']))
        h.append('；'.join(esc(n) for n in L['notes']) + '<br>')
    if s.get('smart'):
        # 聪明钱按实际归属阵营着色：同向 ✓绿 / 反方 ✗红 / 缺席不标
        if '聪明钱' in s['agreeLayers']:
            s_tag, s_col = '✓', '#16a34a'
        elif '聪明钱' in s['opposeLayers']:
            s_tag, s_col = '✗', '#dc2626'
        else:
            s_tag, s_col = '—', '#9ca3af'
        sm = s['smart']
        s_side = '做多' if sm.get('side') == 'LONG' else ('做空' if sm.get('side') == 'SHORT' else '中性')
        h.append('<span style="color:%s"><b>%s 聪明钱（%s）</b></span>：%s<br>'
                 % (s_col, s_tag, s_side, esc('；'.join(sm['reasons']))))
    if s['opposeLayers']:
        opp = []
        for L in s['layers']:
            if L['layer'] in s['opposeLayers']:
                opp.append('%s层（%s）' % (L['layer'], '看多' if L['side'] == 'LONG' else '看空'))
        if '聪明钱' in s['opposeLayers'] and s.get('smart'):
            opp.append('聪明钱（%s）' % ('看多' if s['smart'].get('side') == 'LONG' else '看空'))
        h.append('对立面：%s<br>' % '、'.join(opp))
    if s['neutralLayers']:
        h.append('未表态：%s<br>' % '、'.join(s['neutralLayers']))
    h.append('日线大周期方向：%s' % (s.get('trend1d') or '—'))
    h.append('</div></div>')
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
    summary = '%s %s · 置信度 %d · 共 %d 条' % (
        cn(hd['coin']), '看多' if hd['side'] == 'LONG' else '看空',
        hd['confidence'], len(signals))
    payload = {
        'appToken': token,
        'content': "<h3>📡 熬鹰 · 主流币信号</h3>" + body +
                   "<p style='color:#999;font-size:11px'>三层技术分析+聪明钱投票 · "
                   "监控 %d 个主流币 · %s 北京时间<br>仅供参考，不构成投资建议</p>"
                   % (len(MAJORS), time.strftime('%m-%d %H:%M',
                                                 time.localtime(time.time() + 8 * 3600))),
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

    # 1) 先取持仓结构（庄家层 + 聪明钱）
    rows = []
    raws = {}
    funds = {}
    for sym in MAJORS:
        r = stat(sym)
        rows.append(r)
        if r and r.get('raw'):
            raws[sym] = r['raw']
        funds[sym] = funding_rate(sym)
    okn = len([r for r in rows if r])
    smart = judge_smart(rows)

    # 2) 逐个币做五层技术分析（右侧优先）
    signals = []
    for sym in MAJORS:
        r = analyze_coin(sym, smart, dealer_raw=raws.get(sym), funding=funds.get(sym))
        if r:
            signals.append(r)
        time.sleep(0.15)   # 控制请求频率

    # 排序：右侧优先 > 核心币 > 置信度
    signals.sort(key=lambda s: (
        0 if s.get('tradeType') == '右侧' else 1,
        {'A': 0, 'B': 1, 'C': 2, 'C-': 3, 'L': 4}.get(s.get('grade'), 5),
        0 if s['coin'] in HEAVY else 1,
        -s['confidence']))
    signals = signals[:MAX_PUSH]

    out = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)) + ' 北京时间',
        'ts': int(time.time()),
        'scanned': len(MAJORS),
        'ok': okn,
        'hitCount': len(signals),
        'source': 'gateio',
        'engine': 'TA-5layer-rightside',
        'signals': signals,
        'cost': round(time.time() - t0, 1),
    }
    with open('consensus.json', 'w', encoding='utf-8') as f_:
        json.dump(out, f_, ensure_ascii=False, indent=1)

    # 3) 去重后推送
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

    print('done: coins=%d ok=%d hit=%d fresh=%d cost=%.1fs push=%s'
          % (len(MAJORS), okn, len(signals), len(fresh), time.time() - t0, res))
    for s in signals:
        print('  %-10s %-5s conf=%3d votes=%s agree=%s'
              % (cn(s['coin']), s['side'], s['confidence'], s['votes'],
                 ','.join(s['agreeLayers'])))


if __name__ == '__main__':
    main()
