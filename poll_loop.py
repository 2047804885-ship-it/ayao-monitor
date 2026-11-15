# -*- coding: utf-8 -*-
"""Ayao copy-trade poller —— 持续轮询版（GitHub Actions 用）

为什么不用单次 poll.py：
  GitHub 免费版 cron 会被推迟/合并，实测 */5 的定时任务每小时才真正跑一次。
  对策：一次 workflow 触发后，内部循环轮询约 5 分钟（每 20 秒一次），
  并在检测到新交易时立即推送微信。这样即使一小时只触发一次，
  也能保证「有交易就一定被捕获」，且不漏事件（靠 snap.json 累积去重）。

用法：
  python poll_loop.py             # 循环 ~5 分钟后退出（Actions 用）
  python poll_loop.py --once      # 只跑一次（本地调试）
"""
import json, os, sys, time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import poll as P

CST = P.CST
RUN_SECONDS = int(os.environ.get('POLL_RUN_SECONDS', '290'))   # Actions 里跑 ~4分50秒
INTERVAL = int(os.environ.get('POLL_INTERVAL', '20'))          # 20 秒一次


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False)


def once():
    """抓一次、比对一次、有新事件就推送。返回新增事件数。"""
    cfg = load_json(P.PID_FILE, {})
    pid = str(cfg.get('portfolioId') or '5075281354358777856')
    name = cfg.get('name') or 'allincrypto 熬鹰资本'
    snap = load_json(P.SNAP_FILE, {})
    errs = []

    detail = {}
    try:
        raw = P.getJSON(P.EP_DETAIL + pid)
        d = (raw or {}).get('data') or {}
        detail = {'marginBalance': d.get('marginBalance'), 'aumAmount': d.get('aumAmount'),
                  'lastTradeTime': d.get('lastTradeTime'), 'status': d.get('status'),
                  'positionShow': d.get('positionShow'), 'badgeName': d.get('badgeName'),
                  'currentCopyCount': d.get('currentCopyCount'), 'maxCopyCount': d.get('maxCopyCount')}
        if d.get('leadPortfolioId'):
            pid = str(d['leadPortfolioId'])
    except Exception as e:
        errs.append('detail:' + str(e)[:80])

    q = None
    for kw in [name, '熬鹰']:
        if not kw:
            continue
        try:
            q = P.normQList(P.searchQList(kw), pid)
            if q:
                break
        except Exception as e:
            errs.append('qlist:' + str(e)[:80])
    if q:
        pid = q['pid']
        name = q['name'] or name
        chart = q.pop('chartItems')
    else:
        chart = snap.get('curve') or []

    pos = []
    try:
        pos = P.normPositions(P.getJSON(P.EP_POS + pid))
    except Exception as e:
        errs.append('pos:' + str(e)[:80])

    newprof = dict(detail)
    if q:
        newprof.update({k: v for k, v in q.items() if k != 'chartItems' and v is not None})

    old_pos = snap.get('positions')
    evs = []
    first_run = old_pos is None
    if not first_run:
        evs = P.diffPositions(old_pos, pos)
        old_lt = (snap.get('profile') or {}).get('lastTradeTime')
        new_lt = newprof.get('lastTradeTime')
        if isinstance(old_lt, (int, float)) and isinstance(new_lt, (int, float)) and new_lt > old_lt:
            t = datetime.fromtimestamp(new_lt / 1000, CST).strftime('%m-%d %H:%M')
            evs.append({'time': t, 'type': '最新交易', 'symbol': '', 'side': '', 'qty': ''})

    newsnap = {
        'portfolioId': pid, 'name': name,
        'profile': newprof,
        'curve': chart,
        'positions': pos,
        'events': [e for e in (snap.get('events') or []) if e.get('type') not in P.STAT_NOISE] + evs,
        'lastError': ' | '.join(errs)[:200],
        'lastOk': '' if errs else datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
        'serverTime': datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S'),
    }
    newsnap['events'] = newsnap['events'][:200]
    save_json(P.SNAP_FILE, newsnap)
    save_json(P.PID_FILE, {'portfolioId': pid, 'name': name})

    if errs:
        print('  warn:', newsnap['lastError'][:120])
    return len(evs), evs, name, first_run


def main():
    if '--once' in sys.argv:
        n, evs, name, first = once()
        print('once done. new events:', n)
        return

    started = time.time()
    total = 0
    rounds = 0
    print('=== poll_loop start, run %ds every %ds ===' % (RUN_SECONDS, INTERVAL))
    while time.time() - started < RUN_SECONDS:
        try:
            n, evs, name, first = once()
            rounds += 1
            if first:
                print('  baseline recorded (no push)')
            elif evs:
                total += n
                print('  NEW EVENTS:', n, [e.get('type') + (e.get('symbol') or '') for e in evs])
                # 云端也直接推微信（配了 secrets 才生效），让手机在 Actions 跑时就收到
                print('  ', P.wxpush(evs, name))
        except Exception as e:
            print('  round err:', str(e)[:150])
        time.sleep(INTERVAL)
    print('=== poll_loop done: %d rounds, %d new events ===' % (rounds, total))


if __name__ == '__main__':
    main()
