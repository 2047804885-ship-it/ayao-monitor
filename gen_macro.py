# -*- coding: utf-8 -*-
"""生成宏观 JSON 数据（跑在 GitHub Actions），输出 macro.json / fx.json / holdings.json"""
import json
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import macro


def main():
    t0 = time.time()
    m = macro.get_macro()
    print('macro: %d economies, %.1fs' % (m['meta']['count'], time.time() - t0))
    f = macro.get_fx()
    print('fx: %d currencies' % len(f.get('items', [])))
    h = macro.get_holdings()
    print('holdings: %d nodes, %d links' % (len(h.get('nodes', [])), len(h.get('links', []))))
    with open('macro.json', 'w', encoding='utf-8') as fp:
        json.dump(m, fp, ensure_ascii=False)
    with open('fx.json', 'w', encoding='utf-8') as fp:
        json.dump(f, fp, ensure_ascii=False)
    with open('holdings.json', 'w', encoding='utf-8') as fp:
        json.dump(h, fp, ensure_ascii=False)
    print('done, total %.1fs' % (time.time() - t0))


if __name__ == '__main__':
    main()
