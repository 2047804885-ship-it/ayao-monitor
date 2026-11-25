# -*- coding: utf-8 -*-
"""test_push.py — 主线路纯测试推送（不判断信号，直接调 WxPusher）

用途：人工验证 GitHub 渠道（主线路）推送到手机的链路是否通。
与 tmp_push_one.py 的区别：那个要分析币种出信号才推（无信号 exit 1），
这个直接推一条固定测试消息，用于「掉线约等于零」验收。
"""

import json
import os
import urllib.request

WXPUSH_URL = 'https://wxpusher.zjiecode.com/api/send/message'


def main():
    token = os.environ.get('WXPUSHER_TOKEN', '').strip()
    uid = os.environ.get('WXPUSHER_UID', '').strip()
    if not token or not uid:
        print('缺少 WXPUSHER_TOKEN/WXPUSHER_UID，跳过')
        raise SystemExit(1)

    html = ('<div style="font-size:15px;line-height:1.7">'
            '<b>✅ 熬鹰主线路测试推送</b><br>'
            '这条来自 GitHub 主线路。<br>'
            '<span style="color:#666;font-size:12px">'
            '主线路已恢复正常，双线路（GitHub + Vercel）均已验证。</span></div>')

    payload = {
        'appToken': token,
        'content': html,
        'summary': '熬鹰主线路测试',
        'contentType': 2,
        'uids': [uid],
    }
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        WXPUSH_URL, data=data,
        headers={'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=25) as r:
        d = json.loads(r.read().decode('utf-8'))
    code = d.get('code')
    print('推送结果 code=%s msg=%s' % (code, d.get('msg')))
    if code != 1000:
        print('详情:', json.dumps(d.get('data'), ensure_ascii=False)[:200])
        raise SystemExit(1)
    print('OK')


if __name__ == '__main__':
    main()
