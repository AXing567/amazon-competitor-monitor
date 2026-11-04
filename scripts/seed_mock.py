"""造 / 清 Mock 数据集。

    python scripts/seed_mock.py            # 造一套（先清空）
    python scripts/seed_mock.py --clean    # 只清空
    python scripts/seed_mock.py --show     # 只看现状，不动

## 为什么没有 `--db`

`demo_offline.py` 有 `--db`，因为它要在不同临时库之间切换做验收。
本脚本没有：它写哪个库由 `.env` 的 `MOCK_DATABASE_URL` 决定，
**故意不留命令行口子**。Mock 数据的价值是「可以整体删掉，
删错了也不心疼」，一旦允许把它指到真实库，这个前提就没了；
而 `--db data/competitor_monitor.db` 这种误操作不会有任何报错前兆。

真要换库就改 `.env`——那一步是显式的、可 review 的。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import DATA_MODE_MOCK, ensure_mode_ready, using_data_mode  # noqa: E402
from app.services import mock_data  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description="造 Mock 数据集（写 MOCK_DATABASE_URL 指定的库）",
    )
    ap.add_argument("--clean", action="store_true", help="只清空，不造")
    ap.add_argument("--show", action="store_true", help="只打印现状，不动数据")
    args = ap.parse_args()

    if args.show:
        ensure_mode_ready(DATA_MODE_MOCK)
        with using_data_mode(DATA_MODE_MOCK):
            stats = mock_data.stats_in_current_mode()
        print(f"库文件：{stats['db_file']}")
        print(f"自家 ASIN：{stats['my_asin'] or '（未设置）'}")
        print(f"竞品 {stats['competitors']} 个 · 快照 {stats['snapshots']} 条 · "
              f"信号 {stats['signals']} 条")
        if stats["tiers"]:
            print("档位：" + " · ".join(f"{k}×{v}" for k, v in stats["tiers"].items()))
        print(f"最后采集：{stats['last_capture'] or '—'}")
        return 0

    if args.clean:
        mock_data.clear()
        print("Mock 数据已清空（表结构保留）。")
        return 0

    result = mock_data.build(quiet=False)
    print(f"\n完成。面板上切到「Mock 数据」即可看到这 {result['competitors']} 个竞品。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
