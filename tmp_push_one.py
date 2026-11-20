# -*- coding: utf-8 -*-
"""临时脚本：只分析并推送单个币种（用于人工验证推送格式）

用法：python tmp_push_one.py UNI_USDT
凭证：从环境变量读 WXPUSHER_TOKEN / WXPUSHER_UID
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import poll_market as pm

if __name__ == '__main__':
    sym = sys.argv[1] if len(sys.argv) > 1 else 'UNI_USDT'
    if not sym.endswith('_USDT'):
        sym = sym.upper() + '_USDT'

    print('拉取聪明钱横截面（%d 币）...' % len(pm.MAJORS))
    rows = [pm.stat(s) for s in pm.MAJORS]
    smart = pm.judge_smart(rows)

    print('分析 %s ...' % sym)
    r = pm.analyze_coin(sym, smart)
    if not r:
        print('!! %s 未产出信号（投票未通过或被日线过滤）' % sym)
        sys.exit(1)

    print('信号: %s %s conf=%d votes=%s 1d=%s'
          % (r['coin'], r['side'], r['confidence'], r['votes'], r['trend1d']))
    html = pm.fmt_signal(r)
    print('推送 HTML 长度: %d' % len(html))

    res = pm.wxpush([r])
    print('wxpush: %s' % res)
