# -*- coding: utf-8 -*-
"""李大霄「股市四大风向标」实时监控

四大风向标（《李大霄投资战略》第三版）：
  1. 中央汇金 —— 市场超级风向标。汇金买 ETF 不发公告，用宽基 ETF 份额暴增倒推
  2. 社保基金 —— 市场超级风向标。只按季报披露，无实时源，只能季报时点监控
  3. 上市公司管理层增减持 —— 个股风向标。东财增减持库，每日更新
  4. 中国石油增减持 —— 个股风向标。李大霄点名的标志性标的

推送原则：只推重量级，不刷屏。
  - 中石油/汇金/社保 相关 → 无条件推
  - 单笔金额 ≥ HEAVY_AMOUNT（默认 1 亿） → 推
  - 变动比例 ≥ HEAVY_RATIO（默认 1%） → 推
  - 宽基 ETF 单日净申购/赎回 ≥ ETF_HEAVY（默认 20 亿份） → 推

运行环境：GitHub Actions（无需本机常开）。状态存 flag_state.json 去重。
"""
import base64
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
CTX = None
try:
    import ssl
    CTX = ssl.create_default_context()
    CTX.check_hostname = False
    CTX.verify_mode = ssl.CERT_NONE
except Exception:
    pass

WXPUSH_API = 'https://www.wxpusher.com/msg/send/webhook/send'
STATE_FILE = 'flag_state.json'

# ---- 阈值 ----
# 注意：CHANGE_RATE 是「变动股数占该股东原持股的比例」，300 条样本中位数就有 1.87%，
# 用 >=1% 筛等于没筛。>=20% 才是真正的清仓式/翻倍式大幅变动。
HEAVY_AMOUNT = 1.0e8      # 单笔 1 亿元
HEAVY_RATIO = 20.0        # 变动比例 20%（大幅增减持）
ETF_HEAVY = 20.0          # ETF 单日净申购 20 亿份
SCAN_ROWS = 300           # 每次扫最近 300 条公告
MAX_PUSH_ITEMS = 6        # 单次推送最多几条，超了只推最重的
MAX_AGE_DAYS = 7          # 只推最近 7 天的公告（300 条覆盖近一个月，老公告没意义）

# 风向标个股 / 机构关键词
FLAG_STOCKS = {
    '601857': '中国石油',
}
FLAG_HOLDERS = ('汇金', '证券金融', '社保', '全国社会保障基金')

# 宽基 ETF（国家队主要买入标的）
BROAD_ETF = {
    '510300': '沪深300ETF',
    '510310': '沪深300ETF(易方达)',
    '510050': '上证50ETF',
    '510500': '中证500ETF',
    '510330': '沪深300ETF(华夏)',
    '159919': '沪深300ETF(嘉实)',
}


def get_json(url, timeout=25, headers=None, data=None):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


# ============================================================
#  一、上市公司 / 中石油增减持
# ============================================================
def fetch_increase():
    """东财增减持库，按公告日期倒序取最近 SCAN_ROWS 条"""
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?'
           'reportName=RPT_SHARE_HOLDER_INCREASE&columns=ALL'
           '&pageNumber=1&pageSize=%d&sortColumns=NOTICE_DATE&sortTypes=-1'
           % SCAN_ROWS)
    try:
        d = get_json(url, headers={'Referer': 'https://data.eastmoney.com/'})
    except Exception as e:
        print('增减持接口失败:', e)
        return []
    if not d.get('success'):
        print('增减持接口返回失败:', d.get('message'))
        return []
    return ((d.get('result') or {}).get('data')) or []


def amount_of(x):
    """估算变动金额（元）：CHANGE_NUM 单位万股"""
    try:
        num = float(x.get('CHANGE_NUM') or 0)          # 万股
        px = float(x.get('TRADE_AVERAGE_PRICE') or x.get('CLOSE_PRICE') or 0)
        return abs(num) * 10000 * px
    except Exception:
        return 0.0


def is_flag_item(x):
    """是否属于风向标本体（中石油 / 汇金 / 社保）"""
    code = str(x.get('SECURITY_CODE') or '')
    holder = str(x.get('HOLDER_NAME') or '')
    if code in FLAG_STOCKS:
        return True
    for kw in FLAG_HOLDERS:
        if kw in holder:
            return True
    return False


def pick_heavy_increase(rows):
    """筛出重量级增减持，并按股票去重（同一只票只留最重的一条）

    ⚠️ 必须按股票去重：基金公司（如华夏基金）对同一只票会有 8 条以上
    被动持仓变动记录，不去重会瞬间占满推送，把真正的风向标挤掉。
    """
    import datetime
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=MAX_AGE_DAYS)).strftime('%Y-%m-%d')

    best = {}
    for x in rows:
        nd = str(x.get('NOTICE_DATE'))[:10]
        if nd and nd < cutoff:        # 太老的公告不推
            continue
        amt = amount_of(x)
        try:
            ratio = abs(float(x.get('CHANGE_RATE') or 0))
        except Exception:
            ratio = 0.0
        flag = is_flag_item(x)
        if not (flag or amt >= HEAVY_AMOUNT or ratio >= HEAVY_RATIO):
            continue
        code = str(x.get('SECURITY_CODE') or '')
        key = '%s|%s|%s|%s' % (
            code, str(x.get('NOTICE_DATE'))[:10],
            x.get('HOLDER_NAME'), x.get('DIRECTION'))
        item = {
            'key': key,
            'code': code,
            'name': x.get('SECURITY_NAME_ABBR'),
            'holder': x.get('HOLDER_NAME'),
            'dir': x.get('DIRECTION'),
            'num': float(x.get('CHANGE_NUM') or 0),
            'ratio': ratio,
            'price': float(x.get('TRADE_AVERAGE_PRICE') or x.get('CLOSE_PRICE') or 0),
            'amount': amt,
            'date': str(x.get('NOTICE_DATE'))[:10],
            'flag': flag,
            'isPetro': code in FLAG_STOCKS,
            'isFund': any(k in str(x.get('HOLDER_NAME') or '')
                          for k in ('基金管理', '资产管理', '证券投资基金')),
        }
        # 同一只票保留金额最大的一条（风向标优先）
        old = best.get(code)
        if old is None or (item['flag'], item['amount']) > (old['flag'], old['amount']):
            best[code] = item

    out = list(best.values())
    # 排序：风向标 > 非基金（真金白银的产业资本）> 基金（多为ETF被动申赎）> 金额
    out.sort(key=lambda z: (
        0 if z['flag'] else 1,
        0 if not z['isFund'] else 1,
        -z['amount']))
    return out


# ============================================================
#  二、宽基 ETF 份额（汇金 / 国家队代理指标）
# ============================================================
def fetch_etf_snapshot(date=''):
    """上交所 ETF 份额快照。date='' 取最新交易日"""
    url = ('http://query.sse.com.cn/commonQuery.do?'
           'sqlId=COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L&STAT_DATE=%s' % date)
    try:
        d = get_json(url, headers={'Referer': 'http://www.sse.com.cn/'})
    except Exception as e:
        print('ETF 份额接口失败(%s):' % (date or '最新'), e)
        return {}
    res = {}
    for x in d.get('result') or []:
        code = str(x.get('SEC_CODE') or '')
        if code not in BROAD_ETF:
            continue
        try:
            res[code] = {
                'name': BROAD_ETF[code],
                'vol': float(x.get('TOT_VOL') or 0),      # 万份
                'date': x.get('STAT_DATE'),
            }
        except Exception:
            continue
    return res


def prev_trade_date(dstr):
    """粗略取前一自然日（周末回溯到周五）"""
    import datetime
    try:
        d = datetime.datetime.strptime(dstr, '%Y-%m-%d')
    except Exception:
        return ''
    for _ in range(5):
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:      # 周一~周五
            return d.strftime('%Y-%m-%d')
    return ''


def etf_delta():
    """宽基 ETF 份额日变化（万份）"""
    cur = fetch_etf_snapshot('')
    if not cur:
        return []
    d0 = next((v['date'] for v in cur.values() if v.get('date')), '')
    if not d0:
        return []
    prev = fetch_etf_snapshot(prev_trade_date(d0))
    out = []
    for code, v in cur.items():
        p = prev.get(code)
        if not p:
            continue
        dv = v['vol'] - p['vol']          # 万份
        if abs(dv) < ETF_HEAVY * 10000:   # 阈值换算成万份
            continue
        out.append({
            'code': code, 'name': v['name'],
            'date': v['date'], 'prevDate': p['date'],
            'vol': v['vol'], 'prevVol': p['vol'],
            'delta': dv,                  # 万份
            'deltaYi': dv / 10000.0,      # 亿份
        })
    out.sort(key=lambda z: -abs(z['delta']))
    return out


# ============================================================
#  三、状态去重
# ============================================================
def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'pushed': {}, 'etf': {}}


def save_state(st):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('save_state err:', e)


def push_state_to_repo():
    """把状态文件写回仓库（用 Contents API，避免 git 半截文件）"""
    token = os.environ.get('GH_TOKEN', '').strip()
    repo = os.environ.get('GH_REPO', '').strip()
    if not token or not repo:
        print('无 GH_TOKEN/GH_REPO，跳过状态回写')
        return
    try:
        content = open(STATE_FILE, 'rb').read()
        b64 = base64.b64encode(content).decode()
        api = 'https://api.github.com/repos/%s/contents/%s' % (repo, STATE_FILE)
        sha = None
        try:
            d = get_json(api, headers={'Authorization': 'token ' + token,
                                       'Accept': 'application/vnd.github+json'})
            sha = d.get('sha')
        except Exception:
            pass
        payload = {'message': 'chore: update flag_state %s' %
                              time.strftime('%Y-%m-%d %H:%M'),
                   'content': b64, 'branch': 'main'}
        if sha:
            payload['sha'] = sha
        req = urllib.request.Request(
            api, data=json.dumps(payload).encode(), method='PUT',
            headers={'Authorization': 'token ' + token,
                     'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
            print('状态已回写仓库:', r.status)
    except Exception as e:
        print('状态回写失败（不影响推送）:', e)


# ============================================================
#  四、推送
# ============================================================
def esc(t):
    return (str(t).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def yi(v):
    """元 → 亿元 / 万元"""
    if v >= 1e8:
        return '%.2f 亿元' % (v / 1e8)
    return '%.0f 万元' % (v / 1e4)


def fmt_item(z):
    up = z['dir'] == '增持'
    col = '#d32f2f' if up else '#2e7d32'       # A股习惯：增持红、减持绿
    bg = '#fef2f2' if up else '#f0fdf4'
    mark = '🟢' if up else '🔴'
    if z['isPetro']:
        tag = '★ 中国石油 · 书中点名的风向标'
    elif z['flag']:
        tag = '★ 风向标（汇金/社保相关）'
    elif z['isFund']:
        tag = '基金持仓变动（多为 ETF 被动申赎，非主动增持）'
    else:
        tag = ''
    h = ['<div style="margin:9px 0;padding:9px 11px;border-left:4px solid %s;'
         'background:%s;border-radius:4px">' % (col, bg)]
    h.append('<div style="font-size:15px;font-weight:700;color:%s">'
             '%s %s %s(%s)</div>'
             % (col, mark, esc(z['dir']), esc(z['name']), esc(z['code'])))
    if tag:
        tc = '#b45309' if tag.startswith('★') else '#888'
        h.append('<div style="font-size:11px;color:%s;margin:2px 0">%s</div>'
                 % (tc, tag))
    h.append('<div style="font-size:12px;color:#333;margin-top:3px">'
             '%s：%s 万股 · 占其持仓 %+.2f%% · 均价 %.2f</div>'
             % (esc(z['holder']), '%.2f' % z['num'],
                -z['ratio'] if not up else z['ratio'], z['price']))
    h.append('<div style="font-size:12px;color:#666">金额约 <b>%s</b> · %s</div>'
             % (yi(z['amount']), esc(z['date'])))
    h.append('</div>')
    return ''.join(h)


def fmt_etf(e):
    inflow = e['delta'] > 0
    col = '#d32f2f' if inflow else '#2e7d32'
    bg = '#fef2f2' if inflow else '#f0fdf4'
    h = ['<div style="margin:9px 0;padding:9px 11px;border-left:4px solid %s;'
         'background:%s;border-radius:4px">' % (col, bg)]
    h.append('<div style="font-size:15px;font-weight:700;color:%s">'
             '🏛 %s %s %.2f 亿份</div>'
             % (col, esc(e['name']), '净申购' if inflow else '净赎回',
                abs(e['deltaYi'])))
    h.append('<div style="font-size:12px;color:#333;margin-top:3px">'
             '%s：%.2f 亿份 → %.2f 亿份</div>'
             % (esc(e['date']), e['prevVol'] / 10000.0, e['vol'] / 10000.0))
    h.append('<div style="font-size:11px;color:#888">'
             '宽基 ETF 巨额申赎是国家队进出的主要痕迹（汇金不公告）</div>')
    h.append('</div>')
    return ''.join(h)


def wxpush(title, body_html, summary):
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        return 'skipped (no WXPUSHER_TOKEN/WXPUSHER_UID)'
    payload = {
        'appToken': token,
        'content': ('<h3>%s</h3>' % title) + body_html +
                   "<p style='color:#999;font-size:11px'>数据源：东方财富增减持库 / "
                   "上交所ETF份额 · %s<br>风向标仅作参考，不构成投资建议</p>"
                   % time.strftime('%m-%d %H:%M', time.localtime(time.time() + 8 * 3600)),
        'summary': summary[:95],
        'contentType': 3,
        'uids': [uid],
        'url': 'https://2047804885-ship-it.github.io/ayao-monitor/index.html',
    }
    req = urllib.request.Request(WXPUSH_API, data=json.dumps(payload).encode(),
                                headers={'Content-Type': 'application/json'})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=15, context=CTX)
                          .read().decode())
        ok = resp.get('code') == 1000
        print('wxpush:', resp.get('code'), resp.get('msg'))
        return 'ok' if ok else 'fail:' + str(resp.get('msg'))
    except Exception as e:
        print('wxpush error:', e)
        return 'err:' + str(e)[:80]


# ============================================================
#  主流程
# ============================================================
def main():
    t0 = time.time()
    st = load_state()
    pushed = st.setdefault('pushed', {})
    etf_done = st.setdefault('etf', {})
    now = int(time.time())

    # --- 增减持 ---
    rows = fetch_increase()
    heavy = pick_heavy_increase(rows)
    fresh = [z for z in heavy if z['key'] not in pushed]
    # 限制条数：风向标必留，其余按金额
    flags = [z for z in fresh if z['flag']]
    others = [z for z in fresh if not z['flag']]
    keep = flags + others[:max(0, MAX_PUSH_ITEMS - len(flags))]

    # --- ETF ---
    etfs = []
    for e in etf_delta():
        k = '%s|%s' % (e['code'], e['date'])
        if k not in etf_done:
            etfs.append((k, e))

    print('增减持: 扫描 %d 条 -> 重量级 %d 条 -> 新增 %d 条（推送 %d 条）'
          % (len(rows), len(heavy), len(fresh), len(keep)))
    print('ETF: 异动 %d 个 -> 新增 %d 个' % (len(etf_delta()), len(etfs)))

    if not keep and not etfs:
        print('done: 无新增，不推送 cost=%.1fs' % (time.time() - t0))
        # 仍然回写状态（记录已扫描，避免重复判断）
        save_state(st)
        return

    # --- 组装推送 ---
    # 国家队 ETF 放最前：汇金/国家队是书里的「超级风向标」，优先级高于个股
    parts = []
    if etfs:
        parts.append('<div style="font-size:13px;color:#555;margin:6px 0">'
                     '<b>🏛 国家队 ETF 动向</b>（汇金/国家队代理指标）</div>')
        parts.extend(fmt_etf(e) for _, e in etfs)
    if keep:
        parts.append('<div style="font-size:13px;color:#555;margin:10px 0 6px">'
                     '<b>上市公司增减持</b>（重量级，按金额排序）</div>')
        parts.extend(fmt_item(z) for z in keep)

    # 摘要：ETF 异动优先，其次最大额增减持
    if etfs:
        e0 = etfs[0][1]
        summary = '🏛 %s %s %.1f亿份' % (
            e0['name'], '净申购' if e0['delta'] > 0 else '净赎回', abs(e0['deltaYi']))
        if keep:
            summary += ' | %s %s %s' % (keep[0]['dir'], keep[0]['name'],
                                        yi(keep[0]['amount']))
    else:
        top = keep[0]
        summary = '%s %s %s · 约%s' % (top['dir'], top['name'],
                                       '★风向标' if top['flag'] else '',
                                       yi(top['amount']))

    title = '📊 风向标异动 · 增减持/国家队'
    res = wxpush(title, ''.join(parts), summary)
    print('推送结果:', res)

    if res == 'ok':
        for z in keep:
            pushed[z['key']] = now
        for k, _ in etfs:
            etf_done[k] = now
        # 状态瘦身：只保留 30 天
        cut = now - 30 * 86400
        st['pushed'] = {k: v for k, v in pushed.items() if v > cut}
        st['etf'] = {k: v for k, v in etf_done.items() if v > cut}
        save_state(st)
        push_state_to_repo()

    print('done: 增减持%d 条 ETF%d 个 cost=%.1fs push=%s'
          % (len(keep), len(etfs), time.time() - t0, res))


if __name__ == '__main__':
    main()
