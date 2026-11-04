"""步骤 1：四同法则 + 加权词重合度 + S/A/B/C 分层。

这是全系统唯一实现分层算法的地方。Agent、调度器、Web 都调这里的函数。

**阈值不在本文件里决定**：`judge_tier(..., thresholds=TieringThresholds(...))`
从外面接收阈值。下面这些模块常量已降级为「默认值来源 + 迁移期兼容」，
运行时不再读它们（读取方变成了 `app/domain/thresholds.py` 的 dataclass 默认值）。
保留它们是为了两件事：一是 `tests/test_thresholds.py` 用它锁死默认值等价，
二是不让任何外部 `from app.domain.tiering import KEYWORD_OVERLAP_GATE` 立刻炸掉。
"""
from __future__ import annotations

from typing import Optional

from app.adapters.unified import TieringResult, UnifiedSnapshot
from app.domain.thresholds import TieringThresholds

# ---- 默认阈值常量（**降级**：运行时由 TieringThresholds 提供，见 judge_tier） ----
PRICE_RATIO_MIN = 0.7          # 价格带下限：竞品价 / 自家价
PRICE_RATIO_MAX = 1.4          # 价格带上限
KEYWORD_OVERLAP_GATE = 0.40    # 进池门槛：低于此值直接判无关
OVERLAP_S = 0.60               # S 级门槛
OVERLAP_A = 0.40               # A 级门槛
OVERLAP_B = 0.20               # B 级门槛
CORE_COVERAGE = 0.80           # 核心词池覆盖的流量比例（当前未被消费，仍要进 SPEC）


# ============================================================
# 原子算法
# ============================================================


def calc_price_ratio(my_price: Optional[float], comp_price: Optional[float]) -> Optional[float]:
    """竞品价 / 自家价。任一方缺数据返回 None（不猜）。"""
    if not my_price or not comp_price or my_price <= 0:
        return None
    return round(comp_price / my_price, 4)


def calc_keyword_overlap(
    my_core: dict[str, float],
    comp_keywords: list[dict] | set[str],
) -> Optional[float]:
    """加权出单词重合度。

    my_core:    {keyword: trafficPercentage}，已截取累计 80% 的头部词
    comp_keywords: 竞品词列表（dict）或词集合（str）

    返回 0~1 的浮点，或 None（数据不足）。
    """
    if not my_core:
        return None

    if comp_keywords and isinstance(next(iter(comp_keywords)), dict):
        comp_set = {kw["keyword"] for kw in comp_keywords if kw.get("keyword")}  # type: ignore[index]
    else:
        comp_set = {str(k) for k in comp_keywords}

    if not comp_set:
        return None

    total = sum(my_core.values())
    if total <= 0:
        return None

    hit = sum(w for kw, w in my_core.items() if kw in comp_set)
    return round(hit / total, 4)


def judge_tier(
    *,
    my_price: Optional[float],
    my_core_keywords: dict[str, float],
    my_category_path: list[str] | None,
    my_daily_units: Optional[float],
    comp: UnifiedSnapshot,
    functional_match: Optional[bool] = None,
    thresholds: TieringThresholds | None = None,
) -> TieringResult:
    """跑完整四同法则，输出分层结果与判定依据。

    functional_match 无法自动判定（涉及物理形态判断），默认 None 表示"待人工确认"，
    此时不因此拒绝入池，但在 tier_reasons 里标注。

    thresholds 不传就用默认值：调用方（pool / scheduler / 工具层）不用改一行，
    而「按老板自己的门槛判」只需要多传一个参数。
    """
    th = thresholds or TieringThresholds()
    res = TieringResult()

    # ---- 同 1：价格带 ----
    res.price_ratio = calc_price_ratio(my_price, comp.net_price or comp.price)
    price_ok = (
        res.price_ratio is not None
        and th.price_ratio_min <= res.price_ratio <= th.price_ratio_max
    )

    # ---- 同 2：流量池 ----
    res.keyword_overlap = calc_keyword_overlap(my_core_keywords, comp.keywords)
    keyword_ok = res.keyword_overlap is not None and res.keyword_overlap >= th.keyword_overlap_gate

    # ---- 同 3：功能形态（自动判不了，留给人工） ----
    res.functional_match = functional_match

    # ---- 同 4：节点 / 跨界截流 ----
    res.node_match = _check_node_match(my_category_path, comp.category_path)
    node_ok = res.node_match is True or keyword_ok  # 跨界但词重合 → 也算通过

    res.tier_reasons = {
        "price_ratio": res.price_ratio,
        "price_band_ok": price_ok,
        "keyword_overlap": res.keyword_overlap,
        "keyword_pool_ok": keyword_ok,
        "node_match": res.node_match,
        "functional_match": res.functional_match,
        "functional_note": (
            "未能自动判定功能形态，请人工确认是否为配件/互补品"
            if functional_match is None
            else None
        ),
    }

    # ---- 硬门槛：流量池或节点，二选一必须过 ----
    #
    # TODO(下一轮 · 数据模型): 补 `snapshots.keywords`(JSON) 与 `MyProduct.category_path`。
    #   缺的是什么：竞品流量词（keywords）与自家类目路径（category_path）。
    #   为什么缺：卖家精灵的关键词是慢通道（单次 5~15s、有配额），采集时虽然取到过，
    #     但没有落库；第二天 `collector.row_to_unified()` 还原快照时 keywords 就是空的，
    #     而 job_pool_maintenance 传的 my_category_path 恒为 None。
    #   补齐后：节点维度可判，decidable_dims 常态为 3，insufficient 自然不再出现。
    #   注意：下面这段兜底不要因此删除——它是「将来再加维度」时的护栏，不是临时补丁。
    res.decidable_dims = sum(
        1 for dim in (res.price_ratio, res.keyword_overlap, res.node_match) if dim is not None
    )
    # 少于 2 个维度可判定 = 结论不足以下发。
    # 为什么不按「缺的当 False」处理：那会把「不知道」写成「不合格」，
    # 而这类错误没有任何报错，只会在下游表现为整池降级。
    res.insufficient = res.decidable_dims < 2

    if not (keyword_ok or (node_ok and price_ok)):
        res.passed = False
        res.tier = "C"
        res.reject_reason = _reject_reason(price_ok, keyword_ok, res, th)
        return res

    res.passed = True
    res.tier = _decide_letter_tier(
        overlap=res.keyword_overlap,
        price_ok=price_ok,
        node_ok=node_ok,
        comp=comp,
        my_daily_units=my_daily_units,
        th=th,
    )
    return res


# ============================================================
# 内部判断
# ============================================================


def _decide_letter_tier(
    *,
    overlap: Optional[float],
    price_ok: bool,
    node_ok: bool,
    comp: UnifiedSnapshot,
    my_daily_units: Optional[float],
    th: TieringThresholds,
) -> str:
    """在通过入池门槛后，决定 S/A/B/C。"""
    # ---- A 级：新品黑马（上架 <90 天且销量规模可观） ----
    if _is_new_black_horse(comp):
        return "A"

    if overlap is None:
        return "B"

    # ---- S 级：高重合 + 价格带重叠 + 节点一致 + 体量咬合 ----
    if overlap >= th.overlap_s and price_ok and node_ok and _volume_close(comp, my_daily_units):
        return "S"

    # ---- A 级：中重度重合 ----
    if overlap >= th.overlap_a:
        return "A"

    # ---- B 级：轻度重合，相邻战场 ----
    if overlap >= th.overlap_b:
        return "B"

    return "C"


def _is_new_black_horse(comp: UnifiedSnapshot) -> bool:
    """上架 90 天内 + 月销过千 → 黑马。"""
    if not comp.release_date:
        return False
    try:
        from datetime import date, datetime

        d = datetime.strptime(comp.release_date[:10], "%Y-%m-%d").date()
        age_days = (date.today() - d).days
    except (ValueError, TypeError):
        return False
    if age_days > 90:
        return False
    return (comp.monthly_units or 0) >= 1000


def _volume_close(comp: UnifiedSnapshot, my_daily_units: Optional[float]) -> bool:
    """体量是否咬合：竞品日均销量在自家的 0.8x ~ 2.5x 之间。

    自家没填日均销量时，按"无法判断"处理，返回 True 不阻断 S 级判定，
    但调用方可从 tier_reasons 看到缺失。
    """
    if not my_daily_units or my_daily_units <= 0:
        return True
    comp_daily = comp.daily_units
    if comp_daily is None and comp.monthly_units:
        comp_daily = comp.monthly_units / 30
    if comp_daily is None:
        return True
    ratio = comp_daily / my_daily_units
    return 0.8 <= ratio <= 2.5


def _check_node_match(my_path: list[str] | None, comp_path: list[str] | None) -> Optional[bool]:
    if not my_path or not comp_path:
        return None
    # 末级叶子节点相同即视为同节点
    return my_path[-1].strip().lower() == comp_path[-1].strip().lower()


def _reject_reason(
    price_ok: bool, keyword_ok: bool, res: TieringResult, th: TieringThresholds
) -> str:
    """拒绝理由。**文案里的数字必须来自 th，不许写死**。

    写死一个 40% 的后果：老板把门槛调成 90% 后，面板上仍显示
    「词重合度 80% 低于 40% 门槛」——一句自相矛盾的废话，比没有理由更糟。
    """
    parts = []
    if res.keyword_overlap is None:
        parts.append("缺少流量词数据，无法计算重合度")
    elif not keyword_ok:
        parts.append(
            f"词重合度 {res.keyword_overlap:.1%} 低于 {th.keyword_overlap_gate:.0%} 门槛"
        )
    if not price_ok:
        if res.price_ratio is None:
            parts.append("缺少价格数据")
        else:
            parts.append(
                f"价格比 {res.price_ratio:.2f} 超出 "
                f"{th.price_ratio_min}~{th.price_ratio_max} 价格带"
            )
    return "；".join(parts) or "未通过战场判定"
