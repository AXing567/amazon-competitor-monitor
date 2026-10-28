"""离线演示：不连卖家精灵，用假数据把整条链路跑一遍。

用途有两个：
1. 数据源挂掉时（比如账号被风控）也能验证系统逻辑是否正常
2. 第一次拿到系统，想在 30 秒内看懂「它到底会给我什么」

    python scripts/demo_offline.py
    python scripts/demo_offline.py --clean     # 清掉演示数据
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.adapters.unified import UnifiedSnapshot    # noqa: E402
from app.channels.render import render_alert_card, render_daily_card  # noqa: E402
from app.config import settings                     # noqa: E402
from app.db import get_session, init_db             # noqa: E402
from app.domain.diff import DiffEngine              # noqa: E402
from app.models import Competitor, MyProduct, Signal, Snapshot  # noqa: E402
from app.services import briefer, collector, pool    # noqa: E402

DEMO_ASIN = "B0DEMO1234"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="删除演示数据")
    args = ap.parse_args()

    init_db()
    if args.clean:
        _clean()
        return

    _clean()
    _seed_anchor()
    cid = _seed_competitor()
    _seed_snapshots_and_signals(cid)

    print("=" * 66)
    print("场景：S 级竞品昨天净价 29.99 → 今天 26.49（-11.7%），同时断货，评论一天涨 62 条")
    print("      自家：售价 29.99 / 保本价 22.49 / 日均 40 单 / 库存 800（可售 20 天）")
    print("=" * 66)
    print("")

    brief = briefer.build_daily_brief(settings.tenant_id, hours=24)
    print(brief.to_text())
    print("")
    print("-" * 66)
    print("推送卡片（飞书）标题：", render_daily_card(brief)["header"]["title"]["content"])
    print("卡片颜色：", render_daily_card(brief)["header"]["template"],
          "（有 P0 是红色，有 P1 是橙色，安静是蓝色）")
    print("")

    if brief.p0:
        card = render_alert_card(brief.p0[0])
        print("P0 实时预警卡片：", card["header"]["title"]["content"])
        print("")
        for el in card["elements"]:
            for k in ("text", "elements"):
                if k in el:
                    v = el[k]
                    print(v.get("content", "") if isinstance(v, dict) else v)
    print("")
    print("-" * 66)
    print("要清掉演示数据：python scripts/demo_offline.py --clean")


# ============================================================
# 造数据
# ============================================================


def _clean() -> None:
    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(Competitor.asin == DEMO_ASIN)
        ).scalar_one_or_none()
        if comp is None:
            return
        for sig in s.execute(
            select(Signal).where(Signal.competitor_id == comp.id)
        ).scalars():
            s.delete(sig)
        for snap in s.execute(
            select(Snapshot).where(Snapshot.competitor_id == comp.id)
        ).scalars():
            s.delete(snap)
        s.delete(comp)
    print(f"已清理演示竞品 {DEMO_ASIN}")


def _seed_anchor() -> None:
    pool.update_baseline(
        settings.tenant_id,
        asin="B0MINE0001",
        current_price=29.99,
        target_margin_rate=0.25,
        daily_sales_avg=40,
        fba_available_units=800,
        lead_time_days=35,
        sub_category_rank=3200,
        rating=4.6,
        review_count=812,
        title="Demo Toy Car for Demo Purposes",
    )


def _seed_competitor() -> int:
    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id).limit(1)
        ).scalar_one()

        comp = Competitor(
            tenant_id=settings.tenant_id,
            my_product_id=my.id,
            asin=DEMO_ASIN,
            tier="S",
            tier_reasons={
                "price_ratio": 0.998,
                "price_band_ok": True,
                "keyword_overlap": 0.72,
                "keyword_pool_ok": True,
                "node_match": True,
                "functional_match": True,
            },
            price_ratio=0.998,
            keyword_overlap=0.72,
            node_match=True,
            functional_match=True,
            title="Competitor Toy Car (Demo)",
            brand="DemoBrand",
            status="active",
            last_checked_at=datetime.now(),
        )
        s.add(comp)
        s.flush()
        return comp.id


def _seed_snapshots_and_signals(cid: int) -> None:
    now = datetime.now()
    prev_at = now - timedelta(hours=24)

    # 昨天
    prev = Snapshot(
        competitor_id=cid,
        captured_at=prev_at,
        price=29.99,
        net_price=29.99,
        coupon_value=0.0,
        rank_sub=2800,
        monthly_units=900,
        rating=4.5,
        review_count=500,
        in_stock=True,
        stock_hint="IN_STOCK",
        title="Competitor Toy Car (Demo)",
        image_url="https://example.com/a.jpg",
        data_quality="full",
        source="demo",
    )
    # 今天
    curr = Snapshot(
        competitor_id=cid,
        captured_at=now,
        price=29.99,
        net_price=26.49,
        coupon_value=0.0,
        rank_sub=3100,
        monthly_units=960,
        rating=4.5,
        review_count=562,
        in_stock=False,
        stock_hint="OOS",
        title="Competitor Toy Car (Demo)",
        image_url="https://example.com/a.jpg",
        data_quality="full",
        source="demo",
    )
    with get_session() as s:
        s.add(prev)
        s.add(curr)
        s.flush()
        prev_u = collector.row_to_unified(prev)
        curr_u = collector.row_to_unified(curr)

    # 走真实的差异判定逻辑，不造假信号
    oos_confirmed = (prev.stock_hint or "").upper() == "OOS" or prev.in_stock is False
    drafts = DiffEngine("warmup").compare(
        prev_u, curr_u, tier="S", my_price=29.99, oos_confirmed=oos_confirmed
    )

    with get_session() as s:
        for d in drafts:
            s.add(
                Signal(
                    tenant_id=settings.tenant_id,
                    competitor_id=cid,
                    category=d.category,
                    signal_type=d.signal_type,
                    level=d.level,
                    before_value=d.before_value,
                    after_value=d.after_value,
                    change_pct=d.change_pct,
                    description=d.description,
                    detected_at=now,
                )
            )

    print(f"DiffEngine 产出 {len(drafts)} 条信号：")
    for d in drafts:
        print(f"  [{d.level}] {d.signal_type:<18} {d.description}")
    print("")


if __name__ == "__main__":
    main()
