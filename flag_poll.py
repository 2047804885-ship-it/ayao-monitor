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

WXPUSH_API = 'https://wxpusher.zjiecode.com/api/send/message'
STATE_FILE = 'flag_state.json'
# ⚠️ 状态独立分支：main 被 poll.yml / probe.yml / watch.yml 用 git push 占着，
#    再往 main 写必然撞车（2026-11-23 05:53 已把交易监控的 push 挤掉一次）。
STATE_BRANCH = 'flag-state'
# 当前角色：primary=主线（flag.yml）/ backup=备份线（flag_backup.yml）
ROLE = os.environ.get('FLAG_ROLE', 'primary')

# ---- 阈值 ----
# 注意：CHANGE_RATE 是「变动股数占该股东原持股的比例」，300 条样本中位数就有 1.87%，
# 用 >=1% 筛等于没筛。>=20% 才是真正的清仓式/翻倍式大幅变动。
HEAVY_AMOUNT = 1.0e8      # 单笔 1 亿元
HEAVY_RATIO = 20.0        # 变动比例 20%（大幅增减持）
ETF_HEAVY = 20.0          # ETF 单日净申购 20 亿份
SCAN_ROWS = 300           # 每次扫最近 300 条公告
MAX_PUSH_ITEMS = 6        # 单次推送最多几条，超了只推最重的
MAX_AGE_DAYS = 7          # 只推最近 7 天的公告（300 条覆盖近一个月，老公告没意义）
EXIT_MIN_NUM = 3.0e7      # 判定「退出」的最小上期持股（3000 万股），小仓位清仓不推
STANDARD_PERIODS = ('03-31', '06-30', '09-30', '12-31')   # 只认标准报告期

# ---- 可靠性 ----
# ⚠️ GitHub cron 不保证准时，高负载会整次跳过。ETF 若只比「最新 vs 前一天」，
#    被跳过那天的份额暴增会永久丢失（下次比的是新的一天 vs 昨天）。
#    故回看最近 ETF_LOOKBACK 个交易日，逐日对比，用 (code, date) 去重补推。
ETF_LOOKBACK = 5
# 数据源连续失败多少次就推一条「监控异常」告警（默认每 20 分钟一轮 → 3 次约 1 小时）
FAIL_ALERT_STREAK = 3
# 同一数据源告警的最短间隔（秒），防止 20 分钟一条刷屏
ALERT_GAP = 6 * 3600
# 心跳回写间隔：GitHub 对 public repo 的 scheduled workflow 有「60 天无 commit
# 自动禁用」规则。我们的 commit 只在推送成功时产生，长期无事件就会静默停摆。
# 故每 7 天强制回写一次状态文件，制造一次 commit。
HEARTBEAT_DAYS = 7
# 备份线判定「主线失联」的阈值：主线最近一次成功运行距今超过这么多分钟，就接管。
# 主线 cron 是 7/27/47 分（20 分钟一轮），单轮约 25 秒，45 分钟 ≈ 连丢 2 轮。
STALE_MIN = 45
# 接管提示的最短间隔（秒），防止备份线每 10 分钟推一条
TAKEOVER_NOTICE_GAP = 2 * 3600

# 季度数据完整性告警收集（只进日志，不推送）
INCOMPLETE = []

# 数据源健康标记：区分「真的无变动」和「抓取失败返回空」。
# 不区分的话接口一挂，日志照样显示「无变动（不推送）」，等于瞎了还报平安。
SRC_OK = {'increase': False, 'etf': False, 'hkma': False}

# 风向标个股 / 机构关键词
FLAG_STOCKS = {
    '601857': '中国石油',
}
FLAG_HOLDERS = ('汇金', '证券金融', '社保', '全国社会保障基金')

# 汇金 / 社保 主体全名（东财接口不支持 like，只能精确匹配）
FLAG_HOLDERS_FULL = [
    ('中央汇金投资有限责任公司', '中央汇金·投资'),
    ('中央汇金资产管理有限责任公司', '中央汇金·资管'),
    ('全国社会保障基金理事会', '社保基金理事会'),
]

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
        SRC_OK['increase'] = False
        return []
    if not d.get('success'):
        print('增减持接口返回失败:', d.get('message'))
        SRC_OK['increase'] = False
        return []
    data = ((d.get('result') or {}).get('data')) or []
    SRC_OK['increase'] = bool(data)
    return data


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
    """宽基 ETF 份额日变化（万份）

    ⚠️ 必须回看多天，不能只比「最新 vs 前一天」：
    GitHub cron 会被整次跳过，若只比最后两天，被跳过那天的暴增就永久丢了
    （下一轮比的是新的一天 vs 昨天，中间那天再也不参与比较）。
    这里逐日回看 ETF_LOOKBACK 个交易日，每对相邻日都算一次变化，
    再由调用方按 (code, date) 去重补推。
    """
    cur = fetch_etf_snapshot('')
    if not cur:
        SRC_OK['etf'] = False
        return []
    d0 = next((v['date'] for v in cur.values() if v.get('date')), '')
    if not d0:
        SRC_OK['etf'] = False
        return []
    SRC_OK['etf'] = True

    snaps = {d0: cur}
    d = d0
    for _ in range(ETF_LOOKBACK):
        d = prev_trade_date(d)
        if not d:
            break
        s = fetch_etf_snapshot(d)
        if s:
            snaps[d] = s

    out = []
    days = sorted(snaps.keys(), reverse=True)
    for i in range(len(days) - 1):
        cd, pd = days[i], days[i + 1]
        c, p = snaps[cd], snaps[pd]
        for code, v in c.items():
            pv = p.get(code)
            if not pv:
                continue
            dv = v['vol'] - pv['vol']          # 万份
            if abs(dv) < ETF_HEAVY * 10000:    # 阈值换算成万份
                continue
            out.append({
                'code': code, 'name': v['name'],
                'date': cd, 'prevDate': pd,
                'vol': v['vol'], 'prevVol': pv['vol'],
                'delta': dv,                  # 万份
                'deltaYi': dv / 10000.0,      # 亿份
            })
    # 最近的日期优先，同日按幅度
    out.sort(key=lambda z: (z['date'], abs(z['delta'])), reverse=True)
    return out


# ============================================================
#  三、汇金 / 社保 季度持仓变化（季报披露后才更新）
# ============================================================
def _query_holders(name, since):
    """拉某股东自 since 起的全部持股记录（东财不支持 like，用精确名）"""
    flt = '(HOLDER_NAME="%s")(END_DATE>=\'%s\')' % (name, since)
    url = ('https://datacenter-web.eastmoney.com/api/data/v1/get?'
           'reportName=RPT_F10_EH_HOLDERS&columns=ALL'
           '&pageNumber=1&pageSize=500&sortColumns=END_DATE&sortTypes=-1'
           '&filter=%s' % urllib.parse.quote(flt))
    try:
        d = get_json(url, headers={'Referer': 'https://data.eastmoney.com/'})
    except Exception as e:
        print('季度持仓接口失败(%s):' % name, e)
        return []
    if not d.get('success'):
        print('季度持仓返回失败(%s): %s' % (name, d.get('message')))
        return []
    return ((d.get('result') or {}).get('data')) or []


def fetch_holdings_change():
    """对比汇金/社保最近两个报告期的持股，找出变化

    ⚠️ 不能只信 HOLD_NUM_CHANGE 字段：东财对最新报告期常整片显示「不变」，
    实测 2026-09-30 期汇金资管 17 条全是「不变」。必须自己比对两期 HOLD_NUM。
    """
    import datetime
    since = (datetime.date.today()
             - datetime.timedelta(days=400)).strftime('%Y-%m-%d')
    out = []
    for name, label in FLAG_HOLDERS_FULL:
        rows = _query_holders(name, since)
        if not rows:
            continue
        # 按报告期分组。
        # ⚠️ 只认标准报告期（3-31/6-30/9-30/12-31）：东财 END_DATE 里混着大量
        #    个别公司提前披露的日期（10-23、05-29、09-11…），往往只有 1 只股票。
        #    不过滤的话「最近两期」会变成「1 只 vs 8 只」，对比结果全是假信号。
        by_date = {}
        for x in rows:
            d = str(x.get('END_DATE'))[:10]
            if not d.endswith(STANDARD_PERIODS):
                continue
            by_date.setdefault(d, {})[str(x.get('SECURITY_CODE'))] = x
        dates = sorted(by_date.keys(), reverse=True)
        if len(dates) < 2:
            continue
        cur_d, prev_d = dates[0], dates[1]
        cur, prev = by_date[cur_d], by_date[prev_d]

        # ⚠️ 数据完整性体检：东财个别报告期披露只数会突然掉到历史峰值的零头。
        #    实测「中央汇金资产管理」2025Q1~2026Q1 稳定在 143~152 只，到
        #    2026-06-30 / 2026-09-30 只剩 16 只——一个季度清仓 127 只票会是
        #    市场级新闻，故判为东财该两期数据不全，而非真实退出。
        #    这里只记告警、不推送：告警会写进 Actions 日志，方便发现「静默其实是瞎了」。
        hist_max = max(len(v) for v in by_date.values())
        if hist_max and len(cur) < hist_max * 0.5:
            peak_d = [d for d in by_date if len(by_date[d]) == hist_max][0]
            INCOMPLETE.append('%s %s 仅 %d 只 / 历史峰值 %d 只（%s）'
                              % (label, cur_d, len(cur), hist_max, peak_d))

        for code, x in cur.items():
            try:
                n_new = float(x.get('HOLD_NUM') or 0)
            except Exception:
                continue
            if n_new <= 0:
                continue
            old = prev.get(code)
            if not old:
                # 新进：上期完全没有这只票。但若上期数据不完整，会误判成「新进」，
                # 故只在两期条数接近时才认。
                if len(prev) >= len(cur) * 0.7:
                    out.append({
                        'kind': '新进', 'who': label, 'code': code,
                        'name': x.get('SECURITY_NAME_ABBR'),
                        'num': n_new, 'oldNum': 0, 'ratio': x.get('HOLD_NUM_RATIO'),
                        'curDate': cur_d, 'prevDate': prev_d,
                    })
                continue
            try:
                n_old = float(old.get('HOLD_NUM') or 0)
            except Exception:
                continue
            if n_old <= 0:
                continue
            if n_new > n_old * 1.02:
                kind = '增持'
            elif n_new < n_old * 0.98:
                kind = '减持'
            else:
                continue
            out.append({
                'kind': kind, 'who': label, 'code': code,
                'name': x.get('SECURITY_NAME_ABBR'),
                'num': n_new, 'oldNum': n_old, 'ratio': x.get('HOLD_NUM_RATIO'),
                'curDate': cur_d, 'prevDate': prev_d,
            })

        # 退出（清仓/跌出前十大）：上期有、这期没了。这是很强的信号，
        # 但极易被「当期数据不全」误判——实测 2026-09-30 汇金资管只披露 17 条，
        # 而上期有上百条，一对比就会冒出几十条假「退出」。故加双重保护：
        #   ① 当期条数 >= 上期的 70%（数据完整性）
        #   ② 上期持股 >= EXIT_MIN_NUM 股（小仓位退出无意义）
        if len(cur) >= len(prev) * 0.7:
            for code, x in prev.items():
                if code in cur:
                    continue
                try:
                    n_old = float(x.get('HOLD_NUM') or 0)
                except Exception:
                    continue
                if n_old < EXIT_MIN_NUM:
                    continue
                out.append({
                    'kind': '退出', 'who': label, 'code': code,
                    'name': x.get('SECURITY_NAME_ABBR'),
                    'num': 0, 'oldNum': n_old, 'ratio': x.get('HOLD_NUM_RATIO'),
                    'curDate': cur_d, 'prevDate': prev_d,
                })

    # 排序：退出/减持这类「减少」的信号优先（风险提示优先），再按变动股数
    def _abs_delta(z):
        return abs((z['num'] or 0) - (z['oldNum'] or 0))

    out.sort(key=lambda z: (
        0 if z['kind'] in ('退出', '减持') else 1,
        -_abs_delta(z)))
    return out


# ============================================================
#  五、香港金管局 · 港元干预（每日，全球唯一能做到"实时"的政府买卖美元）
# ============================================================
# 联系汇率制度（7.75 强方 / 7.85 弱方）下：
#   总结余增加 → 金管局买入美元、投放港元 → 资金流入港元
#   总结余减少 → 金管局卖出美元、回收港元 → 资金流出港元
# 总结余（Aggregate Balance）每个交易日收盘后公布，是「政府买卖美元」最直接的证据。
HKMA_LIQ = ('https://api.hkma.gov.hk/public/market-data-and-statistics/'
            'daily-monetary-statistics/daily-figures-interbank-liquidity')
HKMA_HEAVY = 5.0      # 总结余日变化 ≥ 5 亿港元才推（低于此属日常波动）
HKMA_DAYS = 14        # 只推最近 14 天的干预（100 天历史里老数据没意义）


def fetch_hkma():
    """抓香港金管局每日总结余，算日变化"""
    import datetime
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=HKMA_DAYS)).strftime('%Y-%m-%d')
    try:
        d = get_json(HKMA_LIQ, timeout=30)
    except Exception as e:
        print('HKMA 接口失败:', e)
        SRC_OK['hkma'] = False
        return []
    recs = (d.get('result') or {}).get('records') or []
    if len(recs) < 2:
        SRC_OK['hkma'] = False
        return []
    SRC_OK['hkma'] = True
    out = []
    # ⚠️ records 按日期倒序（recs[0] 最新）。所以「较前一日的变化」是
    #    recs[i] 减 recs[i+1]。写成 recs[i-1] 会在 i=0 时取到 recs[-1]
    #    （100 天前最老那条），算出假变化。
    for i in range(len(recs) - 1):
        cur, prev = recs[i], recs[i + 1]
        if str(cur.get('end_of_date')) < cutoff:
            continue
        try:
            c = float(cur.get('closing_balance') or 0) / 100.0    # 百万 → 亿
            p = float(prev.get('closing_balance') or 0) / 100.0
        except Exception:
            continue
        dv = c - p
        if abs(dv) < HKMA_HEAVY:
            continue
        out.append({
            'date': cur.get('end_of_date'),
            'prevDate': prev.get('end_of_date'),
            'cur': c, 'prev': p, 'delta': dv,
            'hiborOn': cur.get('hibor_overnight'),
            'hibor1m': cur.get('hibor_fixing_1m'),
            'baseRate': cur.get('disc_win_base_rate'),
            'weak': cur.get('cu_weakside'),
            'strong': cur.get('cu_strongside'),
            'inflow': dv > 0,
        })
    return out


# ============================================================
#  四、状态去重
# ============================================================
# ⚠️ 状态必须存在独立分支，绝不能落 main。
#    poll.yml / probe.yml / watch.yml 三个工作流都用 `git push` 写 main，
#    之前 flag 的 Contents API 也写 main，实测 2026-11-23 05:53 直接把
#    poll.yml 的 push 挤掉：`! [rejected] main -> main (fetch first)`，
#    交易监控那次快照没提交成功。迁到 flag-state 分支后两边物理隔离。
def fetch_state_from_branch():
    """从 flag-state 分支读状态；读不到返回 None（与「空状态」区分开）"""
    token = os.environ.get('GH_TOKEN', '').strip()
    repo = os.environ.get('GH_REPO', '').strip()
    if not token or not repo:
        return None
    api = ('https://api.github.com/repos/%s/contents/%s?ref=%s'
           % (repo, STATE_FILE, STATE_BRANCH))
    try:
        d = get_json(api, headers={'Authorization': 'token ' + token,
                                   'Accept': 'application/vnd.github+json'})
        raw = base64.b64decode(d.get('content') or '')
        return json.loads(raw.decode('utf-8'))
    except Exception as e:
        print('读 flag-state 分支状态失败:', str(e)[:80])
        return None


def load_state():
    # 1) 本地文件（本地调试用；Actions checkout 的是 main，正常情况下没有）
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        pass
    # 2) 独立分支
    st = fetch_state_from_branch()
    if isinstance(st, dict):
        return st
    print('无历史状态，按全新开始（首轮会把存量都推一遍）')
    return {'pushed': {}, 'etf': {}}


def save_state(st):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('save_state err:', e)


def push_state_to_repo():
    """把状态文件写回 flag-state 分支（Contents API，避免 git 半截文件）

    409 说明有另一条线刚写过，重新取 sha 再试。写失败只影响去重、
    不影响已发出的推送，所以不抛异常。
    """
    token = os.environ.get('GH_TOKEN', '').strip()
    repo = os.environ.get('GH_REPO', '').strip()
    if not token or not repo:
        print('无 GH_TOKEN/GH_REPO，跳过状态回写')
        return
    try:
        content = open(STATE_FILE, 'rb').read()
    except Exception as e:
        print('状态文件读不到，跳过回写:', e)
        return
    b64 = base64.b64encode(content).decode()
    api = 'https://api.github.com/repos/%s/contents/%s' % (repo, STATE_FILE)
    hdr = {'Authorization': 'token ' + token,
           'Accept': 'application/vnd.github+json'}
    for attempt in range(3):
        sha = None
        try:
            d = get_json(api + '?ref=' + STATE_BRANCH, headers=hdr)
            sha = d.get('sha')
        except Exception:
            pass
        payload = {'message': 'chore: flag_state %s [%s]' % (
                       time.strftime('%Y-%m-%d %H:%M'), ROLE),
                   'content': b64, 'branch': STATE_BRANCH}
        if sha:
            payload['sha'] = sha
        req = urllib.request.Request(
            api, data=json.dumps(payload).encode(), method='PUT',
            headers=dict(hdr, **{'Content-Type': 'application/json'}))
        try:
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                print('状态已回写 flag-state 分支:', r.status)
                return
        except urllib.error.HTTPError as e:
            if e.code == 409 and attempt < 2:
                print('状态回写 409（另一条线刚写过），重试 %d' % (attempt + 1))
                time.sleep(2)
                continue
            print('状态回写失败（不影响推送）: HTTP', e.code)
            return
        except Exception as e:
            print('状态回写失败（不影响推送）:', str(e)[:80])
            return


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
    # ⚠️ 必须同时标出对比基准日：回看机制下若中间某天快照抓取失败，
    #    会跨过那天做累计对比（比漏掉好），但得让用户看得出这不是单日变化。
    h.append('<div style="font-size:12px;color:#333;margin-top:3px">'
             '%s（较 %s）：%.2f 亿份 → %.2f 亿份</div>'
             % (esc(e['date']), esc(e.get('prevDate') or ''),
                e['prevVol'] / 10000.0, e['vol'] / 10000.0))
    h.append('<div style="font-size:11px;color:#888">'
             '宽基 ETF 巨额申赎是国家队进出的主要痕迹（汇金不公告）</div>')
    h.append('</div>')
    return ''.join(h)


def fmt_holding(z):
    """汇金/社保持仓变化卡片"""
    up = z['kind'] in ('增持', '新进')
    col = '#d32f2f' if up else '#2e7d32'
    bg = '#fef2f2' if up else '#f0fdf4'
    mark = '🟢' if up else '🔴'
    if z['kind'] == '新进':
        detail = '新进 %.2f 亿股' % (z['num'] / 1e8)
    elif z['kind'] == '退出':
        detail = '清仓 / 跌出前十大，原持 %.2f 亿股' % (z['oldNum'] / 1e8)
    else:
        delta = z['num'] - z['oldNum']
        pct = (delta / z['oldNum'] * 100) if z['oldNum'] else 0
        detail = '%s %.2f 亿股（%+.1f%%）' % (
            z['kind'], abs(delta) / 1e8, pct)
    h = ['<div style="margin:9px 0;padding:9px 11px;border-left:4px solid %s;'
         'background:%s;border-radius:4px">' % (col, bg)]
    h.append('<div style="font-size:15px;font-weight:700;color:%s">'
             '%s %s(%s)</div>'
             % (col, mark, esc(z['name']), esc(z['code'])))
    h.append('<div style="font-size:12px;color:#b45309;margin:2px 0">'
             '★ %s</div>' % esc(z['who']))
    h.append('<div style="font-size:12px;color:#333;margin-top:3px">%s</div>'
             % esc(detail))
    if z.get('ratio') and z['kind'] != '退出':
        h.append('<div style="font-size:11px;color:#666">'
                 '占流通股 %.2f%% · 报告期 %s（对比 %s）</div>'
                 % (float(z['ratio']), esc(z['curDate']), esc(z['prevDate'])))
    else:
        h.append('<div style="font-size:11px;color:#666">'
                 '报告期 %s（对比 %s）</div>'
                 % (esc(z['curDate']), esc(z['prevDate'])))
    h.append('</div>')
    return ''.join(h)


def fmt_hkma(z):
    """香港金管局干预卡片"""
    inflow = z['inflow']
    col = '#d32f2f' if inflow else '#2e7d32'
    bg = '#fef2f2' if inflow else '#f0fdf4'
    action = '买入美元 · 投放港元' if inflow else '卖出美元 · 回收港元'
    h = ['<div style="margin:9px 0;padding:9px 11px;border-left:4px solid %s;'
         'background:%s;border-radius:4px">' % (col, bg)]
    h.append('<div style="font-size:15px;font-weight:700;color:%s">'
             '🇭🇰 香港金管局 %s %.2f 亿港元</div>'
             % (col, action, abs(z['delta'])))
    h.append('<div style="font-size:12px;color:#333;margin-top:3px">'
             '总结余 %.2f 亿 → <b>%.2f 亿</b>（%s）</div>'
             % (z['prev'], z['cur'], esc(z['date'])))
    h.append('<div style="font-size:11px;color:#666;margin-top:2px">'
             'Hibor 隔夜 %s%% · 1个月 %s%% · 贴现窗 %s%%</div>'
             % (z['hiborOn'], z['hibor1m'], z['baseRate']))
    h.append('<div style="font-size:11px;color:#888">'
             '联系汇率 %s（强方）/ %s（弱方）· 总结余变动=金管局买卖美元的直接痕迹</div>'
             % (z['strong'], z['weak']))
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

    # 每个数据源独立 try：一个接口挂了不能拖垮其他三条线。
    # 之前 fetch_increase 没包 try，它一抛异常整个 main 就崩，
    # 连金管局都推不了。

    # --- 增减持 ---
    rows, heavy, fresh, keep = [], [], [], []
    try:
        rows = fetch_increase()
        heavy = pick_heavy_increase(rows)
        fresh = [z for z in heavy if z['key'] not in pushed]
        # 限制条数：风向标必留，其余按金额
        flags = [z for z in fresh if z['flag']]
        others = [z for z in fresh if not z['flag']]
        keep = flags + others[:max(0, MAX_PUSH_ITEMS - len(flags))]
    except Exception as e:
        print('增减持处理失败:', e)

    # --- ETF ---
    # ⚠️ etf_delta() 只算一次：回看 5 天要发 6 个请求，之前打印日志时又调一遍
    #    等于白跑 6 个请求，既慢又容易被上交所限流。
    etfs = []
    etf_raw = []
    try:
        etf_raw = etf_delta()
        for e in etf_raw:
            k = '%s|%s' % (e['code'], e['date'])
            if k not in etf_done:
                etfs.append((k, e))
    except Exception as e:
        print('ETF 处理失败:', e)

    # --- 汇金/社保 季度持仓变化（报告期变化才推）---
    holds = []
    hold_done = st.setdefault('hold', {})
    try:
        for z in fetch_holdings_change():
            k = '%s|%s|%s' % (z['who'], z['code'], z['curDate'])
            if k not in hold_done:
                holds.append((k, z))
    except Exception as e:
        print('季度持仓处理失败:', e)
    # 季度数据一次可能几十条，只推最重的前 8 条
    holds.sort(key=lambda kv: -abs((kv[1]['num'] or 0) - (kv[1]['oldNum'] or 0)))
    holds = holds[:8]

    # --- 香港金管局干预（每日）---
    hk = []
    hk_done = st.setdefault('hkma', {})
    try:
        for z in fetch_hkma():
            k = 'HK|%s' % z['date']
            if k not in hk_done:
                hk.append((k, z))
    except Exception as e:
        print('金管局处理失败:', e)
    hk.sort(key=lambda kv: kv[1]['date'], reverse=True)   # 最近的优先，别推几个月前的
    hk = hk[:4]

    print('增减持: 扫描 %d 条 -> 重量级 %d 条 -> 新增 %d 条（推送 %d 条）'
          % (len(rows), len(heavy), len(fresh), len(keep)))
    print('ETF: 回看 %d 个交易日, 异动 %d 个 -> 新增 %d 个'
          % (ETF_LOOKBACK, len(etf_raw), len(etfs)))
    print('汇金/社保持仓: %s' % (
        ('新增变化 %d 条' % len(holds)) if holds else '无变动（不推送）'))
    if INCOMPLETE:
        print('[告警] 季度数据不全，以下主体本期披露只数不足历史峰值一半，'
              '「无变动」可能只是看不到：')
        for w in INCOMPLETE:
            print('  - ' + w)
    print('香港金管局: %s' % (
        ('新增干预 %d 条' % len(hk)) if hk else '无变动（不推送）'))

    # --- 数据源健康检查 ---
    # 接口挂掉时各 fetch 会返回空，日志看起来和「真的无变动」一模一样。
    # 这里按源记连续失败次数，连续 FAIL_ALERT_STREAK 次就主动推告警，
    # 让「监控瞎了」这件事本身变成一条推送。
    health = st.setdefault('health', {})
    bad = []
    for src, label in (('increase', '东财增减持'), ('etf', '上交所ETF份额'),
                       ('hkma', '香港金管局')):
        h = health.setdefault(src, {'fail': 0, 'lastOk': 0, 'lastAlert': 0})
        if SRC_OK[src]:
            h['fail'] = 0
            h['lastOk'] = now
        else:
            h['fail'] = int(h.get('fail') or 0) + 1
            # 同一数据源最短告警间隔 6 小时，否则每 20 分钟一条会变成骚扰
            if (h['fail'] >= FAIL_ALERT_STREAK
                    and now - int(h.get('lastAlert') or 0) > ALERT_GAP):
                bad.append((label, h['fail'], h['lastOk'], src))
    if bad:
        print('[告警] 数据源连续失败:', [(b[0], b[1]) for b in bad])

    # --- 备份线接管提示 ---
    # 只在 ROLE=backup（flag_backup.yml）时出现。主线恢复后自动消失，
    # 同一轮接管最多每 2 小时提示一次，避免每 10 分钟刷一条。
    banner = ''
    takeover = False
    if ROLE == 'backup':
        if now - int(st.get('lastTakeover') or 0) > TAKEOVER_NOTICE_GAP:
            banner = ('<div style="padding:8px 11px;background:#fef3c7;'
                      'border-left:4px solid #f59e0b;border-radius:4px;'
                      'margin:6px 0;font-size:13px">'
                      '<b>⚠️ 主线失联，备份线已接管</b><br>'
                      '<span style="color:#666">主线 flag.yml 超过 %d 分钟没有成功运行，'
                      '本条由备份线发出。主线恢复后自动交还。</span></div>'
                      % STALE_MIN)
            takeover = True
        print('角色=备份线，接管=%s' % takeover)

    if not keep and not etfs and not holds and not hk and not bad and not takeover:
        print('done: 无新增，不推送 cost=%.1fs' % (time.time() - t0))
        save_state(st)
        # 心跳：GitHub 对 public repo 有「60 天无 commit 自动禁用 schedule」规则，
        # 而我们的 commit 只在推送成功时产生。长期无事件 → 无 commit → 被禁用 → 永久停摆。
        # 所以每 HEARTBEAT_DAYS 天强制回写一次状态文件，制造一次 commit 保活。
        if now - int(st.get('lastBeat') or 0) > HEARTBEAT_DAYS * 86400:
            st['lastBeat'] = now
            save_state(st)
            print('心跳回写（防 GitHub 60 天无 commit 自动禁用）')
            push_state_to_repo()
        return

    # --- 组装推送 ---
    # 顺序：数据源异常（最重要，说明后面几条可能是瞎的）> 香港金管局
    #       > 国家队 ETF > 汇金/社保季度 > 个股增减持
    parts = []
    if banner:
        parts.append(banner)
    if bad:
        rows_html = []
        for label, streak, lastOk, _src in bad:
            if lastOk:
                ago = (now - int(lastOk)) / 3600.0
                tail = '上次成功 %.1f 小时前' % ago
            else:
                tail = '从未成功过'
            rows_html.append(
                '<div style="padding:6px 10px;background:#fff7ed;border-left:3px'
                ' solid #f59e0b;margin:4px 0;font-size:13px">'
                '<b>%s</b> 连续失败 %d 次 · %s</div>' % (label, streak, tail))
        parts.append('<div style="font-size:13px;color:#b45309;margin:6px 0">'
                     '<b>⚠️ 监控数据源异常</b>（这几条线暂时是瞎的，'
                     '不代表「无变动」）</div>')
        parts.extend(rows_html)
    if hk:
        parts.append('<div style="font-size:13px;color:#555;margin:6px 0">'
                     '<b>🇭🇰 香港金管局 · 政府买卖美元</b>（每日公布）</div>')
        parts.extend(fmt_hkma(z) for _, z in hk)
    if etfs:
        parts.append('<div style="font-size:13px;color:#555;margin:10px 0 6px">'
                     '<b>🏛 国家队 ETF 动向</b>（汇金/国家队代理指标）</div>')
        parts.extend(fmt_etf(e) for _, e in etfs)
    if holds:
        parts.append('<div style="font-size:13px;color:#555;margin:10px 0 6px">'
                     '<b>🏦 汇金 / 社保 季度持仓变化</b>（季报披露）</div>')
        parts.extend(fmt_holding(z) for _, z in holds)
    if keep:
        parts.append('<div style="font-size:13px;color:#555;margin:10px 0 6px">'
                     '<b>上市公司增减持</b>（重量级，按金额排序）</div>')
        parts.extend(fmt_item(z) for z in keep)

    # 摘要：按重要性取第一条
    if bad:
        summary = '⚠️监控异常：%s 连续失败' % bad[0][0]
    elif hk:
        z0 = hk[0][1]
        summary = '🇭🇰金管局%s%.1f亿港元' % (
            '买入美元' if z0['inflow'] else '卖出美元', abs(z0['delta']))
    elif etfs:
        e0 = etfs[0][1]
        summary = '🏛 %s %s %.1f亿份' % (
            e0['name'], '净申购' if e0['delta'] > 0 else '净赎回', abs(e0['deltaYi']))
    elif holds:
        h0 = holds[0][1]
        summary = '🏦 %s %s %s' % (h0['who'], h0['kind'], h0['name'])
    else:
        top = keep[0]
        summary = '%s %s %s · 约%s' % (top['dir'], top['name'],
                                       '★风向标' if top['flag'] else '',
                                       yi(top['amount']))
    if holds and not hk and not bad:
        summary += ' | 持仓变动%d条' % len(holds)

    if bad:
        title = '⚠️ 风向标监控异常'
    else:
        title = '📊 风向标异动 · 增减持/国家队'
    res = wxpush(title, ''.join(parts), summary)
    print('推送结果:', res)

    if res == 'ok':
        for z in keep:
            pushed[z['key']] = now
        for k, _ in etfs:
            etf_done[k] = now
        for k, _ in holds:
            hold_done[k] = now
        for k, _ in hk:
            hk_done[k] = now
        # 告警推送成功后记录时间，6 小时内不再重复告警
        for _label, _streak, _lastOk, src in bad:
            health.setdefault(src, {})['lastAlert'] = now
        if takeover:
            st['lastTakeover'] = now
        # 状态瘦身：只保留 30 天
        cut = now - 30 * 86400
        st['pushed'] = {k: v for k, v in pushed.items() if v > cut}
        st['etf'] = {k: v for k, v in etf_done.items() if v > cut}
        st['hold'] = {k: v for k, v in hold_done.items() if v > cut}
        st['hkma'] = {k: v for k, v in hk_done.items() if v > cut}
        st['lastBeat'] = now      # 这次 commit 也算一次保活
        save_state(st)
        push_state_to_repo()

    print('done: 金管局%d 增减持%d ETF%d 持仓%d 异常%d cost=%.1fs push=%s'
          % (len(hk), len(keep), len(etfs), len(holds), len(bad),
             time.time() - t0, res))


if __name__ == '__main__':
    main()
