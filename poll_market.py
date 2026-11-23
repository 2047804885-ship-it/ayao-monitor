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
# ⚠️ 这里原来是「单次最多推几条」，超出的直接扔掉——那是丢包。
#    现在改成「每条微信消息最多放几条」，超了就拆下一条继续发，一条都不丢。
ITEMS_PER_MSG = 4
CHAR_BUDGET = 11000       # 单条消息字符预算（WxPusher content 上限约 20K）
# 数据源全挂的判定阈值：取数成功的币少于这个比例，就推「监控异常」而不是静默
HEALTH_MIN_RATIO = 0.3

# ---- 组合层面刹车：防止"七个币全看空"被误读成七重确认 ----
# 单边行情下所有币同涨同跌，同向信号越多，越可能只是在押同一个宏观方向。
CORR_TRIGGER = 4      # 同向信号达到这个数就触发收敛
CORR_KEEP = 3         # 收敛后最多保留几条
CORR_KEEP_AB = 2      # A/B 级最多占几席（A 再多也不全押，留一个给其他币）


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
        'voteShare': v.get('voteShare', 0),
        'nVoted': v.get('nVoted', 0),
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
    nv = s.get('nVoted') or 0
    vs = s.get('voteShare') or 0
    if nv:
        vshare_txt = (' · 表态 %d 层同向 <b>%.0f%%</b>（≥66.7%% 达标）'
                      % (nv, vs * 100))
    else:
        vshare_txt = ''
    h.append('<div style="font-size:13px;color:#333;margin-bottom:6px">'
             '现价 %s · 置信度 <b>%d</b> · %d 层投票 <b>%s</b> 一致%s'
             % (px_txt, s['confidence'], len(s['layers']), s['votes'], vshare_txt))
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


def brake(signals):
    """组合风险刹车：同向过多时收敛，避免在一个方向上重复押注

    返回 (保留列表, 刹车信息 dict 或 None)
    """
    if not signals:
        return signals, None

    longs = [s for s in signals if s['side'] == 'LONG']
    shorts = [s for s in signals if s['side'] == 'SHORT']

    # 多空兼有 = 天然对冲，不是单边押注，不刹车
    if longs and shorts:
        return signals, None

    side, group = ('LONG', longs) if longs else ('SHORT', shorts)
    if len(group) < CORR_TRIGGER:
        return signals, None

    # 按等级挑（signals 进来时已排好序：右侧 > 等级 > 核心币 > 置信度）
    keep = []
    keep_coins = set()
    # A/B 级优先保，但不超过 CORR_KEEP_AB，防止全是 A 时又变成满仓
    for s in group:
        if len(keep) >= CORR_KEEP_AB:
            break
        if s.get('grade') in ('A', 'B'):
            keep.append(s)
            keep_coins.add(s['coin'])
    # 剩余名额按原顺序补齐（A 级不够时也能补够数量）
    for s in group:
        if len(keep) >= CORR_KEEP:
            break
        if s['coin'] in keep_coins:
            continue
        keep.append(s)
        keep_coins.add(s['coin'])

    dropped = [s for s in group if s['coin'] not in keep_coins]
    # keep 上面已经被 CORR_KEEP 限住，原来的 keep[:MAX_PUSH] 是空操作，去掉
    kept = keep

    info = {
        'side': side,
        'total': len(group),
        'kept': [cn(s['coin']) for s in kept],
        'dropped': [cn(s['coin']) for s in dropped],
        'droppedGrades': [(cn(s['coin']), s.get('grade')) for s in dropped],
    }
    return kept, info


def fmt_brake(info):
    """刹车提示卡片，放在所有信号之前，防止误读"""
    if not info:
        return ''
    side_cn = '看多' if info['side'] == 'LONG' else '看空'
    col = '#0ecb81' if info['side'] == 'LONG' else '#f6465d'
    drop_txt = '、'.join('%s(%s)' % (c, g) for c, g in info['droppedGrades'][:8])
    h = ['<div style="margin:10px 0;padding:9px 12px;border:1px dashed #f59e0b;'
         'background:#fffbeb;border-radius:4px;font-size:12px;color:#78350f;'
         'line-height:1.65">']
    h.append('<b>⚠️ 组合刹车已生效</b><br>')
    h.append('本次<b style="color:%s">%s</b>方向的信号多达 <b>%d</b> 个。'
             '主流币高相关，同向过多往往只是押同一个宏观方向，不是多次独立确认。'
             % (col, side_cn, info['total']))
    h.append('<br>只推等级最高的 <b>%d</b> 个：<b>%s</b>'
             % (len(info['kept']), '、'.join(info['kept']) or '—'))
    if drop_txt:
        h.append('<br>已压制：%s' % drop_txt)
    h.append('</div>')
    return ''.join(h)


def wxpush(signals, brake_info=None):
    """分批推送，返回 (状态, 真正发出去的信号列表)

    ⚠️ 返回 sent 而不是只返回 ok：调用方必须只把「确实发出去」的信号写进去重状态，
    否则某条消息失败时，里面的信号会被误标成已推，永久丢失。
    """
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        return 'skipped (no WXPUSHER_TOKEN/WXPUSHER_UID)', []
    if not signals:
        return 'nothing to push', []

    # 先把卡片都渲染好，再按「条数 + 字符」打包，避免 fmt_signal 重复调用
    cards = [(fmt_signal(s), s) for s in signals]
    chunks = []
    cur = []
    clen = 0
    for h, s in cards:
        if cur and (clen + len(h) > CHAR_BUDGET or len(cur) >= ITEMS_PER_MSG):
            chunks.append(cur)
            cur, clen = [], 0
        cur.append((h, s))
        clen += len(h)
    if cur:
        chunks.append(cur)

    total = len(chunks)
    sent = []
    last = 'ok'
    for i, ch in enumerate(chunks, 1):
        body = ''.join(h for h, _ in ch)
        if i == 1 and brake_info:
            body = fmt_brake(brake_info) + body
        hd = ch[0][1]
        side_cn = '看多' if hd['side'] == 'LONG' else '看空'
        if brake_info:
            summary = '⚠️组合刹车 %d个%s信号→只推%d个 · 首位 %s %s' % (
                brake_info['total'],
                '看多' if brake_info['side'] == 'LONG' else '看空',
                len(brake_info['kept']), cn(hd['coin']), side_cn)
        else:
            summary = '%s %s · 置信度 %d · 共 %d 条' % (
                cn(hd['coin']), side_cn, hd['confidence'], len(signals))
        if total > 1:
            summary = '%s · 第%d/%d条' % (summary, i, total)

        title = '📡 熬鹰 · 主流币信号'
        if total > 1:
            title += '（%d/%d）' % (i, total)
        payload = {
            'appToken': token,
            'content': '<h3>%s</h3>' % title + body +
                       "<p style='color:#999;font-size:11px'>七层技术分析+聪明钱投票 · "
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
            resp = json.loads(urllib.request.urlopen(req, timeout=15, context=CTX)
                              .read().decode())
            ok = resp.get('code') == 1000
            print('wxpush %d/%d: %s %s' % (i, total, resp.get('code'), resp.get('msg')))
            if ok:
                sent.extend(s for _, s in ch)
            else:
                last = 'fail:' + str(resp.get('msg'))
        except Exception as e:
            print('wxpush error %d/%d: %s' % (i, total, e))
            last = 'err:' + str(e)[:80]
    return last, sent


def alert_push(title, html, summary):
    """纯告警推送（不走信号格式），失败只记日志不抛异常"""
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        return 'skipped'
    payload = {
        'appToken': token,
        'content': '<h3>%s</h3>%s' % (title, html),
        'summary': summary[:95],
        'contentType': 3,
        'uids': [uid],
        'url': 'https://2047804885-ship-it.github.io/ayao-monitor/market.html',
    }
    req = urllib.request.Request(WXPUSH_API, data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15, context=CTX)
                          .read().decode())
        ok = resp.get('code') == 1000
        print('alert:', resp.get('code'), resp.get('msg'))
        return 'ok' if ok else 'fail'
    except Exception as e:
        print('alert error:', e)
        return 'err'


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

    # 排序：右侧优先 > 等级 > 核心币 > 置信度
    signals.sort(key=lambda s: (
        0 if s.get('tradeType') == '右侧' else 1,
        {'A': 0, 'B': 1, 'C': 2, 'C-': 3, 'L': 4}.get(s.get('grade'), 5),
        0 if s['coin'] in HEAVY else 1,
        -s['confidence']))
    raw_n = len(signals)
    signals, brake_info = brake(signals)
    if brake_info:
        print('组合刹车: %s 信号 %d 个 -> 保留 %d 个，压制 %s'
              % (brake_info['side'], brake_info['total'],
                 len(brake_info['kept']), ','.join(brake_info['dropped'])))
    # ⚠️ 原来这里有一句 signals = signals[:MAX_PUSH]，直接把超出的信号扔掉。
    #    更糟的是：被扔掉的信号不会写进去重状态，而下一轮排序不变、还是取前 4 个，
    #    所以第 5 条往后**永远轮不到**，等于永久丢失。现在全部保留，
    #    超长由 wxpush 拆多条消息发完。

    out = {
        'time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(time.time() + 8 * 3600)) + ' 北京时间',
        'ts': int(time.time()),
        'scanned': len(MAJORS),
        'ok': okn,
        'hitCount': len(signals),
        'rawHitCount': raw_n,
        'brake': brake_info,
        'source': 'gateio',
        'engine': 'TA-7layer-rightside',
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
        # 刹车提示只列本次真正推出的币，避免"保留但已推送过"的对不上
        bi = None
        if brake_info:
            bi = dict(brake_info)
            bi['kept'] = [c for c in brake_info['kept']
                          if any(cn(s['coin']) == c for s in fresh)]
            if not bi['kept']:
                bi = None
        res, sent = wxpush(fresh, brake_info=bi)
        # ⚠️ 只标记确实发出去的。分批推送时某条失败，里面的信号不能标已推，
        #    否则下一轮会被去重挡掉 → 永久丢失。
        if sent:
            for s in sent:
                state[s['coin'] + '|' + s['side']] = now_ts
            save_state(state)
            if len(sent) < len(fresh):
                print('部分发送成功 %d/%d，其余下轮重试' % (len(sent), len(fresh)))

    # 4) 数据源健康检查
    # 全挂时 okn=0、signals 为空，日志和「真的没信号」一模一样，用户完全看不出来。
    # 这里在取数成功率过低时主动推一条告警（6 小时最多一次，防刷屏）。
    if len(MAJORS) and okn < len(MAJORS) * HEALTH_MIN_RATIO:
        last = state.get('_healthAlertTs', 0)
        if now_ts - last > 6 * 3600:
            html = ('<div style="padding:8px 11px;background:#fff7ed;'
                    'border-left:4px solid #f59e0b;border-radius:4px;font-size:13px">'
                    '<b>取数失败 %d/%d 个币</b><br>'
                    '<span style="color:#666">Gate.io 接口异常或本机网络问题，'
                    '本轮「没有信号」不代表行情平静，是监控看不见。</span></div>'
                    % (len(MAJORS) - okn, len(MAJORS)))
            if alert_push('⚠️ 熬鹰监控数据源异常', html,
                          '⚠️ %d/%d 个币取数失败' % (len(MAJORS) - okn, len(MAJORS))) == 'ok':
                state['_healthAlertTs'] = now_ts
                save_state(state)
                print('已推送数据源异常告警')
        else:
            print('数据源异常但 6 小时内已告警过，不重复推送')

    print('done: coins=%d ok=%d hit=%d/%d fresh=%d cost=%.1fs push=%s'
          % (len(MAJORS), okn, len(signals), raw_n, len(fresh), time.time() - t0, res))
    for s in signals:
        print('  %-10s %-5s conf=%3d votes=%s agree=%s'
              % (cn(s['coin']), s['side'], s['confidence'], s['votes'],
                 ','.join(s['agreeLayers'])))


if __name__ == '__main__':
    main()
