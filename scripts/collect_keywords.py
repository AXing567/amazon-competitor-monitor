"""补采流量词：给 pilot 库里的锚点和竞品逐个取真实流量词，落盘缓存。

## 为什么需要这个（两个原因叠在一起，别只记一个）

1. **pilot 自己把词写成了空**。`_snapshot_from_item` 里 `keywords=[]`，
   注释写的是「该账号关键词模块无权益」——那句话当时是**猜的**，而且猜错了。
2. **bridge 长进程的会话陈旧**。跑了很久的 uvicorn 进程里，关键词页面会返回
   一份没有结果集的壳页面，`pick_table` 取不到表，notice 就写「需要有效会员额度」。
   重启进程后同样一个 `dish drying rack` 立刻返回月搜 702,084。

两者的共同外观都是「接口返回 0 条」，但处理方式完全不同：
一个要改代码，一个要重启。所以这个脚本的进度输出里**每次都会打拿到的词数**，
0 就是警号，别等全部跑完才发现 69 个 ASIN 全空。

## 为什么单独一个脚本而不是塞进 pilot_real

采一次约 2~4 秒（bridge 自己的限速），69 个 ASIN 要跑几分钟。
塞进 pilot 会让「重跑判定」也变成几分钟——而判定本身是纯计算。
分开之后：采集可断点续跑，判定随时重跑，额度不会被重复消费。

## 用法

    python scripts/collect_keywords.py                 # 补齐缺的
    python scripts/collect_keywords.py --force         # 全部重采（会重新消耗额度）
    python scripts/collect_keywords.py --limit 5       # 只采 5 个试试（先看是不是真有额度）

产物：`data/pilot_raw/keyword_cache/<ASIN>.json`，格式与 bridge 返回一致。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_PILOT_DB_PATH = ROOT / "data" / "pilot.db"
MAIN_DB_PATH = ROOT / "data" / "competitor_monitor.db"


def _preset_database_url(argv: list[str]) -> Path:
    """把 DATABASE_URL 定到 pilot 库，**必须在 import app 模块之前**。

    为什么不能靠调用方记得 export：`settings` 在 `app.config` 被 import 的那一刻
    就实例化了，之后再改环境变量对它无效。忘了设的后果不是报错，而是
    「安静地读了主库、给主库里的 ASIN 采词」——采到的词写进缓存，pilot 一读
    发现 ASIN 对不上，又因为没有报错而当成「这些竞品没词」。
    """
    for i, arg in enumerate(argv):
        if arg == "--db" and i + 1 < len(argv):
            path = Path(argv[i + 1]).expanduser()
            path = path if path.is_absolute() else (ROOT / path)
            if path.resolve() == MAIN_DB_PATH.resolve():
                raise SystemExit(
                    "拒绝：--db 指向主库（你的真实数据）。这个脚本只给 pilot 用。"
                )
            os.environ["DATABASE_URL"] = f"sqlite:///{path.resolve().as_posix()}"
            return path.resolve()

    existing = os.environ.get("DATABASE_URL", "").strip()
    if existing:
        prefix = "sqlite:///"
        target = Path(existing[len(prefix):]) if existing.startswith(prefix) else Path(existing)
        if target.name == MAIN_DB_PATH.name:
            raise SystemExit(
                "拒绝：DATABASE_URL 指向主库。采集词会写到 data/pilot_raw/keyword_cache，"
                "但目标 ASIN 会读错库。请指向 data/pilot.db，或用 --db 指定。"
            )
        return target

    os.environ["DATABASE_URL"] = f"sqlite:///{DEFAULT_PILOT_DB_PATH.as_posix()}"
    return DEFAULT_PILOT_DB_PATH


TARGET_DB_PATH = _preset_database_url(sys.argv)

from sqlalchemy import select  # noqa: E402

from app.adapters.sellersprite import SellerSpriteAdapter  # noqa: E402
from app.db import get_session                             # noqa: E402
from app.models import Competitor, MyProduct               # noqa: E402

CACHE_DIR = ROOT / "data" / "pilot_raw" / "keyword_cache"

#: 连续这么多个 ASIN 都拿不到词就停。不是怕慢，是怕**一直在错的方向上花额度**：
#: 全空通常意味着 bridge 会话又陈旧了，继续跑只会把额度耗在一个坏通道上。
MAX_CONSECUTIVE_EMPTY = 5


def _cache_path(asin: str) -> Path:
    return CACHE_DIR / f"{asin}.json"


def _load_cached(asin: str) -> list[dict] | None:
    path = _cache_path(asin)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload.get("keywords") or []


def _save_cached(asin: str, keywords: list[dict], total: int | None) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "asin": asin,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "traffic_keywords_total": total,
        "keywords": keywords,
    }
    _cache_path(asin).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _targets() -> list[tuple[str, str]]:
    """[(asin, 说明)]，锚点排在最前——它是判定时 my_core 的来源，缺了整批都没意义。"""
    out: list[tuple[str, str]] = []
    with get_session() as s:
        my = s.execute(select(MyProduct)).scalars().first()
        if my is not None:
            out.append((str(my.asin).upper(), "锚点（自家）"))
        for comp in s.execute(
            select(Competitor).order_by(Competitor.id)
        ).scalars().all():
            out.append((str(comp.asin).upper(), f"竞品 {(comp.brand or '')[:14]}"))
    # 去重但保序
    seen: set[str] = set()
    uniq = []
    for asin, label in out:
        if asin in seen:
            continue
        seen.add(asin)
        uniq.append((asin, label))
    return uniq


async def _run(args: argparse.Namespace) -> int:
    targets = _targets()
    if not targets:
        print("库是空的：先用 scripts/pilot_real.py 造 pilot 数据")
        return 1

    pending = []
    for asin, label in targets:
        if not args.force and _load_cached(asin) is not None:
            continue
        pending.append((asin, label))

    cached_n = len(targets) - len(pending)
    if args.limit:
        pending = pending[: args.limit]

    print(f"目标 {len(targets)} 个 ASIN · 已缓存 {cached_n} / 待采 {len(pending)}")
    if not pending:
        print("都已缓存，无事可做（--force 可全部重采）")
        return 0

    adapter = SellerSpriteAdapter()
    empty_streak = 0
    got_words = 0
    started = time.time()

    for i, (asin, label) in enumerate(pending, 1):
        t0 = time.time()
        try:
            keywords = await adapter.fetch_keywords(asin)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{i}/{len(pending)}] {asin} 异常 {type(exc).__name__}: {str(exc)[:90]}")
            keywords = []

        _save_cached(asin, keywords, None)
        got_words += len(keywords)
        mark = "空" if not keywords else "ok"
        print(
            f"  [{i}/{len(pending)}] {asin:12} {mark:2} {len(keywords):3} 词"
            f"  {time.time() - t0:4.1f}s  {label}"
        )

        if keywords:
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= MAX_CONSECUTIVE_EMPTY:
                print(
                    f"\n连续 {empty_streak} 个 ASIN 拿不到词，停下。"
                    "\n大概率是 bridge 会话又陈旧了（重启 bridge 再跑，已采的会跳过）。"
                )
                break

    elapsed = time.time() - started
    print(f"\n完成：新增 {got_words} 条词，用时 {elapsed:.0f}s")
    print(f"缓存目录：{CACHE_DIR}")
    if got_words == 0:
        print("一条词都没拿到——先别继续，按上面那句去查 bridge。")
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="给 pilot 库补采真实流量词（断点续跑）")
    ap.add_argument("--force", action="store_true", help="忽略缓存全部重采（会重新消耗额度）")
    ap.add_argument("--limit", type=int, default=0, help="只采前 N 个（先验证额度是否真的可用）")
    ap.add_argument("--db", default=None, help="目标库（默认 data/pilot.db，指向主库会被拒绝）")
    args = ap.parse_args()

    print(f"库：{TARGET_DB_PATH}")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
