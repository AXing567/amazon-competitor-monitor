"""命令行设置自家锚点。

不打开网页、不连飞书，一条命令把关键数字填进去。
    python scripts/set_baseline.py --asin B0XXXXXXXX --price 29.99 --margin 0.25 --daily 40
    python scripts/set_baseline.py --stock 800 --lead 35
    python scripts/set_baseline.py --show
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings      # noqa: E402
from app.db import init_db           # noqa: E402
from app.services import pool        # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="设置自家产品锚点")
    p.add_argument("--asin")
    p.add_argument("--price", type=float, help="当前售价")
    p.add_argument("--be-price", type=float, help="保本价")
    p.add_argument("--margin", type=float, help="目标毛利率，0~1")
    p.add_argument("--daily", type=float, help="日均销量")
    p.add_argument("--stock", type=int, help="FBA 可售库存")
    p.add_argument("--lead", type=int, help="补货周期（天）")
    p.add_argument("--rank", type=int, help="子类目 BSR")
    p.add_argument("--show", action="store_true", help="只看当前锚点状态")
    args = p.parse_args()

    init_db()

    if args.show:
        info = pool.get_my_product(settings.tenant_id)
        if not info:
            print("还没有自家产品记录，先跑 scripts/init_db.py")
            return
        _print(info)
        return

    fields = {}
    if args.asin:
        fields["asin"] = args.asin.upper()
    if args.price is not None:
        fields["current_price"] = args.price
    if args.be_price is not None:
        fields["break_even_price"] = args.be_price
    if args.margin is not None:
        fields["target_margin_rate"] = args.margin
    if args.daily is not None:
        fields["daily_sales_avg"] = args.daily
    if args.stock is not None:
        fields["fba_available_units"] = args.stock
    if args.lead is not None:
        fields["lead_time_days"] = args.lead
    if args.rank is not None:
        fields["sub_category_rank"] = args.rank

    if not fields:
        p.print_help()
        return

    res = pool.update_baseline(settings.tenant_id, **fields)
    print("已更新：" + "、".join(res.get("updated_fields") or ["无"]))
    print("")
    _print(res)


def _print(info: dict) -> None:
    inv = {"alert": "偏紧", "normal": "正常", "surplus": "充裕", "unknown": "未知"}
    print(f"ASIN           {info.get('asin') or '（未填）'}")
    print(f"当前售价        {info.get('current_price')}")
    print(f"保本价          {info.get('break_even_price')}")
    print(f"目标毛利率      {info.get('target_margin_rate')}")
    print(f"日均销量        {info.get('daily_sales_avg')}")
    print(f"FBA 库存        {info.get('fba_available_units')}")
    print(f"可售天数        {info.get('days_of_supply')}（{inv.get(info.get('inventory_status'), '未知')}）")
    print(f"锚点就绪度      {info.get('readiness')}%（{info.get('grade')} 级）")
    for w in info.get("warnings") or []:
        print(f"  ⚠ {w}")


if __name__ == "__main__":
    main()
