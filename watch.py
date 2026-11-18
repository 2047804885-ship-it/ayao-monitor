# -*- coding: utf-8 -*-
"""熬鹰资本 · 实时成交推送（跑在 GitHub Actions 长驻任务里）

数据源：POST https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history
推送：WxPusher（微信）
状态：watchstate.json（存最后处理的 orderTime，避免重复推送）
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

PID = os.environ.get('AYAO_PID', '5075281354358777856')
CST = timezone(timedelta(hours=8))

BAPI = 'https://www.binance.com/bapi/futures/v1'
EP_ORDER = BAPI + '/friendly/future/copy-trade/lead-portfolio/order-history'
WXPUSH_API = 'https://wxpusher.zjiecode.com/api/send/message'
STATE_FILE = 'watchstate.json'
LOG_FILE = 'watchlog.txt'

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Content-Type': 'application/json',
    'clienttype': 'web',
    'lang': 'zh-CN',
    'Origin': 'https://www.binance.com',
    'Referer': 'https://www.binance.com/zh-CN/copy-trading/lead-details/' + PID,
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

POLL = int(os.environ.get('AYAO_POLL_SEC', '15'))
MAX_HOURS = float(os.environ.get('AYAO_MAX_HOURS', '5.6'))
TOKEN = os.environ.get('WXPUSHER_TOKEN', '').strip()
UID = os.environ.get('WXPUSHER_UID', '').strip()


def log(msg):
    line = '[%s] %s' % (datetime.now(CST).strftime('%m-%d %H:%M:%S'), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def trim_log(keep=300):
    try:
        with open(LOG_FILE, encoding='utf-8') as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(LOG_FILE, 'w', encoding='utf-8') as f:
                f.writelines(lines[-keep:])
    except Exception:
        pass


def post_json(url, body, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=UA, method='POST')
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode('utf-8', 'ignore'))


def fetch_orders(page_size=20):
    j = post_json(EP_ORDER, {'portfolioId': PID, 'pageNumber': 1, 'pageSize': page_size})
    if str(j.get('code')) not in ('000000', '0', 'None'):
        raise RuntimeError('api code=%s msg=%s' % (j.get('code'), j.get('message')))
    return ((j.get('data') or {}).get('list') or [])


def classify(side, ps):
    """side: BUY/SELL, positionSide: LONG/SHORT -> 开多/平多/开空/平空"""
    ps = (ps or '').upper()
    side = (side or '').upper()
    if ps == 'LONG':
        return ('开多', '🟢') if side == 'BUY' else ('平多', '🔵')
    if ps == 'SHORT':
        return ('开空', '🔴') if side == 'SELL' else ('平空', '🟠')
    return ('成交', '⚪')


def fmt_num(v, nd=2):
    try:
        v = float(v)
    except Exception:
        return str(v)
    return '{:,.{}f}'.format(v, nd)


def fmt_qty(v):
    try:
        v = float(v)
    except Exception:
        return str(v)
    if v >= 1000:
        return '{:,.2f}'.format(v)
    if v >= 1:
        return '{:,.3f}'.format(v).rstrip('0').rstrip('.')
    return '{:.6f}'.format(v).rstrip('0').rstrip('.')


def build_html(items, title_suffix=''):
    parts = ['<div style="font-family:-apple-system,\'PingFang SC\',sans-serif;font-size:14px;line-height:1.65">',
             '<div style="font-size:16px;font-weight:700;color:#1F2937;margin-bottom:8px">🐼 熬鹰资本 · 最新交易%s</div>' % title_suffix]
    for it in items:
        act, icon = classify(it.get('side'), it.get('positionSide'))
        sym = it.get('symbol') or ''
        base = it.get('baseAsset') or ''
        qty = it.get('executedQty')
        avg = it.get('avgPrice')
        val = (float(qty or 0) * float(avg or 0))
        pnl = float(it.get('totalPnl') or 0)
        ts = int(it.get('orderTime') or it.get('orderUpdateTime') or 0) / 1000.0
        tstr = datetime.fromtimestamp(ts, CST).strftime('%m-%d %H:%M:%S') if ts else ''
        col = {'开多': '#B03A2E', '平多': '#1E7A32', '开空': '#1E7A32', '平空': '#B03A2E'}.get(act, '#333')
        parts.append('<div style="padding:9px 0;border-top:1px solid #EDF0F3">')
        parts.append('<div><b style="color:%s">%s %s</b> <b>%s</b> <span style="color:#6B7280">永续</span></div>'
                     % (col, icon, act, sym))
        parts.append('<div style="color:#374151">均价 <b>%s</b>　数量 <b>%s %s</b></div>'
                     % (fmt_num(avg, 4).rstrip('0').rstrip('.'), fmt_qty(qty), base))
        parts.append('<div style="color:#374151">总价值 <b>%s USDT</b></div>' % fmt_num(val))
        if abs(pnl) > 1e-9:
            pc = '#B03A2E' if pnl > 0 else '#1E7A32'
            parts.append('<div>已实现盈亏 <b style="color:%s">%s USDT</b></div>' % (pc, fmt_num(pnl)))
        parts.append('<div style="color:#9CA3AF;font-size:12px;margin-top:2px">%s</div>' % tstr)
        parts.append('</div>')
    parts.append('<div style="color:#9CA3AF;font-size:11px;margin-top:10px">实时监控 · 每 %d 秒轮询</div>' % POLL)
    parts.append('</div>')
    return ''.join(parts)


def push(items):
    if not TOKEN or not UID:
        log('!! 缺少 WXPUSHER_TOKEN / WXPUSHER_UID，跳过推送')
        return False
    acts = [classify(i.get('side'), i.get('positionSide'))[0] + i.get('symbol', '') for i in items]
    summary = '熬鹰 %d 笔: ' % len(items) + ' / '.join(acts[:3])
    payload = {
        'appToken': TOKEN,
        'content': build_html(items),
        'summary': summary[:95],
        'contentType': 3,
        'uids': [UID],
        'url': 'https://2047804885-ship-it.github.io/ayao-monitor/market.html',
    }
    try:
        req = urllib.request.Request(WXPUSH_API, data=json.dumps(payload).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            resp = json.loads(r.read().decode('utf-8', 'ignore'))
        ok = resp.get('code') == 1000
        log('push %s: %s %s' % ('OK' if ok else 'FAIL', resp.get('code'), str(resp.get('msg'))[:60]))
        return ok
    except Exception as e:
        log('push ERR %s' % str(e)[:100])
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    st['updated'] = datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def git_commit_state(msg='state'):
    """把状态文件提交回仓库，防止任务中断后重复推送"""
    import subprocess
    try:
        trim_log()
        subprocess.run(['git', 'add', STATE_FILE, LOG_FILE], capture_output=True, timeout=30)
        c = subprocess.run(['git', 'commit', '-m', '%s %s' % (msg, datetime.now(CST).strftime('%m-%d %H:%M'))],
                           capture_output=True, text=True, timeout=60)
        if c.returncode != 0:
            return False
        for _ in range(3):
            subprocess.run(['git', 'pull', '--rebase', '--autostash', 'origin', 'main'],
                           capture_output=True, timeout=90)
            p = subprocess.run(['git', 'push'], capture_output=True, text=True, timeout=90)
            if p.returncode == 0:
                log('状态已提交')
                return True
            time.sleep(3)
    except Exception as e:
        log('git state fail: %s' % str(e)[:80])
    return False


def redispatch_next():
    """本次长驻快结束时，自动触发下一棒，保证 24 小时不断"""
    tok = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN') or ''
    repo = os.environ.get('GITHUB_REPOSITORY') or '2047804885-ship-it/ayao-monitor'
    wf = os.environ.get('AYAO_WORKFLOW', 'watch.yml')
    if not tok:
        log('无 GITHUB_TOKEN，无法自动续跑')
        return False
    url = 'https://api.github.com/repos/%s/actions/workflows/%s/dispatches' % (repo, wf)
    req = urllib.request.Request(url, data=b'{"ref":"main"}', method='POST', headers={
        'Authorization': 'Bearer ' + tok,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'ayao-watch',
        'Content-Type': 'application/json',
    })
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:
            log('已触发下一棒 (HTTP %s)' % r.status)
            return True
    except Exception as e:
        log('续跑失败: %s' % str(e)[:100])
        return False


def key_of(o):
    return '%s|%s|%s|%s' % (o.get('orderTime'), o.get('symbol'), o.get('side'), o.get('executedQty'))


def main():
    st = load_state()
    last_ts = int(st.get('lastTs') or 0)
    seen = set(st.get('seen') or [])
    first_run = last_ts == 0

    if first_run:
        orders = fetch_orders(10)
        if orders:
            last_ts = max(int(o.get('orderTime') or 0) for o in orders)
            seen = [key_of(o) for o in orders[:10]]
        save_state({'lastTs': last_ts, 'seen': seen, 'note': 'init'})
        log('首次初始化，游标 = %s（不推送历史）' % last_ts)
        git_commit_state('init')
        # 启动确认单独发一条
        if TOKEN and UID:
            try:
                body = {
                    'appToken': TOKEN,
                    'content': '<div style="font-size:15px">✅ <b>熬鹰实时推送已启动</b><br/>'
                               '<span style="color:#6B7280;font-size:12px">每 %d 秒轮询最新交易，'
                               '有新开/平仓会立即推给你</span></div>' % POLL,
                    'summary': '熬鹰实时推送已启动',
                    'contentType': 3,
                    'uids': [UID],
                }
                req = urllib.request.Request(WXPUSH_API, data=json.dumps(body).encode(),
                                             headers={'Content-Type': 'application/json'})
                urllib.request.urlopen(req, timeout=20, context=CTX).read()
                log('已发送启动确认')
            except Exception as e:
                log('启动确认失败 %s' % str(e)[:80])
        first_run = False

    t0 = time.time()
    last_commit = time.time()
    err_n = 0
    log('开始轮询，间隔 %ds，最长 %.1fh' % (POLL, MAX_HOURS))

    while time.time() - t0 < MAX_HOURS * 3600:
        try:
            orders = fetch_orders(20)
            err_n = 0
            fresh = []
            for o in orders:
                ot = int(o.get('orderTime') or 0)
                k = key_of(o)
                if ot > last_ts and k not in seen:
                    fresh.append(o)
            if fresh:
                fresh.sort(key=lambda x: int(x.get('orderTime') or 0))
                log('发现 %d 笔新成交' % len(fresh))
                for i in range(0, len(fresh), 5):
                    push(fresh[i:i + 5])
                    time.sleep(1.2)
                last_ts = max(int(o.get('orderTime') or 0) for o in fresh)
                seen = [key_of(o) for o in fresh[-10:]]
                save_state({'lastTs': last_ts, 'seen': seen})
                git_commit_state('watch')
                last_commit = time.time()
        except Exception as e:
            err_n += 1
            log('轮询失败(%d): %s' % (err_n, str(e)[:100]))
            time.sleep(min(60, 5 * err_n))

        if time.time() - last_commit > 600:
            save_state({'lastTs': last_ts, 'seen': seen})
            last_commit = time.time()
            log('状态已保存 lastTs=%s' % last_ts)
            git_commit_state('state')

        time.sleep(POLL)

    save_state({'lastTs': last_ts, 'seen': seen, 'note': 'exit'})
    git_commit_state('watch exit')
    log('本次长驻结束，lastTs=%s，触发下一棒' % last_ts)
    redispatch_next()


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--test-push':
        try:
            orders = fetch_orders(5)[:2]
            ok = push(orders, title_suffix='（链路验证）')
            print('test push (real data):', ok, 'n=', len(orders))
        except Exception as e:
            print('fetch fail:', e)
            ok = push([{'symbol': 'TESTUSDT', 'baseAsset': 'TEST', 'side': 'SELL',
                        'positionSide': 'SHORT', 'executedQty': 8.011, 'avgPrice': 64377.98,
                        'totalPnl': 0, 'orderTime': int(time.time() * 1000)}])
            print('fallback test push:', ok)
        sys.exit(0)
    main()
