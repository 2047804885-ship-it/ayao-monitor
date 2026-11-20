# -*- coding: utf-8 -*-
"""
技术分析引擎 —— 三层独立验证 + 一致性投票

设计原则：
  1. 三层各自独立算，互不参考，最后投票。避免"一个指标说了算"。
  2. 每层只用**有量化回测支撑**的经典方法，不用玄学形态。
  3. 每层必须给出明确的多/空/中性，并输出可复核的理由。

第一层【趋势 TA】   EMA 排列 + ADX 趋势强度 + 多周期共振
    依据：趋势跟踪是长期正期望的策略（Turtle / Donchian 体系），
          ADX>25 才认为趋势有效，否则趋势信号作废。
第二层【动量 TA】   RSI 背离 + MACD 柱状 + 量能确认
    依据：动量 + 背离在转折点识别上胜率最高；RSI 单纯超买超卖无效，
          必须配合背离（价格新高但 RSI 不新高）。
第三层【结构 TA】   摆动高低点聚类 + 布林带位置 + ATR 风险
    依据：支撑阻力来自真实成交聚集（swing point clustering），
          布林带衡量价格相对波动率的位置。
第四层【聪明钱】    持仓结构（原 poll_market 逻辑），作为额外一票
"""
import math


# ============================================================
#  基础指标
# ============================================================
def ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(vals, n):
    out = []
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def stdev(vals):
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def rsi(closes, n=14):
    """返回 RSI 序列（前 n 个为 None）"""
    if len(closes) < n + 1:
        return [None] * len(closes)
    out = [None] * n
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / n, losses / n
    out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = d if d > 0 else 0
        l = -d if d < 0 else 0
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out


def macd(closes, fast=12, slow=26, sig=9):
    if len(closes) < slow + sig:
        return None
    ef, es = ema(closes, fast), ema(closes, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, sig)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return {'dif': dif, 'dea': dea, 'hist': hist}


def atr(highs, lows, closes, n=14):
    if len(closes) < 2:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    a = ema(trs, n)
    return a[-1] if a else 0.0


def adx(highs, lows, closes, n=14):
    """平均趋向指数：>25 趋势有效，<20 震荡"""
    L = len(closes)
    if L < n * 2 + 1:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, L):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    def smooth(a):
        out, s = [], None
        for i, v in enumerate(a):
            s = v if s is None else s - s / n + v
            out.append(s)
        return out
    str_, sp, sn = smooth(trs), smooth(pdm), smooth(ndm)
    dxs = []
    for i in range(len(trs)):
        if str_[i] == 0:
            dxs.append(0.0)
            continue
        pdi = 100 * sp[i] / str_[i]
        ndi = 100 * sn[i] / str_[i]
        dxs.append(0.0 if (pdi + ndi) == 0 else 100 * abs(pdi - ndi) / (pdi + ndi))
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for v in dxs[n:]:
        a = (a * (n - 1) + v) / n
    # 同时给出当前方向
    last_pdi = 100 * sp[-1] / str_[-1] if str_[-1] else 0
    last_ndi = 100 * sn[-1] / str_[-1] if str_[-1] else 0
    return {'adx': a, 'pdi': last_pdi, 'ndi': last_ndi,
            'dir': 1 if last_pdi > last_ndi else -1}


def bollinger(closes, n=20, k=2.0):
    m = sma(closes, n)
    up, dn = [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        sd = stdev(closes[i - n + 1:i + 1])
        if m[i] is not None:
            up[i] = m[i] + k * sd
            dn[i] = m[i] - k * sd
    return {'mid': m, 'up': up, 'dn': dn}


def swings(highs, lows, lb=2):
    hi, lo = [], []
    for i in range(lb, len(highs) - lb):
        isH = all(highs[i] > highs[i - j] and highs[i] > highs[i + j] for j in range(1, lb + 1))
        isL = all(lows[i] < lows[i - j] and lows[i] < lows[i + j] for j in range(1, lb + 1))
        if isH:
            hi.append(highs[i])
        if isL:
            lo.append(lows[i])
    return hi, lo


def cluster(levels, tol=0.006):
    """价位聚类：把相邻 0.6% 以内的摆动点合并成一个支撑/阻力位"""
    if not levels:
        return []
    s = sorted(levels)
    groups, cur = [], [s[0]]
    for v in s[1:]:
        if cur and abs(v - cur[-1]) / cur[-1] <= tol:
            cur.append(v)
        else:
            groups.append(cur)
            cur = [v]
    if cur:
        groups.append(cur)
    # 返回 (价格, 触碰次数)
    return [(sum(g) / len(g), len(g)) for g in groups]


# ============================================================
#  第一层：趋势
# ============================================================
def layer_trend(cs):
    """EMA 排列 + ADX + 多周期共振
    cs: 4h K 线列表，每根 {o,h,l,c,v}，按时间升序
    """
    closes = [c['c'] for c in cs]
    highs = [c['h'] for c in cs]
    lows = [c['l'] for c in cs]
    if len(closes) < 60:
        return None

    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    px = closes[-1]

    a = adx(highs, lows, closes, 14)
    adx_v = a['adx'] if a else 0
    adx_dir = a['dir'] if a else 0

    score = 0
    notes = []

    # 1) 均线排列（最经典的趋势判定）
    if e200 is not None:
        if e20 > e50 > e200:
            score += 2
            notes.append('均线多头排列 EMA20>EMA50>EMA200')
        elif e20 < e50 < e200:
            score -= 2
            notes.append('均线空头排列 EMA20<EMA50<EMA200')
        elif e20 > e50:
            score += 1
            notes.append('短期均线上穿中期（EMA20>EMA50）')
        else:
            score -= 1
            notes.append('短期均线在中期之下（EMA20<EMA50）')
    else:
        if e20 > e50:
            score += 1
            notes.append('EMA20>EMA50，短中期偏多')
        else:
            score -= 1
            notes.append('EMA20<EMA50，短中期偏空')

    # 2) 价格与均线的关系
    if px > e20:
        score += 1
        notes.append('价格站上 EMA20')
    else:
        score -= 1
        notes.append('价格失守 EMA20')

    # 3) ADX 趋势强度：只有 >25 才认这个趋势
    valid = adx_v >= 25
    if adx_v >= 40:
        strength_txt = '极强趋势'
    elif adx_v >= 25:
        strength_txt = '趋势有效'
    elif adx_v >= 20:
        strength_txt = '趋势偏弱'
    else:
        strength_txt = '震荡无趋势'
    if valid and adx_dir != 0:
        # 趋势有效时，ADX 方向加成
        score += adx_dir
        notes.append('ADX %.1f（%s），方向%s' % (adx_v, strength_txt,
                                               '向上' if adx_dir > 0 else '向下'))
    elif adx_v < 25:
        notes.append('ADX %.1f（%s），方向未获动量确认' % (adx_v, strength_txt))

    side = 'LONG' if score >= 3 else ('SHORT' if score <= -3 else 'NEUTRAL')
    # 置信度分档：ADX 有效给满额加成；ADX 偏弱(20~25)给半额，避免"方向对但力度不足"被一刀切丢弃
    if valid:
        _adx_bonus = 20
    elif adx_v >= 20:
        _adx_bonus = 10
    else:
        _adx_bonus = 0
    return {
        'layer': '趋势', 'side': side, 'score': score,
        'valid': valid, 'adx': round(adx_v, 1),
        'adxBand': strength_txt,
        'ema20': round(e20, 6), 'ema50': round(e50, 6),
        'ema200': round(e200, 6) if e200 else None,
        'px': px, 'notes': notes,
        'confidence': min(100, abs(score) * 18 + _adx_bonus),
    }


# ============================================================
#  第二层：动量
# ============================================================
def layer_momentum(cs):
    """RSI 背离 + MACD + 量能确认"""
    if len(cs) < 60:
        return None
    closes = [c['c'] for c in cs]
    vols = [c['v'] for c in cs]
    highs = [c['h'] for c in cs]
    lows = [c['l'] for c in cs]

    r = rsi(closes, 14)
    r_now = r[-1] if r[-1] is not None else 50

    m = macd(closes)
    if not m:
        return None
    hist = m['hist']
    dif, dea = m['dif'], m['dea']

    score = 0
    notes = []

    # 1) MACD 柱状方向与转折
    h_now, h_prev = hist[-1], hist[-2]
    if h_now > 0 and h_prev <= 0:
        score += 2
        notes.append('MACD 柱由负转正（金叉），动能翻多')
    elif h_now < 0 and h_prev >= 0:
        score -= 2
        notes.append('MACD 柱由正转负（死叉），动能翻空')
    elif h_now > 0:
        score += 1
        notes.append('MACD 柱为正（%.4f），多头动能维持' % h_now)
    elif h_now < 0:
        score -= 1
        notes.append('MACD 柱为负（%.4f），空头动能维持' % h_now)

    # 2) DIF 与 DEA 的相对位置
    if dif[-1] > dea[-1]:
        score += 1
        notes.append('DIF 在 DEA 之上')
    else:
        score -= 1
        notes.append('DIF 在 DEA 之下')

    # 3) RSI 背离检测（最重要的转折信号）
    hi_levels, lo_levels = swings(highs, lows, 2)
    divergence = None
    # 顶背离：价格新高，RSI 不新高
    recent_hi_idx = []
    for i in range(len(highs) - 1, max(0, len(highs) - 40), -1):
        if highs[i] in hi_levels:
            recent_hi_idx.append(i)
        if len(recent_hi_idx) >= 2:
            break
    if len(recent_hi_idx) >= 2:
        i2, i1 = recent_hi_idx[0], recent_hi_idx[1]
        if highs[i2] > highs[i1] and r[i2] is not None and r[i1] is not None:
            if r[i2] < r[i1] - 2:
                divergence = 'bearish'
                score -= 2
                notes.append('顶背离：价格创新高但 RSI 走低（%.0f→%.0f），上涨乏力'
                             % (r[i1], r[i2]))
    # 底背离：价格新低，RSI 不新低
    recent_lo_idx = []
    for i in range(len(lows) - 1, max(0, len(lows) - 40), -1):
        if lows[i] in lo_levels:
            recent_lo_idx.append(i)
        if len(recent_lo_idx) >= 2:
            break
    if len(recent_lo_idx) >= 2 and divergence != 'bearish':
        i2, i1 = recent_lo_idx[0], recent_lo_idx[1]
        if lows[i2] < lows[i1] and r[i2] is not None and r[i1] is not None:
            if r[i2] > r[i1] + 2:
                divergence = 'bullish'
                score += 2
                notes.append('底背离：价格创新低但 RSI 走高（%.0f→%.0f），下跌衰竭'
                             % (r[i1], r[i2]))

    # 4) 量能确认（信号必须放量才可信）
    v_recent = sum(vols[-6:]) / 6
    v_base = sum(vols[-30:]) / 30
    vr = v_recent / v_base if v_base > 0 else 1
    vol_ok = vr >= 1.15
    if vol_ok:
        notes.append('近 6 根量能为 30 根均量的 %.2f 倍，放量确认' % vr)
    else:
        notes.append('量能仅 %.2f 倍均量，动能不足' % vr)

    # RSI 位置辅助
    if r_now >= 75:
        notes.append('RSI %.0f 超买区' % r_now)
    elif r_now <= 25:
        notes.append('RSI %.0f 超卖区' % r_now)

    side = 'LONG' if score >= 3 else ('SHORT' if score <= -3 else 'NEUTRAL')
    return {
        'layer': '动量', 'side': side, 'score': score,
        'rsi': round(r_now, 1), 'macdHist': round(h_now, 6),
        'volRatio': round(vr, 2), 'volOk': vol_ok,
        'divergence': divergence, 'notes': notes,
        'confidence': min(100, abs(score) * 18 + (15 if vol_ok else 0)),
    }


# ============================================================
#  第三层：结构
# ============================================================
def layer_structure(cs):
    """支撑阻力聚类 + 布林带位置 + ATR"""
    if len(cs) < 40:
        return None
    closes = [c['c'] for c in cs]
    highs = [c['h'] for c in cs]
    lows = [c['l'] for c in cs]
    px = closes[-1]

    score = 0
    notes = []

    # 1) 布林带位置
    bb = bollinger(closes, 20, 2.0)
    up, dn, mid = bb['up'][-1], bb['dn'][-1], bb['mid'][-1]
    pct_b = None
    if up is not None and dn is not None and up > dn:
        pct_b = (px - dn) / (up - dn)
    if pct_b is not None:
        if pct_b > 1.0:
            score -= 1
            notes.append('价格突破布林上轨（%%B=%.2f），短期过热' % pct_b)
        elif pct_b < 0.0:
            score += 1
            notes.append('价格跌破布林下轨（%%B=%.2f），短期超跌' % pct_b)
        elif pct_b > 0.65:
            score += 1
            notes.append('价格位于布林带上半区（%%B=%.2f）' % pct_b)
        elif pct_b < 0.35:
            score -= 1
            notes.append('价格位于布林带下半区（%%B=%.2f）' % pct_b)

    # 2) 摆动高低点聚类 -> 最近的支撑与阻力
    hi_l, lo_l = swings(highs, lows, 2)
    res_levels = cluster([h for h in hi_l if h > px])
    sup_levels = cluster([l for l in lo_l if l < px])
    near_res = min(res_levels, key=lambda x: x[0] - px) if res_levels else None
    near_sup = max(sup_levels, key=lambda x: x[0]) if sup_levels else None

    pos_txt = []
    if near_res:
        d_res = (near_res[0] - px) / px * 100
        pos_txt.append('上方阻力 %.6g（距 %.2f%%，触碰 %d 次）' % (near_res[0], d_res, near_res[1]))
    if near_sup:
        d_sup = (px - near_sup[0]) / px * 100
        pos_txt.append('下方支撑 %.6g（距 %.2f%%，触碰 %d 次）' % (near_sup[0], d_sup, near_sup[1]))
    notes.extend(pos_txt)

    # 阻力更近 = 上行空间受限 -> 略偏空；支撑更近 = 下行受限 -> 略偏多
    if near_res and near_sup:
        d_res = (near_res[0] - px) / px
        d_sup = (px - near_sup[0]) / px
        if d_res > d_sup * 1.8:
            score += 1
            notes.append('距离支撑近、阻力远，盈亏比偏多')
        elif d_sup > d_res * 1.8:
            score -= 1
            notes.append('距离阻力近、支撑远，盈亏比偏空')

    # 3) 近期结构：是否创出更高的高点与更高的低点
    if len(hi_l) >= 2 and len(lo_l) >= 2:
        hh = hi_l[-1] > hi_l[-2]
        hl = lo_l[-1] > lo_l[-2]
        if hh and hl:
            score += 2
            notes.append('结构上抬：更高高点 + 更高低点（HH/HL）')
        elif (not hh) and (not hl):
            score -= 2
            notes.append('结构下移：更低高点 + 更低低点（LH/LL）')

    # 4) 用 ATR 量化风险空间
    a = atr(highs, lows, closes, 14)
    atr_pct = a / px * 100 if px else 0
    notes.append('ATR(14) 波动 %.2f%% / 根，止损参考 %.2f%%' % (atr_pct, atr_pct * 1.5))

    side = 'LONG' if score >= 2 else ('SHORT' if score <= -2 else 'NEUTRAL')
    # 置信度标尺与趋势/动量层对齐：基础分 + 结构位移加成 + 关键位贴合加成
    _struct_ok = (len(hi_l) >= 2 and len(lo_l) >= 2
                  and ((hi_l[-1] > hi_l[-2]) == (lo_l[-1] > lo_l[-2])))
    _key_ok = bool(near_res or near_sup)
    return {
        'layer': '结构', 'side': side, 'score': score,
        'pctB': round(pct_b, 3) if pct_b is not None else None,
        'res': near_res[0] if near_res else None,
        'sup': near_sup[0] if near_sup else None,
        'atrPct': round(atr_pct, 2),
        'notes': notes,
        'confidence': min(100, abs(score) * 18 + (18 if _struct_ok else 0) + (10 if _key_ok else 0)),
    }


# ============================================================
#  汇总投票
# ============================================================
def vote(layers, smart=None):
    """三层 + 聪明钱投票，返回最终结论

    规则（宁可漏报不可错报）：
      - 中性层不算反方，只稀释一致性
      - 至少 2 层给出一致方向
      - 反方票数必须严格小于正方
      - 置信度不足 45 的弃掉
    """
    valid = [l for l in layers if l]
    if smart and smart.get('side'):
        valid.append({'layer': '聪明钱', 'side': smart['side'],
                      'score': smart.get('score', 3),
                      'confidence': min(100, smart.get('score', 3) * 15),
                      'notes': smart.get('reasons', [])})

    if not valid:
        return None

    longs = [l for l in valid if l['side'] == 'LONG']
    shorts = [l for l in valid if l['side'] == 'SHORT']
    neutral = [l for l in valid if l['side'] == 'NEUTRAL']

    if len(longs) > len(shorts):
        side, agree, oppose = 'LONG', longs, shorts
    elif len(shorts) > len(longs):
        side, agree, oppose = 'SHORT', shorts, longs
    else:
        return None

    # 至少 2 票一致
    if len(agree) < 2:
        return None
    # 反方不能多于正方
    if len(oppose) > len(agree):
        return None

    total = len(valid)
    ratio = len(agree) / total
    # 置信度：以同向层为主（中性层只表示"没表态"，轻微折扣，不做分母主体）
    # 反方层才真正削弱结论
    avg_conf = sum(l['confidence'] for l in agree) / len(agree)
    penalty = len(oppose) * 15 + len(neutral) * 5
    conf = round(avg_conf - penalty)
    conf = max(0, min(100, conf))
    # 三层全同向额外加成
    if len(oppose) == 0 and len(neutral) == 0 and len(agree) >= 3:
        conf = min(100, conf + 8)

    # 硬确认标记（仅用于展示与「非全一致」时的兜底）
    has_trend = any(l.get('valid') and l['layer'] == '趋势' for l in agree)
    has_vol = any(l.get('volOk') and l['layer'] == '动量' for l in agree)
    has_smart = any(l['layer'] == '聪明钱' for l in agree)

    # 三层技术分析全部同向 = 最强确认，无需额外硬确认
    # 只在「仅两层一致」时才要求至少一项硬确认，防止震荡市误报
    unanimous = len(oppose) == 0 and len(neutral) == 0 and len(agree) >= 3

    if conf < 45:
        return None
    if not unanimous and not (has_trend or has_vol or has_smart):
        return None

    return {
        'side': side,
        'confidence': conf,
        'votes': '%d/%d' % (len(agree), total),
        'agreeLayers': [l['layer'] for l in agree],
        'opposeLayers': [l['layer'] for l in oppose],
        'neutralLayers': [l['layer'] for l in neutral],
        'confirm': {'trend': has_trend, 'volume': has_vol, 'smartMoney': has_smart},
        'unanimous': unanimous,
    }
