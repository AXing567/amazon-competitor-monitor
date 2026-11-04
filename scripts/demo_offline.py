"""离线演示：不连卖家精灵，用假数据把整条链路和**整个面板**跑起来。

## 为什么需要它

1. 数据源挂掉时（账号被风控、额度用完）也能验证系统逻辑是否正常
2. 第一次拿到系统，想在 30 秒内看懂「它到底会给我什么」
3. **验收面板**：没有数据的面板看不出好坏——空表、空曲线、"待判定"不解释，
   这些在空库上全都不明显。所以这里刻意造出**该难看的地方**：
   一个价格不可知的竞品、几条采集失败、一个证据不足的待判定竞品。

## 造出来的数据（每次都一样，可重复演示）

| 竞品 | 分层 | 状态 | 剧情 |
|---|---|---|---|
| B0DEMO0001 | S | 在盯 | 降价 + 断货（P0 双响） |
| B0DEMO0002 | A | 在盯 | 小步降价 8% |
| B0DEMO0003 | B | 在盯 | 涨价 + 星级掉 0.3 |
| B0DEMO0004 | C | 在盯 | 只改标题（P2，进周报不打扰） |
| B0DEMO0005 | **待判定** | 在盯 | 缺流量词与类目路径 → 不判，面板要说明缺什么 |
| B0DEMO0006 | S | 在盯 | **今天净价不可知**（促销接口失败）→ 价格可信度曲线不再是一条直线 |
| B0DEMO0007 | A | **已暂停** | 停止采集、保留历史 |
| B0DEMO0008 | C | **已归档** | 不再盯，但仍能看见并恢复 |

另外：2 个用户 + 3 条平台身份映射（面板右上角切换操作人用）、3 条动作记录、
2 条对标绑定（带 basis 快照）、每个竞品 3 天的 `collect_runs`（含 2 条失败，分属两个竞品）。

## 用法

    python scripts/demo_offline.py                  # 造数 + 打印一份日报，告诉你怎么起服务
    python scripts/demo_offline.py --serve          # 造数 + 直接起服务（Ctrl+C 停）
    python scripts/demo_offline.py --clean          # 清掉演示数据（只删演示的，不动真数据）
    python scripts/demo_offline.py --db data/x.db   # 换一个库（除非你清楚在做什么，否则别用）

## 默认写哪个库：独立演示库

**演示脚本永远不碰主库** `data/competitor_monitor.db`。不传 `--db` 时默认写
`data/demo_offline.db`——这个决定发生在 `_preset_database_url()` 里，位置在
`from app.config import settings` **之前**（`Settings` 在导入时就实例化并读环境变量，
之后再改 `os.environ` 是无效的）。

造数 / 清场之前还有一道 `_guard_target_db()` 守卫，三条判据任一命中就 **`SystemExit`
拒绝运行**：

1. 目标库**就是主库**（路径或文件名等于 `competitor_monitor.db`）——只看路径，不看内容；
2. 库里有**不属于演示的自家锚点**（ASIN 不是 `B0MINE0001`）；
3. 库里有**不属于演示前缀**的竞品（不以 `B0DEMO` 开头）。

第 1 条是实测补的：主库里那条自家锚点的 ASIN 恰好就是 `B0MINE0001`、唯一竞品恰好叫
`B0DEMO1234`（历史演示残留），光看内容它和演示库无法区分，2/3 条对它无效。

为什么是"拒绝"而不是"警告一下"：D3 的真实事故就是演示在三个月真实数据的库上
跑了一次（竞品 1→8、快照 2→71、真实锚点被改成演示值），最后靠备份还原。
"允许在真库上跑"本身就是缺陷，警告会被 `--quiet`、会被滚屏、会被"我就试一下"忽略。

## 为什么按标记清数据

`--clean` 只删 ASIN 以 `B0DEMO` 开头的竞品及其快照 / 信号 / 采集日志 / 对标绑定，
外加 `reason` 以 `[演示]` 开头的动作、本次新增的演示用户、`demo_*` 身份映射、
以及 `updated_by='演示数据'` 的阈值覆盖与版本。
不用「清空整库」：`--clean` 也会在（被守卫放行的）演示库上跑，
清空整库意味着"重跑一次演示"等于"删掉整段演示历史"，
更意味着某天守卫被绕过时它会把真实数据一起清掉——破坏范围不该由"有没有 bug"决定。
清完会回头数一遍并在有残留时**直接报错**——默默留着残留，下次演示就会叠出重复数据。

## 演示数据怎么认领（D3 / D4 之后）

五条"只有演示自己才认得出的东西"把演示数据和真实数据隔开：

| 东西 | 认领方式 | 为什么不能靠"第一条" |
|---|---|---|
| 竞品 | ASIN 前缀 `B0DEMO` | — |
| 自家锚点 | ASIN `B0MINE0001`（独立一条，`MyProduct.asin` 精确匹配） | 真实库里"第一条"是用户的真锚点 |
| 动作 | `reason` 以 `[演示] ` 开头 | 单产品租户里"第一条锚点"就是用户自己 |
| 阈值 | `updated_by='演示数据'` | 公司层/产品层都可能已有真实覆盖 |
| 用户 / 身份 | 用户名「小李」+ 身份 `demo_` 前缀 | — |

动作的 `[演示] ` 前缀是**故意让人看见**的：`reason` 是面板「动作记录」里直接展示
给人看的一栏，演示数据就该一眼看出是演示。

删除与残留核对用的是**同一个谓词**（`reason LIKE '[演示]%'`）。
D4 的教训：原实现按 `my_product_id` 删，而单产品租户里那条 id 就是用户自己的锚点，
于是真实动作被一起删光；更糟的是核对也用同一个口径，删光时恰好显示 "actions 0"——
**删得越多看起来越干净**。现在删和核对都只认前缀，删多了或删不掉都会在残留里露出来。

剩余的唯一前提：**别在真库上跑**。守卫会保证这一点，所以想演示就放心用默认库。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ============================================================
# 数据库落点：**必须在** `from app.config import settings` 之前决定
#
# `app/config.py` 末尾是 `settings = get_settings()`，也就是 `Settings` 在**导入时**
# 就实例化并读完环境变量。之后再改 `os.environ` 对 `settings.database_url` 无效，
# 引擎（`app/db.py` 的 `engine`）也早就绑在旧 URL 上了。
#
# 为什么必须换库（缺陷 D3）：默认 URL 指向 `data/competitor_monitor.db`，那是用户
# 真实的库。演示脚本在它上面跑过一次的后果是竞品 1→8、快照 2→71、真实锚点被覆盖，
# 最后靠备份还原。所以默认值改为独立演示库，想换库只能显式 `--db`。
# ============================================================

#: 主库（真实数据）。演示脚本**永远**不写它。
MAIN_DB_PATH = ROOT / "data" / "competitor_monitor.db"

#: 演示默认库。和主库并列放在 `data/` 下，方便 `.gitignore` 与备份策略一起处理。
DEFAULT_DEMO_DB_PATH = ROOT / "data" / "demo_offline.db"


def _argv_db_path(argv: list[str]) -> Optional[Path]:
    """从 argv 里取 `--db` 的值，支持 `--db PATH` 与 `--db=PATH` 两种写法。

    为什么不用 argparse 取：argparse 的解析发生在 `main()` 里，那已经在
    `from app.config import settings` 之后，改环境变量为时已晚。

    Args:
        argv: 通常是 `sys.argv`。

    Returns:
        `--db` 给出的路径；没给或给了空值则返回 None。
    """
    for i, item in enumerate(argv):
        if item == "--db":
            return Path(argv[i + 1]).expanduser() if i + 1 < len(argv) else None
        if item.startswith("--db="):
            value = item.split("=", 1)[1]
            return Path(value).expanduser() if value else None
    return None


def _path_of_sqlite_url(url: str) -> Path:
    """`sqlite:///<path>` → `Path`。非 SQLite 的 URL 原样包成 `Path`（只用于打印）。"""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        return Path(url[len(prefix):])
    return Path(url)


def _preset_database_url(argv: list[str]) -> Path:
    """决定这次运行打哪个库，写进 `DATABASE_URL`，返回该库路径。

    优先级：`--db PATH` > 环境里**已经存在**的 `DATABASE_URL` > `data/demo_offline.db`。

    为什么保留"环境里已有的 `DATABASE_URL`"：测试 / CI 需要一个临时库
    （`DATABASE_URL=sqlite:///<tmp>/x.db pytest tests/`），去掉这条路就只能改代码。
    它不构成"在真库上跑"的口子——真库会在 `_guard_target_db()` 被拒。

    Args:
        argv: 通常是 `sys.argv`，用来找 `--db`。

    Returns:
        实际使用的库文件路径（绝对路径）。
    """
    explicit = _argv_db_path(argv)
    if explicit is not None:
        path = explicit if explicit.is_absolute() else (Path.cwd() / explicit)
        resolved = path.resolve()
        os.environ["DATABASE_URL"] = f"sqlite:///{resolved.as_posix()}"
        return resolved

    existing = os.environ.get("DATABASE_URL", "").strip()
    if existing:
        return _path_of_sqlite_url(existing)

    os.environ["DATABASE_URL"] = f"sqlite:///{DEFAULT_DEMO_DB_PATH.as_posix()}"
    return DEFAULT_DEMO_DB_PATH


#: 本次运行预置的库路径。**只用于打印与自检**：真正起作用的是 `DATABASE_URL`。
TARGET_DB_PATH = _preset_database_url(sys.argv)


from sqlalchemy import delete, select  # noqa: E402

from app.config import settings                              # noqa: E402
from app.db import get_session, init_db                      # noqa: E402
from app.domain.diff import DiffEngine                        # noqa: E402
from app.models import (                                      # noqa: E402
    Action,
    CollectRun,
    Competitor,
    CompetitorBinding,
    MyProduct,
    Signal,
    Snapshot,
    ThresholdConfig,
    ThresholdVersion,
    User,
    UserIdentity,
)
from app.services import action_log, briefer, binding, collector, identity  # noqa: E402

DEMO_ASIN_PREFIX = "B0DEMO"
DEMO_MY_ASIN = "B0MINE0001"

#: 演示数据统一用这个操作人名字写阈值版本，`--clean` 靠它认自己的行。
#: 为什么不按 scope/scope_id 删：真实环境里公司层可能本来就有覆盖值，
#: 按作用域删会连用户自己配的那条一起删掉。
DEMO_OPERATOR = "演示数据"

#: 演示用的第二个用户。面板右上角的身份切换需要至少两个人才看得出效果，
#: 而且动作记录里「谁记的」才有对比。
DEMO_MEMBER_NAME = "小李"

#: 演示身份的 external_id 前缀。`--clean` 按它认身份行。
#: 用前缀而不是 user_id：默认用户（老板）是 init_db 建的，不属于演示数据，
#: 只有挂在它名下的**演示身份**才该被清掉。
DEMO_IDENTITY_PREFIX = "demo_"

#: 自家锚点。三个数（售价 / 保本价 / 日均销量）缺一个，跟价判断就返回"判断不了"。
DEMO_MY = {
    "asin": DEMO_MY_ASIN,
    "current_price": 29.99,
    "break_even_price": 22.49,
    "target_margin_rate": 0.25,
    "daily_sales_avg": 40,
    "fba_available_units": 800,
    "lead_time_days": 35,
    "sub_category_rank": 3200,
    "rating": 4.6,
    "review_count": 812,
    "title": "Demo Toy Car (Our Listing)",
    # 分层判断的「节点」维度靠它。演示里必须填一次，
    # 否则所有竞品的 node_match 都是 None，面板上又会变成一整列"待判定"。
    "category_path": ["Toys & Games", "Toy Vehicles", "Play Trucks"],
}

#: 演示竞品的流量词。真实数据来自卖家精灵的 traffic-keywords（5 个接口之一），
#: 这里用固定值代替，但**形状和真实返回一致**——演示要是用了简化形状，
#: 它验证的就不是生产链路了。
#: 每个层级分到哪几个词。**不是装饰**：`judge_tier` 的加权重合度 =
#: 自家核心词里被竞品也覆盖的那些词的权重占比，所以竞品覆盖 3 个词的会算 S，
#: 覆盖 2 个的算 A，只覆盖 1 个的算 B，一个都不覆盖的算 C。
#: 如果所有竞品共用同一份词表，重算一遍会得到 8 个一模一样的层级——
#: 面板上「S 级对手」和「C 级」就变成同一个东西，演示反而证明了系统没用。
_DEMO_KW_BY_TIER: dict[str, list[str]] = {
    "S": ["toy car", "diecast car", "kids toy car"],
    "A": ["toy car", "diecast car"],
    "B": ["toy car"],
    # C 级刻意是**完全不相干**的词：重合度算出来是 0.0 而不是 None。
    # 这个区别很重要——0.0 是「判过了，不重合」，None 是「判不了」。
    "C": ["puzzle", "board game"],
    "unknown": [],
}

#: 体量：S/A 要和自家的日均销量咬合（0.8x~2.5x），否则算法会把它降成 A。
_DEMO_UNITS_BASE: dict[str, int] = {"S": 1250, "A": 1150, "B": 1000, "C": 600}


def _demo_keywords(scen: dict) -> Optional[list[dict]]:
    """按层级给一套词。`unknown` 返回 None（这条演示的就是"没数据"）。"""
    tier = str(scen.get("tier") or "unknown").upper()
    picked = _DEMO_KW_BY_TIER.get(tier)
    if not picked:
        return None
    share = 0.34
    out: list[dict] = []
    for i, word in enumerate(picked):
        out.append(
            {
                "keyword": word,
                "traffic_share": round(share / (i + 1), 4),
                "search_volume": 120000 // (i + 1),
                "organic_rank": 8 + i * 5,
                "sponsored_rank": None,
            }
        )
    return out

DEMO_COMP_PATH = ["Toys & Games", "Toy Vehicles", "Play Trucks"]


# ============================================================
# 场景定义
#
# `*_changes` 的 key 是「几天前」，值是「从那天起变成这个值」。
# 所以 {3: 26.99} = 三天前开始是 26.99，今天还是 26.99。
# 用「几天前」而不是「第几天」，是因为写的人脑子里想的是"昨天降的价"。
# ============================================================

SCENARIOS: list[dict[str, Any]] = [
    {
        "asin": "B0DEMO0001",
        "title": "Demo Toy Car · S 级对手",
        "brand": "DemoBrand",
        "tier": "S",
        "days": 10,
        "base_price": 29.99,
        "price_changes": {2: 26.49},
        "rank_changes": {2: 3100},
        "review_changes": {2: 562},
        "stock_days": {0},          # 今天断货
    },
    {
        "asin": "B0DEMO0002",
        "title": "Demo Toy Car · A 级对手",
        "brand": "SecondBrand",
        "tier": "A",
        "days": 12,
        "base_price": 19.99,
        "price_changes": {1: 18.39},   # -8%，P0 但没断货
        "rank_changes": {1: 1900},
    },
    {
        "asin": "B0DEMO0003",
        "title": "Demo Toy Car · B 级对手",
        "brand": "ThirdBrand",
        "tier": "B",
        "days": 9,
        # 价格必须落在自家价格带内（0.7x~1.4x，即 21.0~42.0），否则
        # `price_ok` 为假，四同法则会把它判成 C——而这条演示的是 B 级。
        # 15.99 只有 0.53x，属于另一个价格带的对手，本来就不该进这个池子。
        "base_price": 21.99,
        "price_changes": {3: 23.99},   # 涨价 +9.1% = 我的提价窗口
        "rating_changes": {3: 4.2},
        # 昨天详情页 503：健康页要有**两条不同竞品**的失败，
        # 只有一条时看不出「失败是按竞品分散的，还是一直卡在同一个 ASIN 上」。
        "fail_collect_days": {1: "详情页 503 Service Unavailable（连续两次超时后放弃）"},
    },
    {
        "asin": "B0DEMO0004",
        "title": "Demo Toy Car · C 级对手",
        "brand": "FourthBrand",
        "tier": "C",
        "days": 8,
        "base_price": 12.49,
        "rank_changes": {1: 5400},
        # 只改标题 → P2：进周报，不单独打扰。用来验证"P2 不推"这条规则
        "title_suffix_at": 1,
    },
    {
        "asin": "B0DEMO0005",
        "title": "Demo Toy Car · 待判定（缺证据）",
        "brand": "MysteryBrand",
        "tier": "unknown",
        # 这条演示的是「缺证据 → 待判定，且不硬判成 C」。
        # 缺的正是流量词与类目路径这两维——所以下面写快照时**刻意不给**。
        "missing_dims": True,
        "days": 7,
        "base_price": 24.99,
        "price_changes": {2: 23.99},
        # 缺流量词与类目路径 → 可判定维度 < 2 → 不判级（拿不到证据 ≠ 证据表明不成立）
        "tier_reasons": {
            "price_ratio": 0.834,
            "price_band_ok": None,
            "keyword_overlap": None,
            "keyword_pool_ok": None,
        },
        "keyword_overlap": None,
        "node_match": None,
        "functional_match": True,
    },
    {
        "asin": "B0DEMO0006",
        "title": "Demo Toy Car · 价格不可知",
        "brand": "SixthBrand",
        "tier": "S",
        "days": 11,
        "base_price": 33.99,
        # 3 天前一次 -0.9% 的微调：单独看只是 P2，
        # 但它是**试算必须能看出差别**的那条样本——
        # 把 price_p0_pct 收到 0.001 时它会从 P2 变 P0，P0 计数才严格变大；
        # 全是 8%~12% 的大幅变动时，收紧/放松门槛都看不出变化。
        "price_changes": {3: 33.69},
        # 今天促销接口失败：net_price 为 NULL（价格不可知，不是 0）
        "net_none_days": {0},
        "fail_collect_days": {0: "促销接口 429 Too Many Requests（额度用尽）"},
    },
    {
        "asin": "B0DEMO0007",
        "title": "Demo Toy Car · 已暂停",
        "brand": "SeventhBrand",
        "tier": "A",
        "days": 7,
        "base_price": 21.99,
        "status": "paused",
    },
    {
        "asin": "B0DEMO0008",
        "title": "Demo Toy Car · 已归档",
        "brand": "EighthBrand",
        "tier": "C",
        "days": 7,
        "base_price": 9.99,
        "status": "archived",
    },
]

#: 演示动作的认领标记，写在 `Action.reason` 的最前面。
#:
#: 为什么用 `reason` 而不是加一个新列 / 新表：`reason` 是面板「动作记录」里
#: 直接展示给人看的一栏，加上 `[演示] ` 之后**一眼就能看出这是演示数据**——
#: 这是故意的，不是占位符。
#: 顺带它成了 `--clean` 唯一可靠的认领凭据：D4 的根因就是按 `my_product_id` 删，
#: 而单产品租户里那条 id 恰好是用户自己的锚点。
#:
#: 注意 `[演示]` 里没有 `%` 和 `_`，所以 `LIKE '[演示]%'` 是安全的前缀匹配
#: （SQLite 的 LIKE 只对 `%` / `_` 特殊）。改这个常量时**别加这两个字符**。
DEMO_REASON_PREFIX = "[演示] "

#: 造出来的 3 条动作。故意混两种来源：面板手补的 + 飞书卡片点的。
#: 第三条刻意把验证日期设成「已经到期」，让面板能演示「已到期未复盘」那个提醒。
#: `reason` 在写入时统一加上 `DEMO_REASON_PREFIX`（见 `_seed_actions()`），
#: 所以这里只写人话本身，不要自己再拼前缀。
DEMO_ACTIONS: list[dict[str, Any]] = [
    {
        "asin": "B0DEMO0001",
        "action_type": "price_hold",
        "reason": "他降到 26.49 但同时断货——没货卖的人标多低都成交不了，跟价只是白丢利润",
        "description": "不跟价；改抢他详情页广告位",
        "source": "feishu_card",
        "days_ago": 1,
        "verify_days": 7,
    },
    {
        "asin": "B0DEMO0002",
        "action_type": "price_follow",
        "reason": "他只降 8% 且没断货，跟到 28.99 保住转化，仍在保本价之上",
        "description": "售价 29.99 → 28.99",
        "source": "feishu_card",
        "days_ago": 0,
        "verify_days": 7,
    },
    {
        "asin": None,
        "action_type": "ad_shift",
        "reason": "两个对手同时进入价格战，先不加价跟，把预算挪到防守词",
        "description": "把 20% 预算从大词挪到长尾防守词（手工补记）",
        "source": "panel",
        "days_ago": 4,
        "verify_days": 2,   # 4 天前记的 + 2 天 → 两天前就到期了，面板会标「已到期未复盘」
    },
]


def main() -> None:
    ap = argparse.ArgumentParser(
        description="离线演示：造假数据 + 起服务",
        epilog="演示默认使用独立库 data/demo_offline.db，不会碰主库 "
               "data/competitor_monitor.db。",
    )
    ap.add_argument("--clean", action="store_true",
                    help="删除演示数据（只删 B0DEMO* 竞品、[演示] 动作与演示锚点）")
    ap.add_argument("--serve", action="store_true", help="造完数据后直接起服务")
    ap.add_argument("--port", type=int, default=8000, help="起服务时的端口（默认 8000）")
    ap.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="演示数据写到哪个库（默认 data/demo_offline.db）。"
             "演示默认使用独立库，不会碰主库 data/competitor_monitor.db；"
             "这个参数只在你确实想换一个空库时用。",
    )
    ap.add_argument("--quiet", action="store_true", help="少打印一些")
    args = ap.parse_args()

    # 只看路径的第一道闸：**必须在 `init_db()` 之前**。
    # `init_db()` 会 create_all + migrate，在真库上跑一次就可能加列 / 加索引/
    # 重建表——那已经算"动了主库"，把守卫放在它后面就太晚了。
    _assert_not_main_db()
    init_db()
    # 造数之前再确认这个库能碰：内容脏（有真实锚点 / 真实竞品）直接 SystemExit。
    # 放在 `_clean()` 之前是有意的——`_clean()` 也会删数据，同样只能在演示库上跑。
    _guard_target_db()

    if args.clean:
        _clean()
        return

    _clean()
    my_id = _seed_anchor()
    uid = identity.ensure_default_user(settings.tenant_id)["id"]
    _seed_users(uid)

    comps = _seed_competitors(my_id)
    for scen in SCENARIOS:
        _seed_history(scen, comps[scen["asin"]]["id"])

    _seed_bindings(comps, my_id, uid)
    _seed_collect_runs(comps, my_id)
    _seed_actions(uid, my_id)
    _seed_thresholds(my_id, uid)

    if not args.quiet:
        _report(comps)

    if args.serve:
        _serve(args.port, TARGET_DB_PATH)


# ============================================================
# 运行前守卫：这个库能不能被演示写
# ============================================================


def _target_db_display() -> str:
    """把当前生效的库地址显示成人看得懂的（尽量相对项目根）。"""
    path = _path_of_sqlite_url(str(settings.database_url))
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _assert_not_main_db() -> None:
    """只看路径：目标是不是主库。是就 `SystemExit`。

    **必须在 `init_db()` 之前调用**：`init_db()` 会 `create_all` + `migrate`，
    在主库上跑一次就可能加列 / 加索引 / 重建表——那已经算"动了主库"，
    把这道闸放在它后面就太晚了。

    只看路径不看内容，是因为实测主库的内容本身就"长得像演示库"
    （自家锚点 ASIN 恰好 `B0MINE0001`、唯一竞品恰好 `B0DEMO1234`，历史演示残留），
    内容判据对它完全无效。

    Raises:
        SystemExit: 目标是主库。
    """
    target = _path_of_sqlite_url(str(settings.database_url))
    # 比一次文件名，防止"拷一份到自己目录也叫 competitor_monitor.db"这种绕过。
    if target.name == MAIN_DB_PATH.name or target.resolve() == MAIN_DB_PATH.resolve():
        raise SystemExit(
            f"目标库 {_target_db_display()} 就是主库（{MAIN_DB_PATH.name}）："
            "演示脚本不写它。请用 --db 指定一个独立的空库"
            "（例如 --db data/demo_offline.db），或者干脆不传 --db —— 默认就是独立演示库。"
        )


def _guard_target_db() -> None:
    """造数 / 清场之前校验目标库，脏了就**拒绝运行**（`raise SystemExit`）。

    为什么是拒绝而不是警告：D3 的真实事故是演示在三个月真实数据的库上跑了一次
    —— 竞品 1→8、快照 2→71、真实锚点被改成 `B0MINE0001`，
    当事人直到某天对账才发现，最后靠备份还原。
    警告会被 `--quiet` 吞掉、会被滚屏带走、会被"我就试一下"忽略；拒绝不会。

    三条判据，任一命中就退出：

    1. 目标库**就是主库**（见 `_assert_not_main_db()`，只看路径）；
    2. 租户下存在 `asin != DEMO_MY_ASIN` 的自家锚点 → 那是你自己的产品；
    3. 存在不以 `DEMO_ASIN_PREFIX` 开头的竞品 → 那是真实竞品。

    判据 2/3 **不看数量**：一条真实数据就足够危险。

    同时无条件打印实际使用的库路径：每次运行都能一眼看到"我在打哪个库"。

    Raises:
        SystemExit: 目标是主库，或库里已经有用户的真实数据。
    """
    display = _target_db_display()
    print(f"演示目标库：{display}")
    # 再打一行**解析后的绝对路径**（realpath）：事后对账时唯一能作数的就是它。
    # 相对路径会被 cwd 影响，日志里只留 `data/x.db` 的话，"到底写没写主库"没人能复核。
    url = str(settings.database_url)
    if url.startswith("sqlite:"):
        realpath = _path_of_sqlite_url(url).resolve().as_posix()
        if realpath != display:
            print(f"          绝对路径：{realpath}")
    _assert_not_main_db()

    with get_session() as s:
        anchors = list(
            s.execute(
                select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id)
            ).scalars()
        )
        real_anchors = [m for m in anchors if m.asin != DEMO_MY_ASIN]
        if real_anchors:
            first = real_anchors[0]
            keywords = first.core_keywords or {}
            raise SystemExit(
                f"目标库 {display} 里已有你自己的自家锚点"
                f"（ASIN {first.asin}，含 {len(keywords)} 个关键词）。"
                "演示脚本不覆盖它——请用 --db 指定一个独立库"
                "（例如 --db data/demo_offline.db）。"
            )

        real_asins = list(
            s.execute(
                select(Competitor.asin).where(
                    Competitor.tenant_id == settings.tenant_id,
                    Competitor.asin.not_like(f"{DEMO_ASIN_PREFIX}%"),
                )
            ).scalars()
        )
        if real_asins:
            sample = "、".join(real_asins[:3])
            raise SystemExit(
                f"目标库 {display} 里有真实竞品 {len(real_asins)} 个"
                f"（例如 {sample}），这不是演示库。"
                "演示脚本不在含真实竞品的库上造数或清场——"
                "请用 --db 指定一个独立库（例如 --db data/demo_offline.db）。"
            )


# ============================================================
# 清场
# ============================================================


def _clean(verify: bool = True) -> None:
    """只删演示数据。按 ASIN 前缀 / `[演示]` 动作前缀 / 演示锚点 / 操作人名字认自己的东西。

    ## 动作记录为什么按 `reason` 前缀删，**不按** `my_product_id` 删

    这是 QA 打回来的 D4。旧版 docstring 曾写着"按 my_product_id 删，这样'没关竞品'
    那条也能被清掉"——那句话描述的正是危险行为，已经删掉：

    `my_product_id` 在单产品租户里**就是用户自己的那条锚点**，用户的真实动作全都挂在
    它上面。按它删等于「跑一次演示 = 清空真实动作记录」，而 `_verify_clean()` 也用
    同一个口径数残留，于是删光时恰好打印 `actions 0`——**删得越多看起来越干净**。

    现在只认 `reason LIKE '[演示]%'`：这个前缀是 `_seed_actions()` 写进去的
    （见 `DEMO_REASON_PREFIX`），真实动作天然没有它。
    删除条件里刻意**不加** `my_product_id`：删除与残留核对必须是同一个谓词，
    否则又会退化成"删了但在另一处数不出来"。
    `[演示]` 不含 `%` / `_`，所以这是安全的前缀匹配，不需要 `escape` 参数。

    ## 为什么锚点删除要留守卫

    只有「演示锚点没被任何竞品挂着」时才删。真有人手工把一个真实竞品绑到演示锚点上
    （虽然 UI 里很难做到），删锚点会让那行竞品的 `my_product_id` 指向不存在的行——
    SQLite 默认不强制外键，这种孤儿数据不会报错，只会在某次 join 里静静消失。

    另外：这个函数**只应在演示库上跑**。真实锚点根本进不来，因为
    `_guard_target_db()` 在 `main()` 里先一步拒绝了含真实数据的库。

    `verify=True` 时清理完会**回头数一遍**并断言归零。
    为什么要断言而不是只打印：`--clean` 是唯一会删数据的入口，
    而"删了但没删干净"这种情况不报错——它只会在下次演示时表现为
    "怎么多了一个竞品 / 版本号从 v5 开始"，那时已经很难追是哪一步漏了。
    """
    with get_session() as s:
        ids = [
            row
            for row in s.execute(
                select(Competitor.id).where(Competitor.asin.like(f"{DEMO_ASIN_PREFIX}%"))
            ).scalars()
        ]
        # 只认 asin == B0MINE0001 的那一条。**不要**用 `.limit(1)`：
        # 真实库里第一条永远是用户的真锚点，那正是 D3/D4 的根因。
        demo_my = s.execute(
            select(MyProduct).where(
                MyProduct.tenant_id == settings.tenant_id,
                MyProduct.asin == DEMO_MY_ASIN,
            ).limit(1)
        ).scalar_one_or_none()

        # 动作按 `[演示]` 前缀认领，不按 my_product_id（见 docstring 第二节）。
        s.execute(
            delete(Action).where(
                Action.tenant_id == settings.tenant_id,
                Action.reason.like(f"{DEMO_REASON_PREFIX}%"),
            )
        )
        if ids:
            s.execute(delete(CollectRun).where(CollectRun.competitor_id.in_(ids)))
            s.execute(delete(Signal).where(Signal.competitor_id.in_(ids)))
            s.execute(delete(Snapshot).where(Snapshot.competitor_id.in_(ids)))
            s.execute(
                delete(CompetitorBinding).where(CompetitorBinding.competitor_id.in_(ids))
            )
            s.execute(delete(Competitor).where(Competitor.id.in_(ids)))

        # 演示身份 / 演示用户：身份按 external_id 前缀删，用户按名字删。
        # 顺序不能反：先删身份再删用户，否则外键指向一个不存在的 user_id
        # （测试库开了外键约束时这一步会直接报错，而报错信息不会告诉你顺序错了）。
        s.execute(
            delete(UserIdentity).where(
                UserIdentity.external_id.like(f"{DEMO_IDENTITY_PREFIX}%")
            )
        )
        s.execute(
            delete(User).where(
                User.tenant_id == settings.tenant_id,
                User.name == DEMO_MEMBER_NAME,
                User.role == "member",
            )
        )

        # 演示阈值：按操作人删，且**两个作用域分别删**（公司层 / 产品层各一条）。
        # 不按 scope_id 删的理由见 DEMO_OPERATOR 的注释。
        s.execute(
            delete(ThresholdConfig).where(
                ThresholdConfig.tenant_id == settings.tenant_id,
                ThresholdConfig.updated_by == DEMO_OPERATOR,
            )
        )
        s.execute(
            delete(ThresholdVersion).where(
                ThresholdVersion.tenant_id == settings.tenant_id,
                ThresholdVersion.operator_id == DEMO_OPERATOR,
            )
        )

        # 演示锚点：只有**没有任何行还挂着它**时才删。
        #
        # 三种引用都要查，不只查竞品：`actions.my_product_id` 与
        # `competitor_bindings.my_product_id` 也是指向 `my_products.id` 的外键，
        # 而 `app/db.py` 把 `PRAGMA foreign_keys=ON` 打开了 —— 漏查一个就是
        # `IntegrityError: FOREIGN KEY constraint failed`（实测踩到：
        # 面板上记过一条真实动作之后，`--clean` 会在删锚点这一步炸掉）。
        #
        # 留一条演示锚点在库里是无害的：`_demo_residuals()` 不统计 my_products，
        # 下次造数也会按 ASIN 复用它。反过来"硬删"会制造孤儿 row 或直接报错。
        #
        # **这是刻意的取舍，别"顺手清理干净"**：宁可留下一个演示锚点，
        # 也不删一条真实动作。留下的锚点下次演示会复用，删掉的动作找不回来。
        #
        # 为什么不当成"残留"报错：锚点是**故意**留的，把它塞进 `_demo_residuals()`
        # 会让"库里有一条真实动作"这种正常情况变成 `--clean` 失败。
        # 折中办法是把"留了几个、为什么留"直接打印出来——看得见，但不拦路。
        anchor_note = ""
        if demo_my is not None:
            refs = {
                "竞品": s.execute(
                    select(Competitor.id).where(Competitor.my_product_id == demo_my.id)
                ).scalars().all(),
                "动作": s.execute(
                    select(Action.id).where(Action.my_product_id == demo_my.id)
                ).scalars().all(),
                "对标绑定": s.execute(
                    select(CompetitorBinding.id).where(
                        CompetitorBinding.my_product_id == demo_my.id
                    )
                ).scalars().all(),
            }
            if any(refs.values()):
                parts = "、".join(f"{name} {len(rows)} 条" for name, rows in refs.items() if rows)
                anchor_note = (
                    f"演示锚点 {DEMO_MY_ASIN} 保留（仍被 {parts} 引用；"
                    "不硬删，以免连带删掉真实动作）"
                )
            else:
                s.execute(delete(MyProduct).where(MyProduct.id == demo_my.id))
                anchor_note = f"演示锚点 {DEMO_MY_ASIN} 已删除（无任何引用）"

    print(f"已清理演示数据（{DEMO_ASIN_PREFIX}*，共 {len(ids)} 个竞品）")
    if anchor_note:
        print(anchor_note)
    if verify:
        _verify_clean()


def _demo_residuals() -> dict[str, int]:
    """清理后仍属于演示数据的行数。全部应为 0。

    注意 `actions` 的判定口径：**`reason` 以 `[演示]` 开头的动作**，
    不是"演示锚点名下的动作"，更不是"第一个 my_product 名下的"。

    为什么纠这一条（D4 的下半段）：旧口径和删除口径都是 `my_product_id == 第一个锚点`，
    于是单产品租户里"把用户的真实动作删光"之后，残留数恰好是 0，
    `_verify_clean()` 就打印 `actions 0` —— **删得越多看起来越干净**。
    核对口径必须独立于"哪一条锚点"，否则它只是在确认自己的删除动作执行了。
    """
    with get_session() as s:
        ids = list(
            s.execute(
                select(Competitor.id).where(Competitor.asin.like(f"{DEMO_ASIN_PREFIX}%"))
            ).scalars()
        )

        def count(stmt) -> int:  # noqa: ANN001 —— 内部小工具，不需要完整签名
            return len(list(s.execute(stmt).scalars()))

        return {
            "users": count(
                select(User.id).where(
                    User.tenant_id == settings.tenant_id,
                    User.name == DEMO_MEMBER_NAME,
                )
            ),
            "identities": count(
                select(UserIdentity.id).where(
                    UserIdentity.external_id.like(f"{DEMO_IDENTITY_PREFIX}%")
                )
            ),
            "bindings": count(select(CompetitorBinding.id)) if ids else 0,
            "threshold_configs": count(
                select(ThresholdConfig.id).where(
                    ThresholdConfig.updated_by == DEMO_OPERATOR
                )
            ),
            "threshold_versions": count(
                select(ThresholdVersion.id).where(
                    ThresholdVersion.operator_id == DEMO_OPERATOR
                )
            ),
            "collect_runs": count(select(CollectRun.id).where(CollectRun.competitor_id.in_(ids)))
            if ids
            else 0,
            "actions": count(
                select(Action.id).where(
                    Action.tenant_id == settings.tenant_id,
                    Action.reason.like(f"{DEMO_REASON_PREFIX}%"),
                )
            ),
        }


def _verify_clean() -> None:
    """清理后自检：残留必须为 0，否则**大声报错**（而不是静默继续造数）。"""
    left = _demo_residuals()
    pretty = " · ".join(f"{k} {v}" for k, v in left.items())
    print(f"清理后残留：{pretty}")
    dirty = {k: v for k, v in left.items() if v}
    if dirty:
        raise SystemExit(
            "清理不干净：" + ", ".join(f"{k}={v}" for k, v in dirty.items())
            + "。请先修 _clean() 再演示——否则重复运行会叠出重复数据。"
        )


# ============================================================
# 造数据
# ============================================================


def _seed_anchor() -> int:
    """建立**独立的演示锚点**（ASIN 固定 `B0MINE0001`），绝不碰用户真实锚点。

    ## 为什么不再用 `pool.update_baseline()`

    这是 QA 打回来的 D3：`update_baseline()` 对已存在的 MyProduct 是**更新**
    `.limit(1)` 的第一条——真实库里那就是用户的真锚点，于是跑一次演示会把
    ASIN 改成 `B0MINE0001`、核心词换成演示 5 词、价格/销量换成演示值。
    而 `--clean` 只在「锚点 ASIN 是演示值且没有竞品挂着」时才删，
    真实竞品还挂着 → 演示值永久留在真库里，之后所有价格带/节点判定都对着一个
    不存在的 ASIN。全程不报错。

    所以这里按 ASIN 精确查找（查不到才新建），从不读写"第一条"。
    演示竞品、演示动作、产品层阈值都挂在这个锚点上；`_clean` 也只认它。
    """
    with get_session() as s:
        my = s.execute(
            select(MyProduct).where(
                MyProduct.tenant_id == settings.tenant_id,
                MyProduct.asin == DEMO_MY_ASIN,
            ).limit(1)
        ).scalar_one_or_none()
        if my is None:
            my = MyProduct(
                tenant_id=settings.tenant_id,
                asin=DEMO_MY_ASIN,
                marketplace="US",
                platform="amazon",
            )
            s.add(my)
            s.flush()

        # 保本价 / 日均销量必须给：缺了跟价判断就只会说"判断不了"。
        for key, value in DEMO_MY.items():
            setattr(my, key, value)
        my.core_keywords = {
            "toy car": 0.31, "diecast car": 0.22, "kids toy car": 0.18,
            "mini car toy": 0.12, "car toy for boys": 0.09,
        }
        my.break_even_price = DEMO_MY["break_even_price"]
        s.flush()
        return int(my.id)


def _seed_users(owner_id: int) -> dict[str, int]:
    """第二个用户 + 3 条平台身份映射。

    为什么要造第二个人：面板右上角的「操作人」下拉只有一个人时，
    它看起来像个装饰；而验收要求「切到小李 → 录动作 → 列表显示小李」，
    这条链路没有第二个用户根本走不通。

    为什么身份映射要造 3 条、还跨渠道：`resolve_by_external()` 是通用的，
    但"同一个人的飞书号和企微号绑到同一个 user"这件事只有在数据里真的出现，
    才能验证「未知身份回落默认用户」与「已绑定身份反查出人」是两条不同的路径。
    这里刻意**漏掉**一个渠道（钉钉）：数据健康页要能演示"有飞书号还没绑人"。
    """
    member = identity.create_user(DEMO_MEMBER_NAME, role="member", tenant_id=settings.tenant_id)
    if not member.get("ok"):
        print(f"  ! 演示用户创建失败：{member.get('error')}")
        return {"owner": owner_id, "member": owner_id}

    member_id = int(member["id"])
    pairs = (
        (owner_id, "feishu", f"{DEMO_IDENTITY_PREFIX}owner_feishu"),
        (owner_id, "wecom", f"{DEMO_IDENTITY_PREFIX}owner_wecom"),
        (member_id, "feishu", f"{DEMO_IDENTITY_PREFIX}member_feishu"),
    )
    for uid, channel, ext in pairs:
        res = identity.upsert_identity(uid, channel, ext, tenant_id=settings.tenant_id)
        if not res.get("ok"):
            print(f"  ! 身份映射 {channel}:{ext} 失败：{res.get('error')}")
    return {"owner": owner_id, "member": member_id}


def _seed_competitors(my_id: int) -> dict[str, dict]:
    """8 个竞品，五个分层各至少一个，状态覆盖 active / paused / archived。"""
    now = datetime.now()
    out: dict[str, dict] = {}
    with get_session() as s:
        for scen in SCENARIOS:
            tier = scen["tier"]
            reasons = scen.get(
                "tier_reasons",
                {
                    "price_ratio": 0.96,
                    "price_band_ok": True,
                    "keyword_overlap": 0.71,
                    "keyword_pool_ok": True,
                    "node_match": True,
                    "functional_match": True,
                },
            )
            comp = Competitor(
                tenant_id=settings.tenant_id,
                my_product_id=my_id,
                platform="amazon",
                marketplace="US",
                asin=scen["asin"],
                tier=tier,
                tier_reasons=reasons,
                tier_updated_at=now - timedelta(days=3),
                price_ratio=scen.get("price_ratio", reasons.get("price_ratio")),
                keyword_overlap=scen.get("keyword_overlap", 0.71),
                node_match=scen.get("node_match", True),
                functional_match=scen.get("functional_match", True),
                title=scen["title"],
                brand=scen["brand"],
                status=scen.get("status", "active"),
                last_checked_at=now if scen.get("status", "active") != "paused"
                else now - timedelta(days=2),
            )
            s.add(comp)
            s.flush()
            out[scen["asin"]] = {"id": comp.id, "tier": tier, "title": scen["title"]}
    return out


def _pick(default: Any, changes: Optional[dict[int, Any]], days_ago: int) -> Any:
    """`changes` 的语义：从 `days_ago` 那天起，值变成这个。

    所以「今天」取所有 `key >= 0` 里 key 最小的那个……不对，是取**最新的那条**，
    即 `key` 最小（离今天最近）且 `days_ago <= key` 的项。
    """
    if not changes:
        return default
    value = default
    # 从最老的一天开始应用，后应用的覆盖先应用的 → 得到"从某天起"的阶梯
    for key in sorted(changes, reverse=True):
        if days_ago <= key:
            value = changes[key]
    return value


def _seed_history(scen: dict, cid: int) -> int:
    """一个竞品的完整时间线：快照 → 走真实的 DiffEngine 判出信号。

    **不假造信号**：所有 level 都是 `DiffEngine.compare()` 判出来的。
    如果哪天阈值改了，这个演示的结论也会跟着变——这才是"演示"该有的样子，
    否则演示只是在展示一段写死的数字。
    """
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    days = int(scen["days"])
    base = float(scen["base_price"])
    asin = scen["asin"]

    rows: list[Snapshot] = []
    for days_ago in range(days - 1, -1, -1):
        price = _pick(base, scen.get("price_changes"), days_ago)
        net: Optional[float] = price
        if days_ago in scen.get("net_none_days", set()):
            # 促销接口失败：价格不可知。**不要补 0**——补 0 会被当成"降到 0"。
            net = None
        title = scen["title"]
        suffix_at = scen.get("title_suffix_at")
        if suffix_at is not None and days_ago <= suffix_at:
            title = scen["title"] + "（新包装）"
        in_stock = days_ago not in scen.get("stock_days", set())
        quality = "partial" if days_ago in scen.get("net_none_days", set()) else "full"
        rows.append(
            Snapshot(
                competitor_id=cid,
                captured_at=now - timedelta(days=days_ago),
                price=price,
                coupon_value=0.0,
                net_price=net,
                rank_main=_pick(32000, scen.get("rank_changes"), days_ago),
                rank_sub=_pick(2800, scen.get("rank_changes"), days_ago),
                rank_label="Toys & Games",
                # 体量随层级走：S/A 要与自家（日均 40）咬合，否则 S 会被降成 A
                monthly_units=_pick(
                    _DEMO_UNITS_BASE.get(str(scen.get("tier") or "").upper(), 900),
                    scen.get("units_changes"),
                    days_ago,
                ),
                monthly_revenue=_pick(27000.0, None, days_ago),
                rating=_pick(4.5, scen.get("rating_changes"), days_ago),
                review_count=_pick(500, scen.get("review_changes"), days_ago),
                in_stock=in_stock,
                stock_hint="IN_STOCK" if in_stock else "OOS",
                title=title,
                image_url=None,
                # 分层判断的两维：不给就是 None（不猜）。0005 刻意留空，
                # 用来演示「证据不足 → unknown」这条线。
                keywords=_demo_keywords(scen),
                category_path=None if scen.get("missing_dims") else list(DEMO_COMP_PATH),
                data_quality=quality,
                source="demo",
            )
        )

    with get_session() as s:
        for r in rows:
            s.add(r)
        s.flush()

        # 逐对比较，走真实的判定引擎
        total = 0
        for i in range(1, len(rows)):
            prev, curr = rows[i - 1], rows[i]
            prev_u = collector.row_to_unified(prev)
            curr_u = collector.row_to_unified(curr)
            oos_confirmed = (prev.stock_hint or "").upper() == "OOS" or prev.in_stock is False
            phase = "warmup" if i == 1 else "normal"
            try:
                drafts = DiffEngine(phase).compare(
                    prev_u,
                    curr_u,
                    tier=scen["tier"],
                    my_price=DEMO_MY["current_price"],
                    oos_confirmed=oos_confirmed,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {asin} 第 {i} 对判定失败（跳过）：{exc}")
                continue
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
                        detected_at=curr.captured_at,
                        # P0/P1 在"当天"就推了；P2 不推（进周报）
                        pushed_at=curr.captured_at if d.level in ("P0", "P1") else None,
                    )
                )
                total += 1
    return total


def _seed_bindings(comps: dict[str, dict], my_id: int, uid: int) -> None:
    """2 条对标绑定，走真实的 `upsert_from_judgment`（basis 里带当时的阈值版本）。"""
    for asin in ("B0DEMO0001", "B0DEMO0002"):
        c = comps[asin]
        res = binding.upsert_from_judgment(
            tenant_id=settings.tenant_id,
            competitor_id=c["id"],
            my_product_id=my_id,
            tier=c["tier"],
            tier_reasons={
                "price_ratio": 0.96,
                "price_band_ok": True,
                "keyword_overlap": 0.71,
                "keyword_pool_ok": True,
                "node_match": True,
                "functional_match": True,
            },
            user_id=uid,
            confidence=0.82,
        )
        if not res.get("ok"):
            print(f"  ! 绑定 {asin} 失败：{res.get('error')}")


def _seed_thresholds(my_id: int, uid: int) -> None:
    """两层各写一点覆盖值。

    不写的话「阈值配置」页会全部显示"系统默认"、「数据健康」页的"阈值来源"是一张空表——
    而这两处恰恰是这个系统最该被看懂的地方：改了哪个键、现在生效的是哪一层。
    所以演示数据必须让两层都有覆盖，并且**故意重叠一个键**（`price_p1_pct`），
    这样面板上能直接看到"产品层盖住了公司层"。
    """
    from app.services import settings_store

    company = settings_store.save(
        scope="company",
        group_name="diff",
        values={"price_p0_pct": 0.025, "price_p1_pct": 0.008},
        tenant_id=settings.tenant_id,
        operator=DEMO_OPERATOR,
        note="演示：旺季收紧价格判定",
    )
    product = settings_store.save(
        scope="product",
        group_name="diff",
        values={"price_p1_pct": 0.012},
        scope_id=str(my_id),
        tenant_id=settings.tenant_id,
        operator=DEMO_OPERATOR,
        note="演示：这个产品对小幅降价更敏感",
    )
    for label, res in (("company", company), ("product", product)):
        if not res.get("ok"):
            print(f"  ! 阈值覆盖写入失败（{label}）：{res.get('error')}")


def _seed_collect_runs(comps: dict[str, dict], my_id: int) -> None:
    """采集日志：既有成功也有失败。没有失败的健康页看不出"失败长什么样"。

    `fail_collect_days` 是 `{几天前: 错误文案}`，允许多天、多个竞品各自失败。
    保留旧的 `fail_collect_today` 字符串写法兼容（等价于 `{0: 文案}`）——
    这个脚本是给人改着玩的，别人加场景时很可能照着老字段名写。
    """
    now = datetime.now()
    with get_session() as s:
        for scen in SCENARIOS:
            cid = comps[scen["asin"]]["id"]
            fail_days: dict[int, str] = dict(scen.get("fail_collect_days") or {})
            legacy = scen.get("fail_collect_today")
            if legacy:
                fail_days.setdefault(0, legacy)
            for days_ago in range(min(3, scen["days"])):
                err = fail_days.get(days_ago)
                failed = err is not None
                s.add(
                    CollectRun(
                        tenant_id=settings.tenant_id,
                        competitor_id=cid,
                        asin=scen["asin"],
                        started_at=now - timedelta(days=days_ago, minutes=7),
                        finished_at=now - timedelta(days=days_ago, minutes=6),
                        ok=not failed,
                        data_quality="failed" if failed else "full",
                        signal_count=0 if failed else 1,
                        trigger="schedule",
                        errors=[err] if failed else None,
                        duration_ms=1450 if not failed else 30000,
                    )
                )


def _seed_actions(uid: int, my_id: int) -> None:
    """3 条动作记录：两条来自飞书卡片，一条是在面板上手补的。

    **必须显式传 `my_product_id`**：不传的话 `action_log.record` 会走
    `binding.resolve_my_product()` 兜底到"租户默认品"——在真实库里就是用户的真锚点，
    演示动作就混进真实数据里了（之后 `--clean` 要么删不掉、要么连真的一起删）。

    `reason` 统一加 `DEMO_REASON_PREFIX` 前缀：既让面板上"一眼看出是演示"，
    也让 `--clean` / `_demo_residuals()` 有一个与 `my_product_id` 无关的认领凭据。
    """
    now = datetime.now()
    for spec in DEMO_ACTIONS:
        res = action_log.record(
            settings.tenant_id,
            action_type=spec["action_type"],
            asin=spec["asin"],
            description=spec["description"],
            reason=f"{DEMO_REASON_PREFIX}{spec['reason']}",
            verify_days=spec.get("verify_days"),
            user_id=uid,
            my_product_id=my_id,
            source=spec["source"],
        )
        if not res.get("ok"):
            print(f"  ! 动作记录失败：{res.get('error')}")
            continue
        # 把时间往前挪，让时间线看起来是几天里陆续记的。
        # 验证日期要跟着一起挪：否则「4 天前记的动作、验证期 2 天」这条
        # 永远不会到期，就演示不出「已到期未复盘」那个提醒。
        with get_session() as s:
            row = s.get(Action, res["action_id"])
            if row is not None:
                base = now - timedelta(days=spec["days_ago"])
                row.executed_at = base
                row.verify_due_at = (
                    base + timedelta(days=int(spec["verify_days"]))
                    if spec.get("verify_days")
                    else None
                )


# ============================================================
# 打印 / 起服务
# ============================================================


def _report(comps: dict[str, dict]) -> None:
    ids = [c["id"] for c in comps.values()]
    with get_session() as s:
        # 日报按"租户第一条自家产品"生成（`briefer` 就是这么实现的）。
        # 演示锚点平时就是唯一一条，所以 `demo_is_primary` 为真。
        # 为假的情况只可能出现在"守卫被绕过"的库上——那时宁可少打一份日报，
        # 也不要打一份不含演示竞品、让人误以为"演示没生效"的日报。
        first_my = s.execute(
            select(MyProduct).where(MyProduct.tenant_id == settings.tenant_id)
            .order_by(MyProduct.id).limit(1)
        ).scalar_one_or_none()
        demo_is_primary = first_my is not None and first_my.asin == DEMO_MY_ASIN
        brief = briefer.build_daily_brief(settings.tenant_id, hours=24) if demo_is_primary else None

        snap_n = len(
            s.execute(select(Snapshot.id).where(Snapshot.competitor_id.in_(ids))).scalars().all()
        )
        sig_rows = list(
            s.execute(select(Signal).where(Signal.competitor_id.in_(ids))).scalars()
        )
        # 动作数按 `[演示]` 前缀数，与 `_clean()` / `_demo_residuals()` 同一口径，
        # 这样"造完 3 条"和"清完 0 条"两边对得上。
        act_n = len(
            s.execute(
                select(Action.id).where(
                    Action.tenant_id == settings.tenant_id,
                    Action.reason.like(f"{DEMO_REASON_PREFIX}%"),
                )
            ).scalars().all()
        )
        bind_n = (
            len(
                s.execute(
                    select(CompetitorBinding.id).where(
                        CompetitorBinding.competitor_id.in_(ids)
                    )
                ).scalars().all()
            )
            if ids
            else 0
        )
        run_n = len(
            s.execute(select(CollectRun.id).where(CollectRun.competitor_id.in_(ids))).scalars().all()
        )
        fail_n = len(
            s.execute(
                select(CollectRun.id).where(
                    CollectRun.competitor_id.in_(ids), CollectRun.ok.is_(False)
                )
            ).scalars().all()
        )
        user_n = len(
            s.execute(select(User.id).where(User.tenant_id == settings.tenant_id)).scalars().all()
        )
        id_n = len(
            s.execute(
                select(UserIdentity.id).where(
                    UserIdentity.external_id.like(f"{DEMO_IDENTITY_PREFIX}%")
                )
            ).scalars().all()
        )

    by_level: dict[str, int] = {}
    for sig in sig_rows:
        by_level[sig.level] = by_level.get(sig.level, 0) + 1

    print("=" * 72)
    print("离线演示数据已就绪（全部为假数据，不连卖家精灵）")
    print("=" * 72)
    print(f"竞品 {len(comps)} 个 · 快照 {snap_n} 条 · 信号 {len(sig_rows)} 条"
          f"（P0 {by_level.get('P0', 0)} / P1 {by_level.get('P1', 0)} / P2 {by_level.get('P2', 0)}）")
    print(f"动作 {act_n} 条 · 对标绑定 {bind_n} 条 · 采集轮次 {run_n} 条（其中失败 {fail_n} 条）")
    print(f"用户 {user_n} 个 · 演示身份映射 {id_n} 条（面板右上角可切换操作人）")
    print("")
    print("五视图各自能看到：")
    print("  数据看板  7 行 = 6 个在盯 + 1 个已暂停（灰显）；已归档的 1 个只在竞品池里")
    print("            （归档 = 不再盯，看板只回答「我在盯谁」，所以它被排除；")
    print("             暂停 = 暂时不采集，仍在盯的名单里，所以灰显保留）")
    print("            B0DEMO0005 显示「待判定」并说明缺流量词/缺类目路径；")
    print("            B0DEMO0006 今天价格是「—」而不是 0")
    print("  阈值配置  两层（公司默认 / 产品覆盖），试算按 P0/P1/P2 分行")
    print("  竞品池    状态覆盖 在盯 / 已暂停 / 已归档；判定理由逐项列出")
    print("  动作记录  3 条，2 条来自飞书卡片、1 条面板手补")
    print("  数据健康  价格可信度曲线在 B0DEMO0006 那天掉一根红柱")
    print("")
    print("-" * 72)
    if brief is None:
        print("今日日报：当前库里第一条自家产品不是演示锚点，说明这个库不是纯演示库")
        print("          （正常路径上 `_guard_target_db()` 已经拒绝过这种库了）。")
        print("          要看完整演示，用默认的独立库即可：python scripts/demo_offline.py")
    else:
        print("今日日报（/api/brief 的同一份内容）：")
        print("")
        print(brief.to_text())
    print("-" * 72)
    print("")
    print("起服务看面板：")
    print("  python scripts/demo_offline.py --serve    →  http://127.0.0.1:8000/panel")
    print("  或：  uvicorn app.api.server:app --port 8000")
    print("")
    print("清掉演示数据：python scripts/demo_offline.py --clean")
    print(f"（演示库固定在 {DEFAULT_DEMO_DB_PATH.relative_to(ROOT).as_posix()}，"
          "主库 data/competitor_monitor.db 全程不碰）")


def _serve(port: int, db_path: Optional[Path] = None) -> None:
    """起服务。`db_path` 只用于打印，让人知道面板连的是哪个库。"""
    import uvicorn

    shown = db_path if db_path is not None else TARGET_DB_PATH
    print(f"正在启动服务：http://127.0.0.1:{port}/panel  （Ctrl+C 停止）")
    print(f"  连的库：{shown}")
    uvicorn.run("app.api.server:app", host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
