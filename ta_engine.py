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
#  第四层：量价（回测驱动：只做衰竭/背离，不做放量追涨）
# ============================================================
def layer_volprice(cs):
    """量价配合层

    回测结论（24 币 / 4h / 10 月）：
      ✗ 「放量确认」在 4h 是负贡献：+量比>=1.5 时胜率从 69.7% 反降到 62.8%
      ✓ 「缩量衰竭」逻辑有效，但单独作为左侧信号胜率仍偏低（35.6%）
    → 本层仅作加权项（权重 0.8），不主导方向
    """
    if len(cs) < 40:
        return None
    closes = [c['c'] for c in cs]
    opens = [c['o'] for c in cs]
    highs = [c['h'] for c in cs]
    lows = [c['l'] for c in cs]
    vols = [c['v'] for c in cs]
    score = 0
    notes = []

    v6 = sum(vols[-6:]) / 6
    v30 = sum(vols[-30:]) / 30
    vr = v6 / v30 if v30 > 0 else 1.0
    px_chg = (closes[-1] - opens[-6]) / opens[-6] * 100 if opens[-6] else 0

    if px_chg < -1.0 and vr < 0.75:
        score += 3
        notes.append('缩量下跌（价格 %.1f%%/量比 %.2f），抛压衰竭'
                     % (px_chg, vr))
    elif px_chg > 1.0 and vr < 0.75:
        score -= 3
        notes.append('缩量上涨（价格 %+.1f%%/量比 %.2f），跟风盘不足，上涨虚'
                     % (px_chg, vr))
    elif px_chg < -1.0 and vr > 1.8:
        score -= 2
        notes.append('放量下跌（价格 %.1f%%/量比 %.2f），抛压真实释放'
                     % (px_chg, vr))
    elif px_chg > 1.0 and vr > 1.8:
        score += 1
        notes.append('放量上涨（价格 %+.1f%%/量比 %.2f），有资金承接'
                     % (px_chg, vr))
    else:
        notes.append('量价中性（价格 %+.1f%%/量比 %.2f）' % (px_chg, vr))

    if len(cs) >= 24:
        v1 = sum(vols[-6:]) / 6
        v2 = sum(vols[-18:-6]) / 12
        if v2 > 0 and v1 < v2 * 0.85:
            if max(highs[-6:]) > max(highs[-18:-6]):
                score -= 2
                notes.append('量价顶背离：价格创新高但成交萎缩 %.0f%%'
                             % ((1 - v1 / v2) * 100))
            if min(lows[-6:]) < min(lows[-18:-6]):
                score += 2
                notes.append('量价底背离：价格创新低但成交萎缩 %.0f%%'
                             % ((1 - v1 / v2) * 100))

    side = 'LONG' if score >= 3 else ('SHORT' if score <= -3 else 'NEUTRAL')
    return {
        'layer': '量价', 'side': side, 'score': score,
        'volRatio': round(vr, 2), 'pxChg6': round(px_chg, 2),
        'notes': notes,
        'confidence': min(100, abs(score) * 16 + (10 if abs(vr - 1) > 0.25 else 0)),
    }


# ============================================================
#  第五层：庄家（资金面 / 操盘意图）
# ============================================================
def layer_dealer(stat_rows, funding=None):
    """庄家操盘层：持仓结构 + 大户散户对立 + 资金费率 + 增减仓 + 爆仓分布

    stat_rows: Gate contract_stats 原始 list（时间升序）
    funding:   当前资金费率（小数，负数=空头付钱给多头）
    """
    if not stat_rows or len(stat_rows) < 12:
        return None

    def _f(x):
        try:
            return float(x)
        except Exception:
            return None

    tops = [x for x in (_f(r.get('top_lsr_size')) for r in stat_rows) if x is not None]
    accs = [x for x in (_f(r.get('lsr_account')) for r in stat_rows) if x is not None]
    takers = [x for x in (_f(r.get('lsr_taker')) for r in stat_rows) if x is not None]
    ois = [x for x in (_f(r.get('open_interest')) for r in stat_rows) if x is not None]
    mps = [x for x in (_f(r.get('mark_price')) for r in stat_rows) if x is not None]

    if not tops:
        return None

    score = 0
    notes = []
    top_now, top_prev = tops[-1], tops[0]
    top_chg = top_now - top_prev

    if top_now >= 0.65:
        score += 2
        notes.append('大户多单占比 %.0f%%，重仓押多' % (top_now * 100))
    elif top_now <= 0.40:
        score -= 2
        notes.append('大户多单占比 %.0f%%，重仓押空' % (top_now * 100))

    if top_chg > 0.03:
        score += 2
        notes.append('大户近 12h 加多 %.1f 个点（%.1f%%→%.1f%%）'
                     % (top_chg * 100, top_prev * 100, top_now * 100))
    elif top_chg < -0.03:
        score -= 2
        notes.append('大户近 12h 减多 %.1f 个点（%.1f%%→%.1f%%）'
                     % (abs(top_chg) * 100, top_prev * 100, top_now * 100))

    if accs:
        acc_now = accs[-1]
        if top_now > 0.58 and acc_now < 1.0:
            score += 2
            notes.append('大户偏多(%.0f%%)但散户偏空(账户比 %.2f)，吸筹结构'
                         % (top_now * 100, acc_now))
        elif top_now < 0.45 and acc_now > 1.2:
            score -= 2
            notes.append('大户偏空(%.0f%%)但散户偏多(账户比 %.2f)，派发结构'
                         % (top_now * 100, acc_now))

    if len(ois) >= 2 and ois[0] and len(mps) >= 2 and mps[0]:
        oi_chg = (ois[-1] - ois[0]) / ois[0] * 100
        px_chg = (mps[-1] - mps[0]) / mps[0] * 100
        if oi_chg > 2 and px_chg > 0:
            score += 2
            notes.append('持仓增 %.1f%% 且价格上涨，多头主动进场' % oi_chg)
        elif oi_chg > 2 and px_chg < 0:
            score -= 2
            notes.append('持仓增 %.1f%% 但价格下跌，空头加仓压制' % oi_chg)
        elif oi_chg < -2 and px_chg < 0:
            score += 1
            notes.append('持仓减 %.1f%% 且价格下跌，多头止损离场，下跌将尽' % abs(oi_chg))
        elif oi_chg < -2 and px_chg > 0:
            score -= 1
            notes.append('持仓减 %.1f%% 但价格上涨，空头平仓推动，涨势虚' % abs(oi_chg))

    if takers:
        tk = sum(takers[-6:]) / min(6, len(takers))
        if tk > 1.15:
            score += 1
            notes.append('主动买入占优（taker 比 %.2f）' % tk)
        elif tk < 0.87:
            score -= 1
            notes.append('主动卖出占优（taker 比 %.2f）' % tk)

    if funding is not None:
        fr = funding * 100
        if fr <= -0.03:
            score += 2
            notes.append('资金费率 %.4f%%，空头付钱给多头，空头拥挤易被挤' % fr)
        elif fr >= 0.05:
            score -= 2
            notes.append('资金费率 %+.4f%%，多头付钱给空头，多头拥挤易被砸' % fr)
        else:
            notes.append('资金费率 %+.4f%%，多空成本均衡' % fr)

    ll = sum(_f(r.get('long_liq_usd')) or 0 for r in stat_rows[-24:])
    sl = sum(_f(r.get('short_liq_usd')) or 0 for r in stat_rows[-24:])
    if ll + sl > 0:
        if ll / (ll + sl) > 0.8:
            score += 2
            notes.append('24h 爆仓 %.0f%% 是多头被清洗，浮筹已清'
                         % (ll / (ll + sl) * 100))
        elif sl / (ll + sl) > 0.8:
            score -= 2
            notes.append('24h 爆仓 %.0f%% 是空头被清洗，空头已打爆'
                         % (sl / (ll + sl) * 100))
        else:
            notes.append('24h 爆仓多空较均衡')

    side = 'LONG' if score >= 3 else ('SHORT' if score <= -3 else 'NEUTRAL')
    return {
        'layer': '庄家', 'side': side, 'score': score,
        'topLs': round(top_now, 3), 'topChg': round(top_chg, 4),
        'funding': round(funding * 100, 4) if funding is not None else None,
        'notes': notes,
        'confidence': min(100, abs(score) * 14 + 15),
    }


# ============================================================
#  汇总投票：右侧交易优先（权重与门槛全部由回测标定）
# ============================================================
# 权重来源：24 币 / 4h / 10 个月 / 489 个信号样本回测
#   趋势(EMA+ADX)   右侧胜率 69.9%  249 样本  → 权重 3.0  【核心】
#   结构(布林+摆动)  单层胜率 58.3%  669 样本  → 权重 3.0  【核心】
#   庄家(资金面)     无历史可回测             → 权重 1.5  【情报，不独断】
#   量价(缩量衰竭)   左侧胜率 35.6%           → 权重 0.8  【仅加分，不主导】
#   动量(MACD+RSI)  左侧胜率 35.6%           → 权重 0.5  【仅微调】
#
# 【右侧交易原则】用户明确要求：
#   默认只推右侧（趋势已确认、顺势入场、等回调）
#   左侧（背离抄底摸顶）实测胜率仅 35.6%，做多抄底更只有 29.3% → 默认禁止
#   只有量价出现"极其可靠"的衰竭+背离共振时，才允许左侧介入，并在推送中显式标注
LAYER_WEIGHT = {
    '趋势': 3.0,
    '结构': 2.5,
    '缠论': 2.5,     # 回测：缠论+趋势同向 73.4%（218 样本），是最强组合
    '订单流': 1.5,   # 实时资金行为，无法回测但情报价值高
    '庄家': 1.5,
    '量价': 0.8,
    '动量': 0.5,
}

# ---- 右侧交易门槛 ----
RIGHT_MIN_ADX = 40        # 右侧要求趋势强度 ADX>=40（回测 69.9%，ADX>=55 时 71.0%）
RIGHT_MIN_CONF = 62       # 右侧最终置信度门槛
RIGHT_MIN_RATIO = 0.55    # 右侧加权净得分占比门槛
VOTE_MIN_SHARE = 2 / 3    # 表态层中同向占比须达 2/3（三分之二多数）

# ---- 左侧特批条件（极苛刻，宁可不推）----
LEFT_MIN_CONF = 80        # 左侧要求置信度 >=80
LEFT_REQUIRE_ADX = 30     # 左侧也要求趋势不能完全死掉
LEFT_REQUIRE_VP_SCORE = 5 # 量价层必须给满 5 分（缩量衰竭 + 背离共振）


def vote(layers, smart=None):
    """五层加权投票 + 右侧优先裁决

    返回字段新增：
      tradeType: '右侧' / '左侧'   —— 推送中必须显式标注
      rightSide: True/False        —— 是否顺势
    """
    valid = [l for l in layers if l]

    # 兼容旧调用：smart 传 dict 时转成庄家层
    if smart and smart.get('side'):
        valid.append({
            'layer': '庄家',
            'side': smart['side'],
            'score': smart.get('score', 3),
            'confidence': min(100, smart.get('score', 3) * 14 + 15),
            'notes': smart.get('reasons', []),
        })

    if not valid:
        return None

    trend = next((l for l in valid if l['layer'] == '趋势'), None)
    volp = next((l for l in valid if l['layer'] == '量价'), None)

    # ============ 第一关：右侧资格（默认路径）============
    trend_ok = bool(trend and trend['side'] != 'NEUTRAL'
                    and (trend.get('adx') or 0) >= RIGHT_MIN_ADX)
    right_side = trend['side'] if trend_ok else None

    # ============ 第二关：左侧特批（极苛刻）============
    left_ok = False
    left_side = None
    if not right_side and trend and volp:
        vp_score = abs(volp.get('score') or 0)
        adx_v = trend.get('adx') or 0
        # 量价必须满 5 分（缩量衰竭 3 + 背离 2），且趋势不能死透
        if vp_score >= LEFT_REQUIRE_VP_SCORE and adx_v >= LEFT_REQUIRE_ADX:
            left_ok = True
            left_side = volp['side']
            # 趋势方向与之相反，才算真正逆势抄底
            if trend['side'] == left_side:
                left_ok = False

    if not right_side and not left_ok:
        return None

    target_side = right_side or left_side
    trade_type = '右侧' if right_side else '左侧'

    # ============ 加权计分 ============
    total_w = 0.0
    net = 0.0
    win, lose, neutral = [], [], []

    for l in valid:
        w = LAYER_WEIGHT.get(l['layer'], 1.0)
        # ADX 不足时趋势票打折
        if l['layer'] == '趋势' and (l.get('adx') or 0) < RIGHT_MIN_ADX:
            w *= 0.5
        strength = 0.4 + min(1.0, (l.get('confidence', 50) / 100.0)) * 0.6

        if l['side'] == 'LONG':
            net += w * strength
            total_w += w
        elif l['side'] == 'SHORT':
            net -= w * strength
            total_w += w

        if l['side'] == 'NEUTRAL':
            neutral.append(l)
        elif l['side'] == target_side:
            win.append(l)
        else:
            lose.append(l)

    if not win or total_w <= 0:
        return None

    # ⚠️ 分母只算「表态层」。中立层不表态就不该稀释一致性，
    #    否则加层越多、分母越大，真信号被稀释到永远过不了门槛。
    ratio = abs(net) / total_w

    # 但只看表态层会让「仅两层一致」直接等于满分（ratio≈1.0），
    # 而回测里趋势+结构的真实胜率只有 69.7% —— 必须用表态覆盖率回压。
    # 覆盖率 = 表态层权重 / 全部层权重（含中立层）。7 层里只 2 层说话 ≈ 0.45。
    w_all = 0.0
    for l in valid:
        w_all += LAYER_WEIGHT.get(l['layer'], 1.0)
    cover = (total_w / w_all) if w_all > 0 else 0.0
    # 覆盖率映射：1.0→1.0（全员表态），0.4→0.62，0.25→0.5
    cover_f = 0.5 + 0.5 * min(1.0, cover / 1.0) ** 0.6

    conf = round(ratio * 100 * cover_f)

    # 顺势加成 / 逆势减分
    if right_side:
        if len(win) >= 3 and not lose:
            conf = min(100, conf + 6)
        adx_v = (trend.get('adx') or 0) if trend else 0
        if adx_v >= 55:
            conf = min(100, conf + 6)
        elif adx_v >= 45:
            conf = min(100, conf + 3)
        # 缠论共振是回测最优组合（73.4%），额外加成
        chan_ok = any(l['layer'] == '缠论' and l['side'] == target_side for l in win)
        if chan_ok:
            conf = min(100, conf + 5)
    else:
        conf = min(100, conf - 5)   # 左侧一律扣分

    # ============ 门槛裁决 ============
    # 三分之二多数：按「表态层」计票（中立层不投票，不该占分母）
    #   表态层 = win + lose；win 必须 >= 表态层的 2/3
    n_voted = len(win) + len(lose)
    vote_share = (len(win) / n_voted) if n_voted else 0.0

    if right_side:
        if ratio < RIGHT_MIN_RATIO:
            return None
        if len(win) < 2:            # 至少两层同向
            return None
        if vote_share < VOTE_MIN_SHARE:
            return None
        w_win = sum(LAYER_WEIGHT.get(l['layer'], 1) for l in win)
        w_lose = sum(LAYER_WEIGHT.get(l['layer'], 1) for l in lose)
        if w_lose > w_win * 0.8:    # 反方不能压过正方
            return None
        if conf < RIGHT_MIN_CONF:
            return None
    else:
        # 左侧：必须有庄家层或结构层佐证，不能只靠量价一层
        if len(win) < 2:
            return None
        if vote_share < VOTE_MIN_SHARE:
            return None
        if conf < LEFT_MIN_CONF:
            return None

    # ============ 质量分级（回测标定，置信度分档对胜率几乎无影响，
    #              真正拉开差距的是缠论共振）============
    chan_agree = any(l['layer'] == '缠论' and l['side'] == target_side for l in win)
    chan_against = any(l['layer'] == '缠论' and l['side'] != target_side for l in lose)
    adx_now = (trend.get('adx') or 0) if trend else 0

    if trade_type == '右侧':
        if chan_agree and adx_now >= 45:
            grade = 'A'      # 回测 76.5%（98 样本）
        elif chan_agree:
            grade = 'B'      # 回测 69.4%（49 样本）
        elif chan_against:
            grade = 'C'      # 缠论明确反对
        else:
            # 缠论不表态（震荡/无中枢）。注意：ADX>=50 但缠论无话可说时
            # 回测只有 52.0%，比 C 还差 —— 强趋势 + 结构不明 = 陷阱
            grade = 'C-'
    else:
        grade = 'L'          # 左侧特批，单列

    return {
        'side': target_side,
        'confidence': conf,
        'grade': grade,
        'chanAgree': chan_agree,
        'chanAgainst': chan_against,
        'votes': '%d/%d' % (len(win), len(valid)),
        'voteShare': round(vote_share, 3),
        'nVoted': n_voted,
        'tradeType': trade_type,
        'rightSide': bool(right_side),
        'agreeLayers': [l['layer'] for l in win],
        'opposeLayers': [l['layer'] for l in lose],
        'neutralLayers': [l['layer'] for l in neutral],
        'netRatio': round(ratio, 3),
        'confirm': {
            'trend': any(l['layer'] == '趋势' for l in win),
            'volume': any(l['layer'] == '量价' for l in win),
            'smartMoney': any(l['layer'] == '庄家' for l in win),
        },
        'unanimous': (not lose and not neutral and len(win) >= 3),
    }


# ============================================================
#  第六层：订单流（Order Flow）
# ============================================================
def layer_orderflow(m1):
    """订单流层：从 1m K 线构造主动买卖压力

    原理：单根 K 线的收盘位置决定该根量能归属哪一方
      收在最高附近 → 买方主动吃掉卖单（主动买）
      收在最低附近 → 卖方主动砸（主动卖）
    这是对逐笔成交的稳健近似（Gate /trades 只覆盖 1.4 分钟，不可用）
    """
    m = m1
    if not m or len(m) < 200:
        return None

    rows = []
    for x in m:
        try:
            o, h, l, c = float(x['o']), float(x['h']), float(x['l']), float(x['c'])
            v = float(x.get('sum') or x.get('v') or 0)
        except Exception:
            continue
        if v <= 0:
            continue
        rng = h - l
        if rng <= 0:
            pos = 0.5
        else:
            pos = (c - l) / rng        # 0=最低 1=最高
        rows.append((pos, v, o, c))

    if len(rows) < 200:
        return None

    def window(n):
        seg = rows[-n:]
        buy = sum(v * p for p, v, o, c in seg)
        sell = sum(v * (1 - p) for p, v, o, c in seg)
        tot = buy + sell
        return (buy / tot) if tot > 0 else 0.5

    r15 = window(15)
    r60 = window(60)
    r240 = window(240)

    score = 0
    notes = []
    notes.append('主动买占比：15min %.0f%% / 1h %.0f%% / 4h %.0f%%'
                 % (r15 * 100, r60 * 100, r240 * 100))

    # 短周期主导方向
    if r60 >= 0.58 and r15 >= 0.55:
        score += 2
        notes.append('短周期买盘持续占优（1h %.0f%%，15min %.0f%%）' % (r60 * 100, r15 * 100))
    elif r60 <= 0.42 and r15 <= 0.45:
        score -= 2
        notes.append('短周期卖盘持续占优（1h %.0f%%，15min %.0f%%）' % (r60 * 100, r15 * 100))

    # 大小周期背离：短周期转向但长周期未跟 → 趋势可能反转
    if r240 >= 0.56 and r15 <= 0.44:
        score -= 2
        notes.append('订单流背离：4h 偏买(%.0f%%)但 15min 急转卖(%.0f%%)，短线抛压出现'
                     % (r240 * 100, r15 * 100))
    elif r240 <= 0.44 and r15 >= 0.56:
        score += 2
        notes.append('订单流背离：4h 偏卖(%.0f%%)但 15min 急转买(%.0f%%)，短线承接出现'
                     % (r240 * 100, r15 * 100))

    # 极端单边（>68% 或 <32%）是强信号
    if r60 >= 0.68:
        score += 1
        notes.append('1h 主动买达 %.0f%%，单边扫货特征' % (r60 * 100))
    elif r60 <= 0.32:
        score -= 1
        notes.append('1h 主动卖达 %.0f%%，单边砸盘特征' % (r60 * 100))

    side = 'LONG' if score >= 2 else ('SHORT' if score <= -2 else 'NEUTRAL')
    return {
        'layer': '订单流', 'side': side, 'score': score,
        'buy15': round(r15, 3), 'buy60': round(r60, 3), 'buy240': round(r240, 3),
        'notes': notes,
        'confidence': min(100, abs(score) * 16 + 12),
    }


# ============================================================
#  第七层：缠论（分型 → 笔 → 线段 → 中枢）
# ============================================================
def _chan_fractals(cs, lb=1):
    """识别顶分型/底分型：中间 K 线的高点(低点)高于(低于)两侧

    返回 [{'idx':i,'type':'top'/'bottom','price':p,'high':h,'low':l}, ...]
    """
    out = []
    n = len(cs)
    for i in range(lb, n - lb):
        h, l = cs[i]['h'], cs[i]['l']
        is_top = all(h > cs[i-j]['h'] and h > cs[i+j]['h'] for j in range(1, lb+1))
        is_bot = all(l < cs[i-j]['l'] and l < cs[i+j]['l'] for j in range(1, lb+1))
        if is_top:
            out.append({'idx': i, 'type': 'top', 'price': h})
        elif is_bot:
            out.append({'idx': i, 'type': 'bottom', 'price': l})
    return out


def _chan_bis(fxs, min_gap=3):
    """分型连笔：顶底交替且间隔 >= min_gap，同类型取极值"""
    if not fxs:
        return []
    bis = []
    cur = fxs[0]
    for f in fxs[1:]:
        if f['type'] == cur['type']:
            # 同类型，取更极端的
            if (f['type'] == 'top' and f['price'] > cur['price']) or \
               (f['type'] == 'bottom' and f['price'] < cur['price']):
                cur = f
        else:
            if f['idx'] - cur['idx'] >= min_gap:
                bis.append({'from': cur, 'to': f,
                            'dir': 'up' if f['type'] == 'top' else 'down',
                            'range': abs(f['price'] - cur['price']),
                            'i1': cur['idx'], 'i2': f['idx']})
                cur = f
            else:
                # 间隔太近，跳过（保留原向）
                continue
    return bis


def _chan_zhongshu(bis, min_bis=3):
    """中枢：连续三笔重叠区间

    中枢 = [max(三笔最低点), min(三笔最高点)]，存在重叠即为有效中枢
    """
    if len(bis) < min_bis:
        return None
    for k in range(len(bis) - min_bis, -1, -1):
        seg = bis[k:k+min_bis]
        highs = [max(b['from']['price'], b['to']['price']) for b in seg]
        lows = [min(b['from']['price'], b['to']['price']) for b in seg]
        zg = min(highs)   # 中枢上沿
        zd = max(lows)    # 中枢下沿
        if zg > zd:
            return {'zg': zg, 'zd': zd, 'i1': seg[0]['i1'], 'i2': seg[-1]['i2'],
                    'mid': (zg + zd) / 2}
    return None


def layer_chan(cs):
    """缠论层：走势结构 + 中枢位置 + 背驰

    核心逻辑（缠中说禅）：
      1. 当前笔方向（最后一个未完成笔）
      2. 与中枢的关系：上破中枢=强势，下破=弱势，中枢内=震荡
      3. 背驰：出中枢后一笔的力度（幅度/时长）弱于进中枢那一笔 → 反转信号
    """
    if len(cs) < 80:
        return None

    fxs = _chan_fractals(cs, lb=1)
    if len(fxs) < 6:
        return None
    bis = _chan_bis(fxs, min_gap=2)
    if len(bis) < 4:
        return None

    px = cs[-1]['c']
    last = bis[-1]

    # 三类信号分开记分，避免正负相互抵消（曾出现中枢判空却被一笔加速拉回中性）
    s_bi = 0        # 当前笔延续
    s_zs = 0        # 中枢位置（主结构方向）
    s_bc = 0        # 背驰/加速（力度对比）
    notes = []

    # 1) 当前笔方向（最后一笔走向 + 价格相对其终点的位置）
    notes.append('当前笔：%s（%.4g→%.4g）'
                 % ('上行' if last['dir'] == 'up' else '下行',
                    last['from']['price'], last['to']['price']))
    if last['dir'] == 'up' and px >= last['to']['price'] * 0.995:
        s_bi = 2
        notes.append('价格站在最后一笔上行高点附近，笔延续中')
    elif last['dir'] == 'down' and px <= last['to']['price'] * 1.005:
        s_bi = -2
        notes.append('价格处于最后一笔下行低点附近，笔延续中')

    # 2) 中枢关系（结构背景分）
    zs = _chan_zhongshu(bis, 3)
    if zs:
        notes.append('中枢区间 %.4g ~ %.4g' % (zs['zd'], zs['zg']))
        if px > zs['zg']:
            s_zs = 2
            notes.append('价格站上中枢上沿（第三类买点区）')
        elif px < zs['zd']:
            s_zs = -2
            notes.append('价格跌破中枢下沿（第三类卖点区）')
        else:
            notes.append('价格在中枢内部，方向未选择，震荡格局')
    else:
        notes.append('近期无有效中枢（单边趋势中）')

    # 3) 背驰 / 加速：力度对比。这才是缠论真正的价值所在——
    #    中枢位置告诉你「在哪」，背驰告诉你「还有没有劲」。给最高权重。
    if len(bis) >= 5:
        cur_bis = bis[-1]
        prev_same = None
        for b in reversed(bis[:-1]):
            if b['dir'] == cur_bis['dir']:
                prev_same = b
                break
        if prev_same and prev_same['range'] > 0:
            ratio = cur_bis['range'] / prev_same['range']
            if ratio < 0.65:
                # 背驰 = 动能衰减，方向与前一笔相反
                s_bc = -3 if cur_bis['dir'] == 'up' else 3
                notes.append('%s背驰：本段%s力度仅为前段的 %.0f%%，动能衰竭'
                             % ('顶' if cur_bis['dir'] == 'up' else '底',
                                '上行' if cur_bis['dir'] == 'up' else '下行',
                                ratio * 100))
            elif ratio > 1.4:
                # 加速 = 顺势强化，只在顺中枢方向时才算强化
                accel = 2 if cur_bis['dir'] == 'up' else -2
                if s_zs == 0 or (accel > 0) == (s_zs > 0):
                    s_bc = accel
                    notes.append('%s力度放大至前段 %.0f%%，趋势加速'
                                 % ('上行' if cur_bis['dir'] == 'up' else '下行',
                                    ratio * 100))
                else:
                    notes.append('%s反抽力度放大至前段 %.0f%%，但仍在中枢逆向段内，不计方向'
                                 % ('上行' if cur_bis['dir'] == 'up' else '下行',
                                    ratio * 100))

    score = s_bi + s_zs + s_bc
    # 总分范围 -7~+7，门槛 ±3 要求「中枢 + 至少一个力度信号」同向才表态
    side = 'LONG' if score >= 3 else ('SHORT' if score <= -3 else 'NEUTRAL')

    # 结论要自洽：中性时必须说明是「哪两股力量抵消」，不能留局部措辞误导
    if side == 'NEUTRAL':
        if s_zs and s_bc and (s_zs > 0) != (s_bc > 0):
            notes.append('缠论结论中性：结构位置与力度信号相互抵消（%+d vs %+d）'
                         % (s_zs, s_bc))
        elif score == 0:
            notes.append('缠论结论中性：笔/中枢/背驰三项互不确认')
        else:
            notes.append('缠论结论中性：力度不足以确认方向（合计 %+d，需 ≥3）' % score)
    elif side == 'LONG':
        notes.append('缠论结论看多（合计 +%d/%d）' % (score, 7))
    else:
        notes.append('缠论结论看空（合计 %d/%d）' % (score, -7))
    return {
        'layer': '缠论', 'side': side, 'score': score,
        'scoreParts': {'笔': s_bi, '中枢': s_zs, '背驰': s_bc},
        'bisCount': len(bis),
        'zhongshu': [round(zs['zd'], 4), round(zs['zg'], 4)] if zs else None,
        'notes': notes,
        'confidence': min(100, abs(score) * 15 + 15),
    }
