"""初始化脚本：建库、建表、建租户。

幂等，可以反复跑。
    python scripts/init_db.py
    python scripts/init_db.py --asin B0XXXXXXXX    # 顺便把自家 ASIN 一起建好
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings           # noqa: E402
from app.db import get_session, init_db    # noqa: E402
from app.models import MyProduct, Tenant   # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化竞品监控数据库")
    parser.add_argument("--asin", default="", help="自家 ASIN（可选）")
    args = parser.parse_args()

    init_db()
    print(f"[1/3] 数据表已就绪：{settings.database_url}")

    with get_session() as s:
        tenant = s.get(Tenant, settings.tenant_id)
        if tenant is None:
            s.add(Tenant(id=settings.tenant_id, name=settings.tenant_name))
            print(f"[2/3] 已创建租户：{settings.tenant_id}（{settings.tenant_name}）")
        else:
            print(f"[2/3] 租户已存在：{settings.tenant_id}")

    with get_session() as s:
        from sqlalchemy import select

        my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id).limit(1)
        ).scalar_one_or_none()
        if my is None:
            s.add(
                MyProduct(
                    tenant_id=settings.tenant_id,
                    asin=args.asin.upper(),
                    marketplace="US",
                )
            )
            print(f"[3/3] 已创建自家产品记录（ASIN={args.asin.upper() or '待填'}）")
        else:
            print(f"[3/3] 自家产品已存在（ASIN={my.asin or '待填'}）")

    print("")
    print("下一步：")
    print("  1. 填 .env（至少要有 SELLERSPRITE_BRIDGE_URL，飞书与 DeepSeek 可选）")
    print("  2. python scripts/set_baseline.py --price 29.99 --margin 0.25 --daily 40")
    print("  3. python run.py     然后浏览器打开 http://127.0.0.1:8100")


if __name__ == "__main__":
    main()
