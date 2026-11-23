# -*- coding: utf-8 -*-
"""风向标备份线（看门狗）

为什么单独一条线：
  主线 flag.yml 每 20 分钟跑一次（7/27/47 分），但 GitHub 的 cron 不保证准时，
  高负载会延迟甚至整次跳过。主线一旦连续几轮没跑，用户那边就是「安静」，
  而安静和「真的没消息」无法区分。

独立性保证（与主线、与熬鹰交易监控都互不干扰）：
  - 独立 workflow：flag_backup.yml
  - 独立 concurrency group：flag-backup（主线是 flag-poll，不排队不互锁）
  - 独立 cron：17/37/57 分，与主线 7/27/47 错开 10 分钟
  - 状态仍走 flag-state 分支（与主线共享去重），但绝不碰 main
  - 主线正常时：只查一次 Actions 接口就退出，不抓数据、不推送、不写状态

判定逻辑：
  查 flag.yml 最近几次运行。
  - 正在跑 / 排队          → 主线活着，退出
  - 最近一次成功且距今 < STALE_MIN 分钟 → 主线活着，退出
  - 否则（失败 / 太久没跑 / 从未跑过）  → 接管，跑完整的 flag_poll

为什么用 Actions 接口而不是状态文件心跳：
  心跳要求主线每轮都 commit 一次状态文件，一天 72 次无意义提交；
  查运行记录是只读的，零成本，而且能区分「跑了但失败」和「压根没跑」。
"""
import datetime
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import flag_poll as fp


def _api(path):
    token = os.environ.get('GH_TOKEN', '').strip()
    repo = os.environ.get('GH_REPO', '').strip()
    if not token or not repo:
        return None
    url = 'https://api.github.com/repos/%s/%s' % (repo, path)
    req = urllib.request.Request(url, headers={
        'Authorization': 'token ' + token,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'flag-watchdog'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print('查询主线状态失败:', str(e)[:100])
        return None


def _age_min(s):
    """GitHub 的 ISO 时间（UTC）距今多少分钟"""
    try:
        t = datetime.datetime.strptime(str(s), '%Y-%m-%dT%H:%M:%SZ')
    except Exception:
        return None
    t = t.replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - t).total_seconds() / 60.0


RUNNING = ('in_progress', 'queued', 'waiting', 'requested', 'pending')


def primary_alive():
    """True=主线活着 False=主线失联 None=查不到"""
    d = _api('actions/workflows/flag.yml/runs?per_page=5')
    if d is None:
        return None
    runs = d.get('workflow_runs') or []
    if not runs:
        print('主线从未运行过')
        return False

    for r in runs[:3]:
        st = r.get('status')
        rid = r.get('id')
        if st in RUNNING:
            print('主线正在运行 %s（%s）' % (rid, st))
            return True
        if st != 'completed':
            continue
        age = _age_min(r.get('updated_at') or r.get('created_at'))
        if r.get('conclusion') == 'success':
            if age is not None and age < fp.STALE_MIN:
                print('主线最近一次成功 %s，%.1f 分钟前' % (rid, age))
                return True
            print('主线最近一次成功 %s，但已是 %.0f 分钟前（阈值 %d）'
                  % (rid, age or -1, fp.STALE_MIN))
            return False
        print('主线最近一次 %s 结论=%s，%.1f 分钟前'
              % (rid, r.get('conclusion'), age if age is not None else -1))
        return False
    return False


def main():
    t0 = time.time()
    print('=== 备份线检查 %s ===' % time.strftime('%Y-%m-%d %H:%M:%S'))

    alive = primary_alive()
    if alive is True:
        print('备份线待命：主线正常，本轮不做任何事 cost=%.1fs' % (time.time() - t0))
        return
    if alive is None:
        # 查不到就接管：宁可重复推（状态去重会兜住）也不能漏
        print('查不到主线状态，保守起见接管')

    print('>>> 主线失联，备份线接管')
    fp.ROLE = 'backup'
    fp.main()
    print('备份线结束 cost=%.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
