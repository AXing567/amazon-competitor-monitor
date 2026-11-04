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

#: 三个维度的固定顺序。`missing_dims` 与各项文案都按这个顺序输出，
#: 免得同一份数据在两次运行里长出两种排列、让 diff 和面板看起来在抖。
DIM_ORDER: tuple[str, ...] = ("price_ratio", "keyword_overlap", "node_match")

#: 维度对外中文名。面板与人话理由共用一份，避免两处各写一套措辞
#: （历史教训：同一个「缺类目路径」在面板上前后有两种说法，用户以为是两件事）。
DIM_LABELS: dict[str, str] = {
    "price_ratio": "价格带",
    "keyword_overlap": "流量词",
    "node_match": "类目节点",
}


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

    # ---- 哪些维度压根没测出来 ----
    # 这是「结论的完备性」，不是「结论本身」。值为 None 的含义是**没测**，
    # 不是「测了、不成立」；两者在下游必须区分，否则「不知道」会变成「不合格」。
    # 无论最终判成什么（含 C 级拒收），都要记下来：缺数据是数据问题，
    # 不会因为结论恰好是 C 就消失。
    dim_values = {
        "price_ratio": res.price_ratio,
        "keyword_overlap": res.keyword_overlap,
        "node_match": res.node_match,
    }
    res.missing_dims = [dim for dim in DIM_ORDER if dim_values[dim] is None]

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
        # 两个键先占位：下面命中「暂定」时会被改写。
        # 一开始就写进 dict 的原因：这两个键要落库（`Competitor.tier_reasons`
        # 是 JSON 列），面板读的是库里的那一份，不是内存里的对象。
        # 只有部分行有这个键，会让面板的 `.get()` 长出一堆「有/没有」两种分支。
        "provisional": False,
        # `black_horse` 回答的是「这个 A 是怎么来的」：A 有两个来源——黑马，
        # 或词重合够高。没有这个键时两者在数据上完全同形，面板只能猜，
        # 而猜错的方向恰好是「A 级 · 强相关」——一句拿上架天数换来的断言。
        "black_horse": False,
        "missing_dims": list(res.missing_dims),
    }

    # ---- 硬门槛：流量池或节点，二选一必须过 ----
    #
    # 关于「可判维度」的现状：竞品流量词（`snapshots.keywords`）与自家类目路径
    #   （`MyProduct.category_path`）都已落库并接通——采集侧写、`app/migrations.py`
    #   幂等补列、`collector.row_to_unified()` 还原。所以价格 / 流量池 / 节点三维
    #   常态可判，`decidable_dims` 一般为 3。
    # 下面这段兜底与 `insufficient` 判定**不是**给缺失数据打的临时补丁：
    #   它是「将来再加维度、而新维度的数据尚未采集」时的护栏。
    #   维度少到不足以判定时，宁可不出结论（`unknown`），也不许拿缺数据当不合格——
    #   那样会把「不知道」写成「不合格」，且没有任何报错。**不要**因为当前不命中就删掉。
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
        # 这里**不**标暂定：没通过门槛就不存在「暂定某一档」，
        # 那是「不入池」。但 `missing_dims` 照样保留——缺哪几维是数据问题，
        # 不因为结论是 C 就不存在，面板要用它说明「这次拒收里有多少是缺数据」。
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

    # ---- 标注「暂定」----
    # 词重合没测出来时，`_decide_letter_tier` 会给出 "B"（理由见那里）。
    # 那个 B 必须带上「暂定」，否则它和「测过了、轻度重合」在数据上完全同形，
    # 面板只能照着读成「重合度中等」——一句我们没有任何证据支持的话。
    #
    # 黑马那条路**例外**：A 的依据是「上架 90 天内 + 月销 ≥1000」，两者都不看词维度，
    # 所以它不需要「暂定」来免责。原来一刀切成暂定，面板会说
    # 「A 级（暂定：缺词重合）」——档位是确定的，却被标成"凭缺的那一维猜的"，
    # 反过来伤了黑马这条判据的可信度。
    res.tier_reasons["black_horse"] = bool(
        res.tier == "A" and _is_new_black_horse(comp)
    )
    if res.keyword_overlap is None and not res.tier_reasons["black_horse"]:
        res.provisional = True
        res.provisional_reason = provisional_reason(res.missing_dims)

    # 允许前面出现过的「暂定」被证明是误标（例如未来补上词维度后不走那一支）
    res.tier_reasons["provisional"] = res.provisional
    res.tier_reasons["missing_dims"] = list(res.missing_dims)
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
        # 词维度**没测出来**（拿不到流量词），不是「测了、不重合」。
        # 这里仍然给中间档 B，但调用方必须把 `provisional=True` 置上，
        # 并给出「缺什么、这一档是凭什么暂定的」的理由（见 `judge_tier`）。
        #
        # 为什么是 B 而不是靠边的 S / C：
        #   S = 最高优先级，误报会让用户把注意力全押上去；
        #   C = 无关，误判会直接丢掉一个可能真是 S 的对手。
        #   B 是中间档，两个方向上都最不容易造成不可逆的后果——
        #   在最坏情况下，它只是「看着像要盯一下」，而不是「押上全部」或「丢掉」。
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


def missing_dim_labels(missing: list[str] | None) -> list[str]:
    """把英文维度键翻成中文（面板 / 人话理由共用同一套措辞）。

    未收录的键原样返回：宁可露出一个英文键，也不许悄悄丢掉一维——
    丢掉的那一维会让「缺什么」这句话变成错的。
    """
    return [DIM_LABELS.get(dim, dim) for dim in (missing or [])]


def provisional_reason(missing: list[str] | None) -> str:
    """一句话说明「为什么这一档只是暂定」，可直接上屏。

    措辞必须把「没测」和「测了不重合」分开：写成「词重合较低」会让用户
    以为我们评估过这个对手的流量池，实际连他的出单词都没拿到。
    """
    dims = [dim for dim in DIM_ORDER if dim in (missing or [])]
    lack = "、".join(missing_dim_labels(dims)) or "关键维度"
    kept = "、".join(missing_dim_labels([d for d in DIM_ORDER if d not in dims])) or "其余维度"
    return (
        f"缺{lack}（没测出来，不是测了不重合）："
        f"这一档是按{kept}暂定的，不能据此判断重合深浅"
    )


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
            lo, hi = th.price_ratio_min, th.price_ratio_max
            side = "低于下限" if res.price_ratio < lo else "高于上限"
            parts.append(
                f"价格比 {fmt_ratio_to_show_side(res.price_ratio, lo, hi)} {side}"
                f"（{lo}~{hi} 价格带）"
            )
    return "；".join(parts) or "未通过战场判定"


def fmt_ratio_to_show_side(value: float, lo: float, hi: float) -> str:
    """把价格比格式化到「方向看得出来」为止。

    两位小数不够。`15.99 / 22.99 = 0.6955`，`:.2f` 出来是 `0.70`，
    而下限也是 `0.70`——于是报告里写着

        价格比 0.70 超出 0.7~1.4 价格带

    看起来就是系统坏了。真实数据第一次跑出来就有 4 条这样的行（全是 $15.99）。

    做法：从两位小数开始，**只要四舍五入后的值和边界分不出方向，就再加一位**。
    `0.6955` → `0.70` 和下界相等 → `0.696` 看得出更小 → 用它。
    这样不必预设精度，也不会为了极端边界值把每条都写成 `0.69552`。

    带内的值直接两位小数：它没有方向要说，2 位就是全部信息。
    """
    if lo <= value <= hi:
        return f"{value:.2f}"
    for digits in (2, 3, 4):
        text = f"{value:.{digits}f}"
        shown = float(text)
        if shown < lo or shown > hi:
            return text
    return f"{value:.4f}"
