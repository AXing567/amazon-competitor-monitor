"""真实数据 pilot：拿一个**真实的亚马逊 ASIN** 当「自家产品」，跑真实竞品池。

## 为什么要有这一步

在这之前，面板上所有数字都对得上，但那只证明**内部自洽**。锚点是手填的假设值，
竞品是 `B0DEMO*`，于是「跟谁比、判得对不对」这件事从来没被真实数据检验过。
演示数据能证明算法跑得通，不能证明它判得对——这两件事差得很远。

这个脚本把数据源换成卖家精灵选品库的真实返回，走**同一套判定与落库代码**，
让「分层结果」第一次对着真实市场分布产生。

## 数据从哪来（离线可重跑，不烧额度）

原始返回已经落盘在 `data/pilot_raw/*.json`，由 `scripts/bridge_probe.py` 拉的：

    # 按类目节点选品（Dish Racks，节点路径由 /market/research 给出），翻 5 页
    for page in 1 2 3 4 5; do
      python scripts/bridge_probe.py get --path /api/v1/product/filter \
        --tag filter_band_p$page \
        --param node_id_path=1055398:284507:510136:23691552011:695483011 \
        --param min_price=18 --param max_price=36 --param min_sales=300 \
        --param size=100 --param order_field=bsr --param order_desc=false --param page=$page
    done

本脚本只读这些文件，**不再打网络**。原因有两个：额度是共享的、每次重跑都烧一次；
以及「跑出来的结论要能回溯到当时的原始返回」——只存映射后的快照是回溯不回去的。

选品库一次请求返回 100 行，但**同一条 listing 的几十个变体会各占一行**（实测第一页
100 行只有 5 个独立 listing）。所以下面按 `parent_asin` 归并、每个 listing 只取一个子体，
否则「竞品池」会变成「同一家的 60 个变体」。

## 哪些是真的、哪些不是（这张表比结论更重要）

| 字段 | 真假 | 来源 |
|---|---|---|
| 标价 / 月销估算 / BSR / 评分 / 评论数 / 上架日期 / 品牌 / 标题 | **真**（公开数据） | 选品库 |
| 类目节点 `node_match` | **构造保证**：候选是按节点筛出来的，不是猜的 | 选品库按 node_id_path 筛 |
| 到手价（净价）、优惠券 | **拿不到** | 选品库只给标价 |
| 流量词 / 词重合度 | **真**（由 `scripts/collect_keywords.py` 采、落盘缓存） | 选品库反查 |
| 保本价 / 目标毛利率 / FBA 可售库存 / 补货周期 | **假设值**（卖家自己的账，外部给不出） | 本脚本按锚点反推 + 标 `assumed_fields` |
| 日均销量 | **别人家的估算**（BSR 幂律模型），不是订单级真值 | 月销估算 ÷ 30 |
| 退货率 | **无处可落**（模型没有这个字段）→ 本轮不参与任何判断 | —— |

> 流量词这一行之前写的是「拿不到（该账号关键词模块无权益）」。**那句话是错的**：
> 额度一直有，是 bridge 长进程的会话陈旧，页面返回了没有结果集的壳，而 notice
> 把这种情况写成了「需要会员额度」。重启 bridge 后同一个词立刻返回月搜 702,084。
> 教训记在这里：接口返回 0 条时，「没权限」和「没采到」是两件事，
> 而它们的输出长得一模一样。

## 这个库里的「自家锚点」是 Mock，脚本必须自己承认（2026-11-06 补）

拿一个真实竞品的公开数据当「我们自己」，做出来的锚点**是演示数据**：
它的价格、星级、评价数来自别人家的商品页，库存和保本价是拍的。
这和「接入自家后台」在面板上长得一模一样，所以——

- 本脚本整轮跑在 **Mock 模式**下（`set_data_mode(DATA_MODE_MOCK)`），
  `mock_database_url` 被指向 `--db` 那个文件。于是 `pool.update_baseline` 的
  来源闸门不会把它当真实数据放行，也不用给 `sellersprite_proxy` 开后门。
- 库里那行锚点带着 `source='sellersprite_proxy'`，逐格 `field_sources` 分清
  「反推来的」和「模拟补充的」。切到**真实模式**看这个库，锚点会被判为不可用
  并显示原因——那是正确行为，不是 bug。
- 要看这套演示数据，起服务时两个连接串都指同一个文件（脚本结尾会打印完整命令）。

自家真实数据只有两条路：SP-API 同步（`app/services/self_sync.py`）和手工录入。

由此，这一轮能信的结论和不能信的结论是分开的：

- 能信：同战场识别（节点）、价格带是否重叠（标价对标的，双方同口径）、谁在价格带外、
  **词重合度**（锚点与竞品的核心出单词用同一套口径截取）
- 只能暂定：只有那些**确实没采到词**的竞品（缓存里没有它的文件）。有词的竞品
  给出的是真档位，`provisional` 为 False。
  面板上没词的必须读成「暂定 B」，不能读成「轻度重合」
- 不能用：断货预警、提价/降价空间、补货建议（依赖假设值）

## 观察窗口（采样偏差，必须知道）

候选不是「该节点的全部商品」，而是**这几次拉取的窗口**：

- `filter_dishracks_100.json`：按销量降序第 1 页（100 行）——头部 listing 的变体占满整页；
- `filter_dishracks_band.json` + `filter_band_p2..p5`：**带 $18~$36 价格窗**、按 BSR 升序翻 5 页
  （这个价格窗是探查阶段为了看中腰部而加的，不是判定规则）；
- `filter_dishracks.json`：按 BSR 降序第 1 页（20 行），补低价端的尝试；
- `filter_band_lowprice.json` + `..._p2.json`：按**价格升序**翻 2 页、不带价格窗、
  `max_bsr=20000`，覆盖 $1.41~$16.99 —— 这一对是专门用来消掉低价段偏差的。

上一轮结尾时 $16.78 以下完全没被采样，当时只能说「窗口里没有低价竞品」。
补齐后发现：这个节点的价格结构是**双峰**的——$1.4~$13 是一大簇（100 条里有 95 条价格比 <0.7，
全部落在四同法则的带外），$16~$36 是另一簇，两簇之间 $13~$16 很薄。
锚点定在 $22.99，落在上面那一簇里。

所以现在能说的比上一轮强一句：**低价段不是没人，是形态不同档**——
它们真实存在且量大，但价格比 0.30~0.57，按四同法则本就不该入池。
仍然不能说的是「$13~$16 这个窄缝里没有别的对手」——那需要按价格窗专门再拉一次。

## 这一轮跑不出「变化」

只有一轮快照，没有前后对比动作。`DiffEngine` 要两次快照才能判「他降价了」。
所以 pilot 的产出是**基准线**：池子里有谁、各自什么档、凭什么。变化检测要等第二轮采集
（那时还需要促销接口确认到手价，否则价格维度按 `_effective_price` 的规则直接失效）。

## 用法

    python scripts/pilot_real.py                    # 造 pilot 数据 + 打印报告
    python scripts/pilot_real.py --serve            # 造数 + 起服务看面板
    python scripts/pilot_real.py --limit 10         # 只取 BSR 最靠前的 10 个竞品（**会截断**，见下）
    python scripts/pilot_real.py --anchor B0XXXXXXX # 换一个自家产品（必须是 raw 里有的）
    python scripts/pilot_real.py --db data/x.db     # 换库（默认 data/pilot.db，绝不写主库）

## 为什么 `--limit` 默认是 0（不限）

第一版默认 20，按 BSR 升序截断。补了低价段数据之后，池子从 17 掉到 12——
**数据变多、竞品反而变少**，因为 $6~$15 那批便宜货 BSR 更好，把带内的真候选挤出了前 20。

这不是「池子本来就是这 12 个」，而是截断点把「采样窗口」混进了「判定规则」：
被挤掉的候选从来没被判过，报告里也就看不到它们被拒的理由。

所以默认改成不限，`--limit N` 只在明确要一张短名单时用，且**必须**在报告里
看到那行截断提示——「有 M 个没评估」和「这 M 个不合格」是两件完全不同的事。

## 写哪个库

默认 `data/pilot.db`——独立库，不混演示库（`demo_offline.db`）、不碰主库
（`competitor_monitor.db`）。真实数据和假数据一旦混在一个库里，以后就分不清了，
所以这是硬规则而不是建议：目标库是主库或演示库时，脚本**拒绝运行**（`SystemExit`）。

重跑是幂等的：会先删掉**本锚点名下**的竞品 / 快照 / 信号再重写。删除范围限定在
`my_product_id` 关联行，不会碰到别的锚点的数据。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ============================================================
# 数据库落点：**必须在** `from app.config import settings` 之前决定
#
# `app/config.py` 末尾是 `settings = get_settings()`，导入时就实例化并读完环境变量；
# 之后改 `os.environ` 对 `settings.database_url` 无效，`app/db.py` 的引擎也早就绑死了。
# ============================================================

#: 主库（用户的真实数据）。本脚本**永远**不写它。
MAIN_DB_PATH = ROOT / "data" / "competitor_monitor.db"
#: 演示库。也不写：`demo_offline.py --clean` 会按自己的谓词删数据，两边互相踩。
DEMO_DB_PATH = ROOT / "data" / "demo_offline.db"
#: pilot 默认库。
DEFAULT_PILOT_DB_PATH = ROOT / "data" / "pilot.db"

RAW_DIR = ROOT / "data" / "pilot_raw"

#: `collect_keywords.py` 落盘的流量词缓存（一个 ASIN 一个文件，可断点续跑）。
KEYWORD_CACHE_DIR = RAW_DIR / "keyword_cache"

#: pilot 用的类目节点（`/market/research` 返回的真实节点）。
#: 候选全部来自这个节点——这就是「同战场」的**构造保证**，
#: 也是 `node_match` 唯一正当的来源（选品库不返回商品的类目路径，商品详情接口那个
#: `category_path` 字段恒为空，三个 ASIN 交叉确认过）。
NODE_ID_PATH = "1055398:284507:510136:23691552011:695483011"
NODE_LABELS = [
    "Home & Kitchen",
    "Kitchen & Dining",
    "Storage & Organization",
    "Racks & Holders",
    "Dish Racks",
]
#: BSR 是大类排名（选品库的 `bsr_rank` 是 Home & Kitchen 大类），不是子类目排名。
#: 所以它进 `Snapshot.rank_main`，**不进** `MyProduct.sub_category_rank`——
#: 把大类排名填进子类目字段，面板上「子类目 BSR 2440」就是一句假话。
MAIN_CATEGORY = "Home & Kitchen"

#: 落盘文件清单（显式列出来，可审计）。
#: 不扫 `filter_*.json` 的 glob：目录里还有按关键词搜玩具车的文件，
#: 那是另一个节点的商品，混进来会破坏「同节点」这个构造保证。
NODE_RAW_FILES = (
    "filter_dishracks.json",
    "filter_dishracks_100.json",
    "filter_dishracks_band.json",
    "filter_band_p2.json",
    "filter_band_p3.json",
    "filter_band_p4.json",
    "filter_band_p5.json",
    # 补采样：按价格升序、**不带价格窗**、`max_bsr=20000`，两页共覆盖 $1.41~$16.99。
    # 加这两页之前，$16.78 以下的低价段一个都没采到——这是上一轮遗留的采样偏差。
    "filter_band_lowprice.json",
    "filter_band_lowprice_p2.json",
)

#: 候选门槛。**刻意不按价格带筛**：价格带是 `judge_tier` 要判的东西，
#: 先按它筛再报「全部在带内」等于自己给自己出题。这里只筛「有没有观察价值」。
MIN_MONTHLY_SALES = 300
MAX_BSR = 20000

#: 默认锚点：JASIWAY 太头部（月销 18844、62 个变体），拿它当「我们自己的产品」
#: 会让「跟价压力」这个前提失真。MERRYBOX 这家中腰部更贴近真实的中小卖家：
#: $22.99（节点均价 26.23 之内）、月销 3345（节点均值 3252）、BSR 2440、
#: 评分 4.8/5335 条评论、2023-03 上架（有历史可看）、FBA。
DEFAULT_ANCHOR_ASIN = "B0BYVBMPTD"


def _argv_db_path(argv: list[str]) -> Optional[Path]:
    """从 argv 取 `--db`（支持 `--db PATH` 与 `--db=PATH`），argparse 来不及。"""
    for i, item in enumerate(argv):
        if item == "--db":
            return Path(argv[i + 1]).expanduser() if i + 1 < len(argv) else None
        if item.startswith("--db="):
            value = item.split("=", 1)[1]
            return Path(value).expanduser() if value else None
    return None


def _preset_database_url(argv: list[str]) -> Path:
    """决定这次打哪个库并写进 `DATABASE_URL`。优先级：`--db` > 已有环境变量 > 默认。"""
    explicit = _argv_db_path(argv)
    if explicit is not None:
        path = explicit if explicit.is_absolute() else (Path.cwd() / explicit)
        resolved = path.resolve()
        os.environ["DATABASE_URL"] = f"sqlite:///{resolved.as_posix()}"
        return resolved

    existing = os.environ.get("DATABASE_URL", "").strip()
    if existing:
        prefix = "sqlite:///"
        return Path(existing[len(prefix):]) if existing.startswith(prefix) else Path(existing)

    os.environ["DATABASE_URL"] = f"sqlite:///{DEFAULT_PILOT_DB_PATH.as_posix()}"
    return DEFAULT_PILOT_DB_PATH


TARGET_DB_PATH = _preset_database_url(sys.argv)


from sqlalchemy import delete, select  # noqa: E402

from app.adapters.unified import TieringResult, UnifiedSnapshot   # noqa: E402
from app.config import settings                                   # noqa: E402
from app.db import (                                              # noqa: E402
    DATA_MODE_MOCK,
    get_session,
    init_db,
    set_data_mode,
)
from app.domain import baseline as baseline_mod                   # noqa: E402
from app.domain.tiering import fmt_ratio_to_show_side, judge_tier  # noqa: E402
from app.models import (                                          # noqa: E402
    Competitor,
    CompetitorBinding,
    MyProduct,
    Signal,
    Snapshot,
)
from app.services import binding, collector, pool, settings_store  # noqa: E402


# ============================================================
# 把这一轮钉在 Mock 模式上（见模块 docstring 的「自家锚点」一节）
# ============================================================


def _bind_mock_mode_to_target_db() -> None:
    """让 Mock 模式指向 `--db` 那个文件，并把进程切到 Mock。

    两个动作缺一不可：
      1. `mock_database_url` 指向 `--db` 那个文件——否则 Mock 模式会跑去写
         `data/mock.db`，而 `--db` 指定的库只剩竞品没有锚点；
      2. `set_data_mode(MOCK)`——否则 `pool.update_baseline` 的来源闸门
         会（正确地）拒掉这次写入，脚本运行到一半才报错。

    **必须在 main() 里调，不能在 import 期调**：`tests/test_pilot_candidate_window.py`
    会 `import scripts.pilot_real` 来测候选筛选逻辑，而 `_current_mode` 是进程级的
    ContextVar——import 期一设，整轮 pytest 后面所有用例都跑在 Mock 模式下，
    症状是十几条风马牛不相及的用例一起红（本轮真踩过）。
    """
    settings.mock_database_url = settings.database_url
    os.environ["MOCK_DATABASE_URL"] = str(settings.database_url)
    set_data_mode(DATA_MODE_MOCK)


# ============================================================
# 守卫：这个库能不能被 pilot 写
# ============================================================


def _path_of_sqlite_url(url: str) -> Path:
    prefix = "sqlite:///"
    return Path(url[len(prefix):]) if url.startswith(prefix) else Path(url)


def _target_display() -> str:
    path = _path_of_sqlite_url(str(settings.database_url))
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def _assert_writable_db() -> None:
    """只看路径，不看内容。**必须在 `init_db()` 之前**——建表/迁移已经算动了那个库。

    Raises:
        SystemExit: 目标是主库或演示库。
    """
    target = _path_of_sqlite_url(str(settings.database_url))
    resolved = target.resolve()
    for forbidden, why in (
        (MAIN_DB_PATH, "那是你的真实数据（主库）"),
        (DEMO_DB_PATH, "那是演示库，`demo_offline.py --clean` 会按自己的谓词清它"),
    ):
        if target.name == forbidden.name or resolved == forbidden.resolve():
            raise SystemExit(
                f"目标库 {_target_display()} 不能写：{why}。"
                f"pilot 默认写 {DEFAULT_PILOT_DB_PATH.relative_to(ROOT).as_posix()}，"
                "要换地方请用 --db 指定一个独立的库。"
            )


def _assert_anchor_slot(anchor_asin: str) -> None:
    """库里若有**别的**自家锚点，说明这不是 pilot 库，拒绝动它。

    判据是「锚点 ASIN 是不是本次要写的那一个」，不是数量——一条真实数据就足够危险。
    """
    with get_session() as s:
        others = list(
            s.execute(
                select(MyProduct).where(
                    MyProduct.tenant_id == settings.tenant_id,
                    MyProduct.asin != anchor_asin,
                )
            ).scalars()
        )
        if others:
            first = others[0]
            raise SystemExit(
                f"目标库 {_target_display()} 里已有别的自家锚点（ASIN {first.asin}）。"
                f"pilot 只写 {anchor_asin} 这一个锚点，请用 --db 换一个独立库。"
            )


# ============================================================
# 读原始返回 → 候选集
# ============================================================


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> Optional[int]:
    f = _to_float(v)
    return int(f) if f is not None else None


def _load_raw_items() -> list[dict]:
    """把落盘的选品库返回合并成一张行表（同一 ASIN 只留第一次出现）。

    为什么要合并多页：同节点按 BSR 升序翻 5 页 × 100 行，归并后才有 100 多个
    独立 listing。单页 100 行只有 2~5 个 listing——变体把排位占满了。
    """
    rows: dict[str, dict] = {}
    missing: list[str] = []
    for name in NODE_RAW_FILES:
        path = RAW_DIR / name
        if not path.exists():
            missing.append(name)
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("items") or []:
            asin = str(item.get("asin") or "").upper()
            if asin and asin not in rows:
                rows[asin] = item
    if missing:
        print(f"  ! 缺这些原始文件（跳过）：{'、'.join(missing)}")
    if not rows:
        raise SystemExit(
            f"{RAW_DIR.relative_to(ROOT).as_posix()} 里没有可用数据。"
            "先用 scripts/bridge_probe.py 拉一轮选品库返回（命令见本文件顶部说明）。"
        )
    return list(rows.values())


def _families(items: list[dict]) -> dict[str, list[dict]]:
    """按 `parent_asin` 归并变体（没有 parent 的按自己算一个家族）。"""
    fam: dict[str, list[dict]] = {}
    for item in items:
        key = str(item.get("parent_asin") or item.get("asin") or "")
        fam.setdefault(key, []).append(item)
    return fam


def _representative(variants: list[dict]) -> dict:
    """一个 listing 只取一个子体，取**最便宜**的那个。

    为什么是最便宜而不是最贵/中位：跟价压力来自对手摆出来的最低价，
    而变体之间的差别主要就是规格与价格。取最便宜的，价格带判定不会因为
    挑了个贵的变体而把一个真在打价格战的对手判到带外去。
    这条规则是**我们的选择**，不是数据给的——写在这里以便回头看时能质疑它。
    """
    priced = [v for v in variants if _to_float(v.get("price")) is not None]
    if not priced:
        return variants[0]
    return min(priced, key=lambda v: _to_float(v.get("price")) or 0.0)


def _pick_candidates(items: list[dict], anchor_asin: str, limit: int) -> list[dict]:
    """从真实行里挑出「值得看的竞品」，每条一个代表子体。

    规则（全部显式，便于质疑）：
      1. 排除锚点本身、以及锚点所在 listing 的其他变体（那是自己，不是对手）；
      2. 月销 ≥ `MIN_MONTHLY_SALES`、BSR ≤ `MAX_BSR` —— 没销量的链接观察成本高于价值；
      3. 按 `parent_asin` 归并后取最便宜子体；
      4. 按 BSR 升序排序；`limit > 0` 时截前 `limit` 个，`limit <= 0` 表示不截断。

    **不按价格带筛**：价格带是同一条判定规则里的一项，先按它筛再报「全在带内」
    等于把结论写进前提。价格带外的候选照样进池子，由 `judge_tier` 判成 C 并说明原因
    （它们在报告里能看到，只是不入池——那才是真实行为）。

    第 4 步的截断也是同一类陷阱：按 BSR 截断时，带外的便宜货会把带内的候选挤出去，
    而「没被判过」和「被判成 C」在报告里长得一模一样。所以调用方有义务把
    「截掉了几个」打出来，`limit` 的默认值因此是 0（不限）。
    """
    anchor_parent = None
    for item in items:
        if str(item.get("asin") or "").upper() == anchor_asin:
            anchor_parent = str(item.get("parent_asin") or anchor_asin)
            break

    usable: list[dict] = []
    for item in items:
        asin = str(item.get("asin") or "").upper()
        if asin == anchor_asin:
            continue
        parent = str(item.get("parent_asin") or asin)
        if anchor_parent and parent == anchor_parent:
            continue
        if _to_float(item.get("price")) is None:
            continue
        if (_to_int(item.get("monthly_sales")) or 0) < MIN_MONTHLY_SALES:
            continue
        # BSR 缺失的行直接跳过：`bsr_rank is None` 说明这条数据本身残缺，
        # 拿它去满足「有观察价值」这个前提就是在猜（第一版写的是 `is not None and ...`，
        # 于是缺排名的行一路放行）。
        bsr = _to_int(item.get("bsr_rank"))
        if bsr is None or bsr > MAX_BSR:
            continue
        usable.append(item)

    picked: list[dict] = []
    for parent, variants in _families(usable).items():
        rep = _representative(variants)
        rep = dict(rep)
        rep["_variant_count"] = len(variants)
        picked.append(rep)

    picked.sort(key=lambda r: (_to_int(r.get("bsr_rank")) or 10**9))
    if limit and limit > 0:
        return picked[:limit]
    return picked


# ============================================================
# 锚点
# ============================================================


def _core_words(asin: str) -> dict[str, float] | None:
    """锚点的核心出单词 {词: 流量占比}，取法与竞品完全一致。

    用 `UnifiedSnapshot.core_keyword_weights(0.8)` 而不是自己再写一遍累加：
    两边口径一旦不同，「词重合度」算的就是两套不同词集的交集，
    而结果看上去和真的没有区别。
    """
    kws = _load_keywords(asin)
    if not kws:
        return None
    weights = UnifiedSnapshot(
        asin=str(asin).upper(), marketplace="US", keywords=kws
    ).core_keyword_weights(0.8)
    return weights or None


def _anchor_fields(item: dict) -> dict:
    """把一条真实商品行翻译成自家锚点字段。

    **财务四项的处理**（pilot 期只能这么做）：保本价 / FBA 可售库存 / 补货周期 /
    目标毛利率外部数据源给不出，全是假设值，写作时进 `assumed_fields`；
    日均销量是「别人家的估算」（BSR 幂律模型），不是订单级真值，同样进 `assumed_fields`。
    退货率无处可落（模型没这个字段），所以它**不参与任何判断**，只在报告里点名。

    保本价的算法 `售价 × (1 − 估算毛利率)`：选品库的 `gross_margin` 是它自己那一套
    成本模型的估算，用它反推出来的保本价**只能当参考**——这个数往下直接决定
    「该不该跟价」，标成假设值是它唯一诚实的身份。
    """
    price = _to_float(item.get("price")) or 0.0
    margin = _to_float(item.get("gross_margin")) or 0.0
    daily = round((_to_int(item.get("monthly_sales")) or 0) / 30, 1)

    break_even = round(price * (1 - margin), 2) if price > 0 and 0 < margin < 1 else None

    asin = str(item.get("asin") or "").upper()

    fields = {
        "asin": asin,
        "title": item.get("title"),
        "image_url": item.get("image_url"),
        "current_price": price or None,
        "rating": _to_float(item.get("rating")),
        "review_count": _to_int(item.get("ratings_count")),
        # 大类排名不进 `sub_category_rank`（见 MAIN_CATEGORY 的说明），这里留空。
        "category_path": list(NODE_LABELS),
        "target_margin_rate": 0.25,
        "break_even_price": break_even,
        "daily_sales_avg": daily or None,
        # FBA 可售库存**刻意留空**。第一版这里填的是「日均 × 45 天」，
        # 于是 DOS 恒等于 45.0、`inventory_status` 恒为 `normal`——
        # 一个由分子分母反推出来的数字，看起来像"查到的库存"，实际上等于什么都没说，
        # 而且它会稳定地把「库存正常」印在面板上。宁可 unknown：
        # `inventory_status="unknown"` 会如实告诉人"这条判断现在做不了"。
        "fba_available_units": None,
        "lead_time_days": 35,
        # 锚点自己的出单词——这是 `calc_keyword_overlap` 的分子分母来源。
        # 取法必须和竞品一致（`core_keyword_weights` 按流量占比累计到 80%），
        # 两边口径不同的话，「重合度」会比的是两套不同的词集。
        "core_keywords": _core_words(asin),
        "source": baseline_mod.SOURCE_SELLERSPRITE,
        "assumed_fields": [
            "break_even_price",
            "target_margin_rate",
            "lead_time_days",
            "daily_sales_avg",
        ],
        # 逐格出身。整行 `source=sellersprite_proxy` 只说「这批数主要是反推的」，
        # 但保本价、目标毛利、补货周期卖家精灵根本给不出——它们是按售价和
        # 毛利率拍出来的。两者混在一个 `source` 里，面板上 13 个格子看起来一样可靠。
        #
        # `fba_available_units` 刻意不在表里：这一格留空，所以没有"出身"可言。
        # 给它标一个 origin 会让人以为那里有值，只是没显示。
        "field_sources": {
            "current_price": baseline_mod.ORIGIN_SELLERSPRITE,
            "rating": baseline_mod.ORIGIN_SELLERSPRITE,
            "review_count": baseline_mod.ORIGIN_SELLERSPRITE,
            "title": baseline_mod.ORIGIN_SELLERSPRITE,
            "image_url": baseline_mod.ORIGIN_SELLERSPRITE,
            "category_path": baseline_mod.ORIGIN_SELLERSPRITE,
            "core_keywords": baseline_mod.ORIGIN_SELLERSPRITE,
            # 月销 ÷ 30：数来自卖家精灵，但换成"日均"这一步是算出来的
            "daily_sales_avg": baseline_mod.ORIGIN_SELLERSPRITE,
            "break_even_price": baseline_mod.ORIGIN_SIMULATED,
            "target_margin_rate": baseline_mod.ORIGIN_SIMULATED,
            "lead_time_days": baseline_mod.ORIGIN_SIMULATED,
        },
    }
    return fields


def _write_anchor(item: dict) -> dict:
    """走生产入口 `pool.update_baseline()` 写锚点，拿到体检结果。"""
    fields = _anchor_fields(item)
    res = pool.update_baseline(settings.tenant_id, **fields)
    if not res.get("id"):
        raise SystemExit(f"锚点写入失败：{res}")

    # `update_baseline` 对 `None` 的语义是**「这一项不改」**，不是「清空」。
    # 于是「上一版写进去的假设库存」不会被这版的 None 擦掉：重跑时 DOS 会继续
    # 用上一次编的数字算出来，而它看起来和真的一样。显式清一次，并把体检结果
    # 重取一遍（`res` 里那些派生值已经过期了）。
    with get_session() as s:
        my = s.get(MyProduct, int(res["id"]))
        if my is not None and my.fba_available_units is not None:
            my.fba_available_units = None
    return pool.get_my_product(settings.tenant_id) or res


# ============================================================
# 竞品：快照 → 判定 → 落库
# ============================================================


def _load_keyword_payload(asin: str) -> tuple[list[dict], int | None]:
    """读 `collect_keywords.py` 落盘的流量词，连同**词库总词数**一起返回。

    总词数是必须的：上游每个 ASIN 只回 20 个词，而真实词库可以到 2000+。
    不带这个数，`calc_keyword_overlap` 会把「1% 抽样里没撞上主力词」算成
    「重合 0」——一个看起来像证据、实际上只是抽样假象的数字。
    """
    path = KEYWORD_CACHE_DIR / f"{str(asin).upper()}.json"
    if not path.exists():
        return [], None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return [], None
    return payload.get("keywords") or [], payload.get("traffic_keywords_total")


def _load_keywords(asin: str) -> list[dict]:
    """只要词列表。缺文件 = 没采过，返回空列表。"""
    return _load_keyword_payload(asin)[0]


def _snapshot_from_item(
    item: dict,
    *,
    captured_at: datetime,
    keywords: list[dict] | None = None,
    traffic_keywords_total: int | None = None,
) -> UnifiedSnapshot:
    """选品库的一行 → 统一快照。

    两个字段刻意留空，理由写在下面（这是本轮最容易被"顺手填上"的地方）：

    - `net_price` / `price_source`：选品库只给标价，拿不到券和到手价。
      把标价塞进 `net_price` 会让 `_effective_price()` 认为「价格已确认」，
      于是降价判断开始拿标价当到手价用——正是那个被修过的事故。
    - `keywords`：**由调用方从缓存传入**，缺缓存就是空列表。
      空列表的含义是「没采到」，下游据此把词维度算作 `None`（而不是 0 重合）。
      注意这里曾经写死 `keywords=[]`，注释是「账号无权益」——那句话是猜的，
      而且猜错了：额度一直是有的，只是 bridge 长进程的会话陈旧 + 没人调采集脚本。

    `category_path` 填节点路径，来源是「按这个节点筛的」，不是商品详情页读的
    （那个字段在选品库返回里恒为空）。这条是构造保证，不是抄来的事实。
    """
    kws = list(keywords or [])
    return UnifiedSnapshot(
        asin=str(item.get("asin") or "").upper(),
        marketplace="US",
        price=_to_float(item.get("price")),
        net_price=None,
        price_source=None,
        rank_main=_to_int(item.get("bsr_rank")),
        rank_label=MAIN_CATEGORY,
        monthly_units=_to_int(item.get("monthly_sales")),
        monthly_revenue=_to_float(item.get("monthly_revenue")),
        rating=_to_float(item.get("rating")),
        review_count=_to_int(item.get("ratings_count")),
        title=item.get("title"),
        brand=item.get("brand"),
        image_url=item.get("image_url"),
        release_date=(str(item.get("release_date"))[:10] if item.get("release_date") else None),
        fulfillment_type=item.get("seller_type"),
        category_path=list(NODE_LABELS),
        keywords=kws,
        # 词库总量 vs 实际拿到的条数 → 下游据此判断「重合 0」能不能作数。
        traffic_keywords_total=traffic_keywords_total,
        # 有词才够 full 的资格；没词仍然是 partial。
        # 报 full 会让数据健康页把这批快照算成"采全了"。
        data_quality="partial",
        # 与五接口聚合的 `sellersprite` 区分开：这批是选品库一次批量拉的。
        # 半年后对账时，"这条快照是怎么来的"靠这个值就能分开。
        source="sellersprite_library",
        captured_at=captured_at,
    )


def _clear_pool(my_id: int) -> int:
    """删掉本锚点名下的竞品 / 快照 / 信号 / 绑定，返回删掉的竞品数。

    删除范围**只认 `my_product_id`**：pilot 库里只有这一个锚点，
    而别的锚点的数据不该被顺手带走。
    """
    with get_session() as s:
        ids = list(
            s.execute(
                select(Competitor.id).where(
                    Competitor.tenant_id == settings.tenant_id,
                    Competitor.my_product_id == my_id,
                )
            ).scalars()
        )
        if not ids:
            return 0
        s.execute(delete(Signal).where(Signal.competitor_id.in_(ids)))
        s.execute(delete(CompetitorBinding).where(CompetitorBinding.competitor_id.in_(ids)))
        s.execute(delete(Snapshot).where(Snapshot.competitor_id.in_(ids)))
        s.execute(delete(Competitor).where(Competitor.id.in_(ids)))
        return len(ids)


def _write_one(
    my: MyProduct,
    item: dict,
    *,
    thresholds: Any,
    captured_at: datetime,
) -> tuple[dict, TieringResult, bool]:
    """判定 + 落库一个竞品。返回 `(摘要, 判定结果, 是否入池)`。

    判定走生产的 `judge_tier`，落库走生产的 `collector._save_snapshot`，
    这样"pilot 结果"和"生产结果"不会因为两套映射代码而分叉。

    入池规则同 `pool.add_competitor`：没通过四同法则就不放进池子（`status="observing"`
    留给 force 的场景）；证据不足（可判维度 < 2）不拒，标 unknown 等补数据。
    """
    _kws, _kw_total = _load_keyword_payload(item.get("asin"))
    snap = _snapshot_from_item(
        item, captured_at=captured_at, keywords=_kws, traffic_keywords_total=_kw_total
    )
    res = judge_tier(
        my_price=my.current_price,
        my_core_keywords=dict(my.core_keywords or {}),
        my_category_path=list(my.category_path or []) or None,
        my_daily_units=my.daily_sales_avg,
        comp=snap,
        # 功能形态是**人工确认**的输入，引擎算不出来。这里给 None（待确认）
        # 而不是 True：写 True 等于替人做了一个没做过的判断。
        functional_match=None,
        thresholds=thresholds,
    )

    admitted = res.passed or res.insufficient
    summary = {
        "asin": snap.asin,
        "brand": snap.brand,
        "price": snap.price,
        "price_ratio": res.price_ratio,
        # 表格里的价格比要和「拒收理由」那句话用同一个数字。
        # 两处各算一次 `:.2f` 的话，$15.99 那行会左边写 0.70、右边写 0.696。
        "price_ratio_text": (
            None
            if res.price_ratio is None
            else fmt_ratio_to_show_side(
                res.price_ratio, thresholds.price_ratio_min, thresholds.price_ratio_max
            )
        ),
        "bsr": snap.rank_main,
        "monthly_units": snap.monthly_units,
        "rating": snap.rating,
        "review_count": snap.review_count,
        "release_date": snap.release_date,
        "variants": item.get("_variant_count", 1),
        "tier": res.effective_tier,
        "passed": res.passed,
        "provisional": res.provisional,
        "missing_dims": list(res.missing_dims),
        "sample_thin": bool(res.tier_reasons.get("keyword_sample_thin")),
        "word_count": res.tier_reasons.get("keyword_word_count"),
        "keyword_overlap": res.keyword_overlap,
        "reject_reason": res.reject_reason,
        "admitted": admitted,
    }
    if not admitted:
        # 不入池就不写库：真实行为就是这样，报告里能看到被拒的和原因。
        return summary, res, False

    with get_session() as s:
        comp = Competitor(
            tenant_id=settings.tenant_id,
            my_product_id=my.id,
            platform="amazon",
            marketplace="US",
            asin=snap.asin,
            tier=res.effective_tier,
            tier_reasons=res.tier_reasons,
            tier_updated_at=captured_at,
            price_ratio=res.price_ratio,
            keyword_overlap=res.keyword_overlap,
            functional_match=res.functional_match,
            node_match=res.node_match,
            title=snap.title,
            image_url=snap.image_url,
            brand=snap.brand,
            status="active" if res.passed else "observing",
            last_checked_at=captured_at,
        )
        s.add(comp)
        s.flush()
        collector._save_snapshot(s, comp.id, snap)      # noqa: SLF001 生产入口，故意复用
        comp_id = int(comp.id)
    # 绑定必须**等上面的事务提交之后**再写：`binding.upsert_from_judgment()` 自己开一个
    # 新 session，而那时候竞品行还没 commit——它会查不到这个竞品，返回
    # `{"ok": False, "error": "竞品 #N 不存在"}`，然后被忽略。第一版就是这样：
    # 池子里 17 个竞品，绑定表 0 条，全程没有任何报错。
    bind_res = binding.upsert_from_judgment(
        tenant_id=settings.tenant_id,
        competitor_id=comp_id,
        my_product_id=my.id,
        tier=res.effective_tier,
        tier_reasons=res.tier_reasons,
    )
    summary["binding_id"] = bind_res.get("binding_id")
    summary["basis_summary"] = bind_res.get("basis_summary")
    if not bind_res.get("ok"):
        # 不静默：绑定写不进去，面板的「战场」列和「人话理由」就是空的，
        # 而报告里却有理由——两边说的不是一回事。
        summary["binding_error"] = bind_res.get("error")
    return summary, res, True


# ============================================================
# 报告
# ============================================================


def _fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _width(text: str) -> int:
    """显示宽度：中日韩字符占 2 列，其余 1 列。

    为什么不用 `len()`：品牌名一半是中文（`7 code` 是 6 列，`沥水架` 是 6 列但
    `len` 只有 3），按 `len` 对齐的表在前端一换字体就散架，且数字列会错位。
    """
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: Any, width: int, align: str = "left") -> str:
    text = str(text)
    gap = max(0, width - _width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def _print_report(anchor: dict, rows: list[dict], captured_at: datetime) -> None:
    print()
    print("=" * 92)
    print("锚点体检（自家产品的数据底座）")
    print("=" * 92)
    print(f"  ASIN        {anchor['asin']}   来源档位 {anchor['source']}")
    print(f"  售价        {_fmt(anchor['current_price'])}    保本价 {_fmt(anchor['break_even_price'])}"
          f"    目标毛利率 {_fmt(anchor['target_margin_rate'])}")
    print(f"  日均销量    {_fmt(anchor['daily_sales_avg'])}    FBA 可售 {_fmt(anchor['fba_available_units'])}"
          f"    补货周期 {_fmt(anchor['lead_time_days'], 0, ' 天')}")
    print(f"  类目路径    {' › '.join(anchor['category_path'])}")
    print(f"  就绪度      {anchor['readiness']}（{anchor['grade']} 级）"
          f"    DOS {_fmt(anchor['days_of_supply'], 1, ' 天')}（{anchor['inventory_status']}）")
    print(f"  假设值      {'、'.join(anchor['assumed_fields']) or '（无）'}")
    for warn in anchor["warnings"]:
        print(f"  ⚠ {warn}")

    admitted = [r for r in rows if r["admitted"]]
    rejected = [r for r in rows if not r["admitted"]]

    print()
    print("=" * 92)
    print(f"竞品池（入池 {len(admitted)} 个 / 扫到 {len(rows)} 个，扫描时间 {captured_at:%Y-%m-%d %H:%M}）")
    print("=" * 92)
    print(
        _pad("ASIN", 13) + _pad("品牌", 17) + _pad("价格", 8, "right")
        + _pad("价格比", 8, "right") + _pad("BSR", 8, "right") + _pad("月销", 8, "right")
        + _pad("评论", 8, "right") + "  " + _pad("档位", 12) + "说明"
    )
    print("-" * 92)
    for r in sorted(admitted, key=lambda x: (_tier_order(x["tier"]), x["price_ratio"] or 9)):
        print(
            _pad(r["asin"], 13) + _pad((r["brand"] or "")[:15], 17)
            + _pad(_fmt(r["price"]), 8, "right") + _pad(r["price_ratio_text"] or "—", 8, "right")
            + _pad(_fmt(r["bsr"]), 8, "right") + _pad(_fmt(r["monthly_units"]), 8, "right")
            + _pad(_fmt(r["review_count"]), 8, "right") + "  "
            + _pad(_tier_cell(r), 12) + _row_note(r)
        )

    if rejected:
        print()
        print(f"未入池（{len(rejected)} 个，真实行为就是不放进来——列在这里是为了让「为什么不算竞品」可见）")
        print("-" * 92)
        for r in sorted(rejected, key=lambda x: x["price_ratio"] or 9):
            print(
                _pad(r["asin"], 13) + _pad((r["brand"] or "")[:15], 17)
                + _pad(_fmt(r["price"]), 8, "right") + _pad(r["price_ratio_text"] or "—", 8, "right")
                + "  " + _pad(f"{r['tier']}", 4) + (r["reject_reason"] or "—")
            )

    print()
    print("=" * 92)
    print("这一轮能信什么、不能信什么")
    print("=" * 92)
    dist: dict[str, int] = {}
    for r in admitted:
        key = f"{r['tier']}{'（暂定）' if r['provisional'] else ''}"
        dist[key] = dist.get(key, 0) + 1
    print(f"  入池档位分布  {'、'.join(f'{k} × {v}' for k, v in sorted(dist.items())) or '（空）'}")
    print("  能信  同战场识别（按节点筛出，构造保证）、价格带是否重叠（标价对标的，双方同口径）")
    print("  暂定  S/A/B/C 的字母档：流量词拿不到 → 词重合一维缺 → 只敢给中间档 B 并标暂定")
    print("  不能用 断货预警、提价/降价空间、补货建议 —— 它们依赖的保本价/库存/日均销量是假设值")
    print("  本轮没有变化信号：只有一轮快照，没有前后对比；变化检测要等第二轮采集，")
    print("        且那时必须有促销接口确认到手价，否则价格维度按规则直接失效（宁可漏报）")
    print("  退货率上无处可落（数据模型没有这一列），本轮不参与任何判断")
    print("  采样窗口 按销量第 1 页 + $18~$36 带内按 BSR 5 页 + 按价格升序 2 页（$1.41~$16.99），")
    print("       低价段已补齐。补上之后看到的是双峰：$1.4~$13 一大簇（价格比 0.06~0.57，全在带外）")
    print("       + $16~$36 一簇（锚点在这簇里），两簇之间 $13~$16 很薄。")
    print("       仍不能说「$13~$16 这个窄缝里没有对手」——那要按价格窗专门再拉一次。")


def _tier_order(tier: str) -> int:
    return {"S": 0, "A": 1, "B": 2, "C": 3, "unknown": 4}.get(tier, 5)


def _tier_cell(row: dict) -> str:
    if row["tier"] == "unknown":
        return "待判定"
    return f"{row['tier']}{'（暂定）' if row['provisional'] else ''}"


def _row_note(row: dict) -> str:
    """一行说明。先说「缺什么」，再说「凭什么」。"""
    if row["tier"] == "unknown":
        return "可判维度 < 2，不出结论（不是不合格）"
    if row["tier"] == "A":
        # A 的两条门槛里，「月销 ≥1000」用的是卖家精灵的 BSR 幂律**估算**，不是订单级真值。
        # 一个估算值单独就能把竞品顶到最高优先级，这句话必须写在它旁边，
        # 否则面板上「A 级」看起来和「measured」一样硬。
        return "黑马（上架 90 天内 + 月销 ≥ 1000，月销为模型估算）"
    if row["provisional"]:
        # 采样太薄和压根没采到是两件事：前者查上游返回条数，后者查采集。
        # 在报告里写成同一句，会把人送去查一个没有问题的地方。
        if row.get("sample_thin"):
            got = row.get("word_count")
            return f"竞品词库抽样薄（{got} 词）→ 暂定" if got else "竞品词库抽样薄 → 暂定"
        # 缺的中文名从引擎拿，别在这里再写一份（两处措辞总有一天会分叉）
        from app.domain.tiering import missing_dim_labels

        labels = "、".join(missing_dim_labels(row["missing_dims"]))
        return f"缺 {labels} → 暂定" if labels else "证据不完备 → 暂定"
    return "通过四同法则"


# ============================================================
# 主流程
# ============================================================


def _serve(port: int) -> None:
    import uvicorn

    print(f"正在启动服务：http://127.0.0.1:{port}/panel  （Ctrl+C 停止）")
    print(f"  连的库：{_target_display()}")
    uvicorn.run("app.api.server:app", host="127.0.0.1", port=port, log_level="info")


def main() -> None:
    ap = argparse.ArgumentParser(description="真实数据 pilot：真实 ASIN 当自家产品，跑真实竞品池")
    ap.add_argument("--db", default=None, help="目标库（默认 data/pilot.db）")
    ap.add_argument("--anchor", default=DEFAULT_ANCHOR_ASIN, help="当自家产品用的真实 ASIN")
    ap.add_argument("--limit", type=int, default=0,
                    help="竞品上限（按 BSR 升序截断；0 = 不限，默认）。"
                         "非 0 时报告会打一行「另有 N 个未评估」")
    ap.add_argument("--serve", action="store_true", help="造完数据后直接起服务")
    ap.add_argument("--port", type=int, default=settings.app_port, help="起服务的端口")
    args = ap.parse_args()

    # 这一步必须在任何读写库的动作之前：它决定了这个脚本写进去的数算 Mock。
    _bind_mock_mode_to_target_db()

    anchor_asin = args.anchor.strip().upper()
    print(f"pilot 目标库：{_target_display()}")
    print(f"           绝对路径：{_path_of_sqlite_url(str(settings.database_url)).resolve().as_posix()}")

    # ① 路径守卫必须在 init_db() 之前（建表/迁移已经算动了那个库）
    _assert_writable_db()
    applied = init_db()
    if applied:
        print(f"  迁移已应用：{applied}")
    _assert_anchor_slot(anchor_asin)

    # ② 读真实返回
    items = _load_raw_items()
    print(f"  原始行 {len(items)} 条（已按 ASIN 去重）")

    anchor_item = next((i for i in items if str(i.get("asin")).upper() == anchor_asin), None)
    if anchor_item is None:
        raise SystemExit(
            f"原始返回里找不到 {anchor_asin}。可选："
            f"{'、'.join(str(i.get('asin')) for i in items[:8])} …"
        )

    # ③ 锚点
    anchor = _write_anchor(anchor_item)
    my_id = int(anchor["id"])
    cleared = _clear_pool(my_id)
    if cleared:
        print(f"  清掉上一次的 {cleared} 个竞品（幂等重跑）")

    # ④ 候选 → 判定 → 落库
    candidates = _pick_candidates(items, anchor_asin, args.limit)
    print(f"  候选 {len(candidates)} 个（同节点 / 月销 ≥ {MIN_MONTHLY_SALES} / BSR ≤ {MAX_BSR}，"
          f"变体已归并）")
    if args.limit:
        dropped = len(_pick_candidates(items, anchor_asin, 0)) - len(candidates)
        if dropped > 0:
            # 这行不是啰嗦：截断掉的候选从来没被判过，报告里也不会出现它们，
            # 所以不写出来就会被读成「它们不合格」。
            print(f"  注意：另有 {dropped} 个候选被 --limit {args.limit} 截掉，本轮**未评估**"
                  f"（按 BSR 排序后被挤出的都在带内，别读成不合格）")

    captured_at = datetime.now().replace(microsecond=0)
    with get_session() as s:
        my = s.get(MyProduct, my_id)
        if my is None:
            raise SystemExit(f"锚点 id={my_id} 读不到，写入失败")
        thresholds = settings_store.resolve_tiering_thresholds(settings.tenant_id, my_id)
        rows = [
            _write_one(my, item, thresholds=thresholds, captured_at=captured_at)[0]
            for item in candidates
        ]

    _print_report(anchor, rows, captured_at)

    print()
    print("下一步：")
    print(f"  python scripts/pilot_real.py --serve     → http://127.0.0.1:{args.port}/panel")
    # 这里打**完整 URL**，不打相对路径：SQLite 的相对路径按进程 cwd 解析，
    # 而 Git Bash 的 `$(pwd)` 给的是 `/c/Users/...`——拼成 `sqlite:////c/Users/...`
    # 之后 SQLite 会安静地建出一个空库（本轮真踩过：面板连上了，池子里 0 条）。
    # 空库不报错，只是什么都没有，所以把绝对 URL 直接给出来让人照抄。
    #
    # 两个连接串都要给：这个库的内容是 Mock，面板**必须**用 Mock 模式打开才看得到
    # 锚点判断。真实模式下打开它会看到 68 个竞品 + 一句「锚点已停用」——
    # 那是正确行为（锚点确实是反推的），但会把第一次跑这个脚本的人吓一跳。
    print("  或：  "
          f'DATABASE_URL="{settings.database_url}" '
          f'MOCK_DATABASE_URL="{settings.database_url}" '
          f"uvicorn app.api.server:app --port {args.port}")
    print("        然后在面板上切到「Mock 数据」——这个库的锚点是反推的，真实模式下会被判为不可用。")
    print("  面板上该看到的：池子里只有入池的竞品（B 暂定 / A）；锚点页会点名哪几个字段是假设值，"
          "并逐格标出「反推 / 模拟」。")

    if args.serve:
        _serve(args.port)


if __name__ == "__main__":
    main()
