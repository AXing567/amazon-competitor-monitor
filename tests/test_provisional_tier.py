"""「暂定档」必须在数据里存在、在面板上说出口。

## 这组用例守的是什么

`app/domain/tiering.py` 的 `_decide_letter_tier()` 里原来有这一行：

```python
if overlap is None:
    return "B"
```

`keyword_overlap is None` 的含义是**词维度根本没测出来**（拿不到流量词），
但代码直接给出 `"B"`，而 `"B"` 的对外语义是「轻度重合，相邻战场」。
于是「没测」被写成了「测了，不重合」——一句没有任何报错、面板上看起来
完全正常的假话。真实数据把这件事暴露得很彻底：卖家精灵账号的关键词模块
没有会员额度（`keywords/reverse` / `aba-reverse` / `keywords/research`
三条通道全空 + notice），`keyword_overlap` 恒为 `None`，而 `price_ratio`
与 `node_match` 都能判，于是 `decidable_dims == 2`、`insufficient is False`，
**每一个竞品都会得到一个不带任何限定的 "B"**。

这跟本项目此前修掉的两类 bug 同族（`battlefield.my_asin` 被当绑定判据、
`_effective_price` 拿标价顶替到手价）：**系统不知道自己不知道**。

## 两件必须分清的事

- `insufficient`（`decidable_dims < 2`）→ `unknown`，连入池都不该定；
- `provisional`（`decidable_dims >= 2`，但定档所需的关键维度缺了）
  → 有一个字母级，但**只是暂定**。

所以下面的用例里既断言「暂定被标出来」，也断言「暂定没有被做成 unknown」，
还断言「维度齐了不许乱标暂定」——只加不滥加。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot                      # noqa: E402
from app.capabilities import queries as queries_cap                   # noqa: E402
from app.capabilities.types import SOURCE_PANEL, CapContext           # noqa: E402
from app.db import get_session, init_db                               # noqa: E402
from app.domain.tiering import judge_tier                             # noqa: E402
from app.models import Competitor, MyProduct                          # noqa: E402
from app.services import binding, pool                                # noqa: E402

#: 自家锚点（三条判据的右侧）
MY_PRICE = 29.99
MY_PATH = ["Toys & Games", "Toy Vehicles", "Play Trucks"]
MY_CORE = {"toy car": 0.4, "diecast car": 0.35}


def _comp_without_keywords(asin: str = "B0PROV0001", **overrides) -> UnifiedSnapshot:
    """竞品快照：价格有、类目路径有、**流量词没有**（关键词模块无额度）。

    这正好是真实账号拿到的那份数据形状，也是原来会静默产出 "B" 的形状。
    """
    base: dict = {
        "asin": asin,
        "price": 26.0,
        "category_path": list(MY_PATH),
        "keywords": [],
    }
    base.update(overrides)
    return UnifiedSnapshot(**base)


def _judge_provisional(asin: str = "B0PROV0001"):
    """跑一次「词重合测不出来」的分层判定（价格带 + 节点可判）。"""
    return judge_tier(
        my_price=MY_PRICE,
        my_core_keywords=dict(MY_CORE),
        my_category_path=list(MY_PATH),
        my_daily_units=15,
        comp=_comp_without_keywords(asin),
    )


def _judge_full(asin: str = "B0PROV0002"):
    """跑一次三维齐全的分层判定（词重合有值）。"""
    return judge_tier(
        my_price=MY_PRICE,
        my_core_keywords=dict(MY_CORE),
        my_category_path=list(MY_PATH),
        my_daily_units=15,
        comp=_comp_without_keywords(
            asin,
            keywords=[
                {"keyword": "toy car", "traffic_share": 0.5},
                {"keyword": "diecast car", "traffic_share": 0.3},
            ],
        ),
    )


# ============================================================
# ① 反例驱动：词维度没测 → 仍然是 B，但必须是「暂定 B」
# ============================================================


def test_missing_keyword_module_yields_provisional_b_not_silent_b():
    """词重合算不出来时，`B` 必须带「暂定」，且不许被降成 `unknown`。

    这条是整轮改动的靶心：`decidable_dims == 2` 说明证据够下一个字母级结论，
    但「词重合」这一维压根没测——两件事同时成立，所以结论是「暂定 B」。
    原来这里会得到一个和「测过了、轻度重合」完全无法区分的 `"B"`。
    """
    res = _judge_provisional()

    # 前提：这一份数据确实是「词没测」而不是「词测了不重合」
    assert res.keyword_overlap is None, "本用例的前提是词重合算不出来"
    assert res.price_ratio is not None and res.node_match is True
    assert res.decidable_dims == 2

    # 结论：有字母级，但只是暂定
    assert res.tier == "B"
    assert res.provisional is True
    assert res.missing_dims == ["keyword_overlap"]
    assert res.provisional_reason, "暂定必须有一句能直接上屏的人话"

    # 暂定 ≠ 证据不足：不许把「暂定」误做成「待判定」
    assert res.insufficient is False
    assert res.effective_tier == "B"


def test_provisional_reason_distinguishes_not_measured_from_not_overlapping():
    """措辞必须说「没测」，不许说成「重合较低」——那是另一件事。

    把「没测」写成「重合低」，用户会以为系统评估过这个对手的流量池；
    实际我们连他的出单词都没拿到。
    """
    reason = _judge_provisional().provisional_reason or ""
    assert reason
    assert "暂定" in reason, reason
    assert "流量词" in reason, reason
    assert ("未测" in reason) or ("没测" in reason), f"必须点明是没测：{reason}"
    for lie in ("重合较低", "重合度低", "重合偏低", "重合一般"):
        assert lie not in reason, f"措辞把「没测」说成了「测了偏低」：{reason}"


def test_provisional_flags_are_written_into_tier_reasons():
    """防回退：`overlap is None` 那一支不许再是「静默 return」。

    这里不去做源码字符串匹配，而是断言**行为**：该场景下 `tier_reasons`
    里必须出现 `provisional`。它是 JSON 列，落库后面板才能读到——
    只在内存里存在的标记，等于没标。
    """
    res = _judge_provisional()
    reasons = res.tier_reasons
    assert reasons.get("provisional") is True, reasons
    assert reasons.get("missing_dims") == ["keyword_overlap"], reasons


# ============================================================
# ② 不许过度标记：三维齐了，就不许打「暂定」
# ============================================================


def test_full_dimensions_are_not_marked_provisional():
    """三个维度都测出来了，`provisional` 必须是 False、`missing_dims` 必须空。

    只加不滥加：如果到处都标「暂定」，这个标记就退化成噪音，
    用户会连真正暂定的那条一起忽略掉。
    """
    res = _judge_full()
    assert res.keyword_overlap is not None
    assert res.decidable_dims == 3
    assert res.provisional is False
    assert res.missing_dims == []
    assert res.provisional_reason is None
    assert res.tier_reasons.get("provisional") is False
    assert res.tier_reasons.get("missing_dims") == []


def test_insufficient_is_not_relabelled_as_provisional():
    """证据不足仍然是 `unknown`，不许改用「暂定」这个词盖过去。

    「暂定」的前提是有字母级结论；连维度都不够时，`unknown` 才是实话。
    """
    res = judge_tier(
        my_price=MY_PRICE,
        my_core_keywords=dict(MY_CORE),
        my_category_path=None,
        my_daily_units=15,
        comp=UnifiedSnapshot(asin="B0PROV0009", price=26.0),
    )
    assert res.insufficient is True
    assert res.effective_tier == "unknown"
    assert res.provisional is False, "证据不足不该被标成暂定"
    assert res.missing_dims == ["keyword_overlap", "node_match"]
    assert "待判定" in queries_cap._tier_label(res.effective_tier, res.tier_reasons)


# ============================================================
# ③ 面板文案：tier_label / tier_missing
# ============================================================


def test_tier_label_says_provisional_only_when_provisional():
    """暂定档的标签带「暂定」，正常档的标签**不许**带。"""
    prov = _judge_provisional()
    label = queries_cap._tier_label(prov.effective_tier, prov.tier_reasons)
    assert "暂定" in label, label
    assert prov.effective_tier in label, f"暂定档也要让用户看到字母级：{label}"

    full = _judge_full()
    normal = queries_cap._tier_label(full.effective_tier, full.tier_reasons)
    assert "暂定" not in normal, normal


def test_provisional_label_drops_relatedness_wording():
    """暂定档**不许**说「强相关 / 弱相关」。

    那两个词描述的是词重合这一维，而词重合正是缺的那一维。
    真实出现过的坏样子（pilot 里第一个 A 级）：
    `A 级 · 强相关（暂定：缺词重合）`——同一句话里既断言「强相关」，
    又说「不知道相不相关」；读的人只会记住前半句。
    """
    prov = _judge_provisional()
    label = queries_cap._tier_label(prov.effective_tier, prov.tier_reasons)
    assert "强相关" not in label, label
    assert "弱相关" not in label, label
    assert prov.effective_tier in label and "暂定" in label, label


def test_tier_label_stays_backward_compatible_for_old_callers():
    """`_tier_label(tier)` 的老调用方式必须照旧可用（现网 3 个调用点）。

    新参数只能带默认值，不许把老调用点逼成必传。
    """
    assert queries_cap._tier_label("B") == queries_cap.TIER_LABELS["B"]
    assert "暂定" not in queries_cap._tier_label("B")
    assert queries_cap._tier_label("unknown") == queries_cap.TIER_LABELS["unknown"]


def test_tier_missing_lists_missing_dims_when_provisional():
    """`tier_missing` 那一栏在暂定时也要列出缺的维度（原来只在 unknown 时填）。"""
    prov = _judge_provisional()
    row = {
        "asin": "B0PROV0001",
        "tier": "B",
        "price_ratio": prov.price_ratio,
        "keyword_overlap": None,
        "node_match": prov.node_match,
        "tier_reasons": prov.tier_reasons,
    }
    missing = queries_cap._missing_dims(row)
    assert missing, "暂定时必须说清缺哪一维，否则面板只有一个字母级"
    assert any("流量词" in m for m in missing), missing


# ============================================================
# ④ 面板数据链路：从库里读出来，暂定信息还在
# ============================================================


def test_provisional_survives_json_column_roundtrip():
    """`tier_reasons` 是 JSON 列：落库再读回来，`True` 不许变成 `"true"`。

    面板读的是**库里的那一份**，不是内存里的对象。类型一坏，
    `if reasons.get("provisional")` 就成了永远为真的字符串判断
    （`"false"` 在 Python 里也是真值），整个标记会反过来骗人。
    """
    tenant = "t-prov-json"
    init_db()
    seed = pool.update_baseline(
        tenant, asin="B0PROVMINE", current_price=MY_PRICE, break_even_price=18.0
    )
    assert seed.get("ok", True), seed

    prov = _judge_provisional("B0PROVJSON")
    with get_session() as s:
        my_id = s.query(MyProduct).filter(MyProduct.tenant_id == tenant).one().id
        comp = Competitor(
            tenant_id=tenant,
            asin="B0PROVJSON",
            my_product_id=my_id,
            status="active",
            tier=prov.effective_tier,
            tier_reasons=prov.tier_reasons,
        )
        s.add(comp)
        s.flush()
        comp_id = comp.id

    with get_session() as s:
        reasons = dict(s.get(Competitor, comp_id).tier_reasons or {})

    assert reasons.get("provisional") is True
    assert isinstance(reasons.get("provisional"), bool)
    assert reasons.get("missing_dims") == ["keyword_overlap"]
    assert isinstance(reasons.get("missing_dims"), list)


def test_list_competitors_exposes_provisional_label_and_missing_dims():
    """端到端：`list_competitors` 给的 `tier_label` / `tier_missing` 必须说出口。

    这一层是面板真正读的东西。如果暂定信息只活在 `judge_tier` 的返回对象里，
    面板永远看不见它——那和没标是一样的。
    """
    tenant = "t-prov-panel"
    init_db()
    seed = pool.update_baseline(
        tenant, asin="B0PROVMINE2", current_price=MY_PRICE, break_even_price=18.0
    )
    assert seed.get("ok", True), seed

    prov = _judge_provisional("B0PROVPANEL")
    with get_session() as s:
        my_id = s.query(MyProduct).filter(MyProduct.tenant_id == tenant).one().id
        comp = Competitor(
            tenant_id=tenant,
            asin="B0PROVPANEL",
            my_product_id=my_id,
            status="active",
            tier=prov.effective_tier,
            tier_reasons=prov.tier_reasons,
            price_ratio=prov.price_ratio,
            keyword_overlap=prov.keyword_overlap,
            node_match=prov.node_match,
        )
        s.add(comp)
        s.flush()

    ctx = CapContext(tenant_id=tenant, user_id=1, source=SOURCE_PANEL)
    listed = queries_cap.list_competitors(ctx)
    assert listed["ok"] is True, listed
    item = next(it for it in listed["items"] if it["asin"] == "B0PROVPANEL")

    assert item["tier"] == "B", "暂定档仍然要作为一个字母级出现在池子里"
    assert "暂定" in item["tier_label"], item["tier_label"]
    assert item["tier_provisional"] is True
    assert item["tier_missing"], "暂定的那一栏必须列出缺什么"
    assert any("流量词" in m for m in item["tier_missing"]), item["tier_missing"]


def test_list_competitors_does_not_mark_full_dimension_competitor():
    """三维齐全的竞品在面板上不许出现「暂定」字样。"""
    tenant = "t-prov-panel-full"
    init_db()
    seed = pool.update_baseline(
        tenant, asin="B0PROVMINE3", current_price=MY_PRICE, break_even_price=18.0
    )
    assert seed.get("ok", True), seed

    full = _judge_full("B0PROVFULL")
    with get_session() as s:
        my_id = s.query(MyProduct).filter(MyProduct.tenant_id == tenant).one().id
        comp = Competitor(
            tenant_id=tenant,
            asin="B0PROVFULL",
            my_product_id=my_id,
            status="active",
            tier=full.effective_tier,
            tier_reasons=full.tier_reasons,
            price_ratio=full.price_ratio,
            keyword_overlap=full.keyword_overlap,
            node_match=full.node_match,
        )
        s.add(comp)
        s.flush()

    ctx = CapContext(tenant_id=tenant, user_id=1, source=SOURCE_PANEL)
    listed = queries_cap.list_competitors(ctx)
    item = next(it for it in listed["items"] if it["asin"] == "B0PROVFULL")
    assert "暂定" not in item["tier_label"], item["tier_label"]
    assert item["tier_provisional"] is False
    assert item["tier_missing"] == [], "正常结论不需要「缺什么」"


# ============================================================
# ⑤ basis_summary：一句人话，不许让人以为测过
# ============================================================


def test_basis_summary_says_not_measured_instead_of_overlap_percentage():
    """暂定时 `basis_summary` 必须说「未测」，且不许出现「加权词重合 x%」。

    这句人话落在 `CompetitorBinding.basis_summary`，面板列表直接显示。
    原来它写的是「缺少流量词数据，重合度未计算」——虽然没说错，
    但结论头部仍然是「判为 B 级」，读起来依旧像是评估过的。
    """
    prov = _judge_provisional()
    summary = binding.build_basis_summary(prov.tier_reasons, tier=prov.effective_tier)

    assert "暂定" in summary, summary
    assert "未测" in summary, summary
    assert "加权词重合" not in summary, f"会让人以为测过了：{summary}"
    assert "重合度 0%" not in summary, summary
    # 其余两维的真实依据不该被丢掉
    assert "价格比" in summary
    assert "节点" in summary


def test_basis_summary_has_no_provisional_wording_when_fully_measured():
    """三维齐全时，这句话不许出现「暂定」。"""
    full = _judge_full()
    summary = binding.build_basis_summary(full.tier_reasons, tier=full.effective_tier)
    assert "暂定" not in summary, summary
    assert "加权词重合" in summary, summary


def test_basis_summary_keeps_unknown_wording_for_insufficient():
    """`unknown` 的措辞不许被本轮的「暂定」改掉：待判定 ≠ 暂定。"""
    res = judge_tier(
        my_price=MY_PRICE,
        my_core_keywords=dict(MY_CORE),
        my_category_path=None,
        my_daily_units=15,
        comp=UnifiedSnapshot(asin="B0PROV0010", price=26.0),
    )
    summary = binding.build_basis_summary(res.tier_reasons, tier=res.effective_tier)
    assert "待判定" in summary, summary
    assert "暂定" not in summary, summary


# ============================================================
# ⑥ 阈值仍然生效：暂定档也按老板的门槛算
# ============================================================


def test_provisional_tier_still_respects_thresholds():
    """暂定不等于「阈值失效」：价格带要按传入的阈值判。

    把价格带收窄到 [0.95, 1.05] 后，26.0/29.99≈0.87 就出界了，
    此时两个可判维度都不通过 → 不该再入池，更不该给暂定 B。
    """
    from app.domain.thresholds import TieringThresholds

    res = judge_tier(
        my_price=MY_PRICE,
        my_core_keywords=dict(MY_CORE),
        my_category_path=list(MY_PATH),
        my_daily_units=15,
        comp=_comp_without_keywords("B0PROV0011"),
        thresholds=TieringThresholds(price_ratio_min=0.95, price_ratio_max=1.05),
    )
    assert res.price_ratio is not None and res.price_ratio < 0.95
    assert res.passed is False
    assert res.tier == "C"
    assert res.provisional is False, "没通过门槛就不是「暂定某一档」，是不入池"
    # 但缺失维度照样要记录：它是数据问题，不因结论是 C 而消失
    assert res.missing_dims == ["keyword_overlap"]
    assert res.reject_reason


# ============================================================
# ⑦ 面板渲染：三层口径不许混成两层
# ============================================================


def test_pool_view_splits_provisional_from_unknown():
    """竞品池页的缺失说明必须分三层：暂定 / 待判定 / 不显示。

    混成两层的后果已经出现过一次：一个「B 级 · 弱相关（暂定：缺词重合）」的竞品，
    下面却挂着一句「待判定是因为缺证据：缺流量词…」——同一行里两种矛盾的口径，
    用户会以为分层没做出来。这属于「系统说的话自相矛盾」，和本轮修掉的那族 bug 同源。

    判据必须是 API 给的 `tier_provisional` 布尔位，**不许**前端去解析
    `tier_label` 里的「（暂定：…）」括号：文案一改，解析就静默失效，
    而失效的表现恰好就是把两层混回去。
    """
    from app.web.views import pool as pool_view

    src = pool_view.js()

    # 1) 暂定档单独一支，且说的是「有字母档、只是暂定」
    assert "it.tier_provisional === true" in src, "池子页没有按 tier_provisional 分支"
    assert "· 暂定：缺的是" in src, "暂定档没有自己的说法"
    assert "最能兜住的中间档" in src, "暂定档要说明这一档是怎么给的"

    # 2) 待判定那一支还在，且只属于 unknown
    assert "it.tier === 'unknown'" in src
    assert "待判定是因为缺证据：" in src
    # 暂定必须排在 unknown 之前判定：顺序反了就先命中「待判定」，暂定永远轮不到
    assert src.index("it.tier_provisional === true") < src.index("待判定是因为缺证据：")

    # 3) 防回退：不许解析标签里的括号
    for parse in ("tier_label.indexOf", "tier_label.includes", "tier_label.match",
                  "label.indexOf", "label.includes"):
        assert parse not in src, f"前端开始解析标签文案（{parse}）——文案一改就静默失效"


def test_board_view_says_provisional_not_pending():
    """看板页同一件事的措辞不许分叉：暂定行说「暂定，缺：」，其余行说「缺：」。

    两处分开写是刻意的（一个是主屏、一个是池子管理），但**事实只有一个**：
    暂定行已经有字母档，只是定档所需的维度没测全，不能读成「待判定」。
    判据同样是 `tier_provisional` 布尔位；缺的那一维也走同一套短名
    （「流量词重合」），否则它在同一个格子里会读成「暂定，缺：缺流量词（…）」。
    """
    from app.web.views import board as board_view

    src = board_view.js()

    assert "'暂定，缺：' : '缺：'" in src, (
        "看板页没有区分「暂定」与「缺」——暂定行会读成「缺：缺流量词（…）」，"
        "且看不出这一档只是暂定的"
    )
    assert "it.tier_provisional === true" in src, "判据必须是 tier_provisional 布尔位"
    # 短名映射与池子页同一套词；unknown 行的长说明（带"为什么缺"）必须保留兜底
    assert "keyword_overlap: '流量词重合'" in src
    assert "(it.tier_missing || []).join('、')" in src
    for parse in ("tier_label.indexOf", "tier_label.includes", "tier_label.match"):
        assert parse not in src, f"看板页开始解析标签文案（{parse}）"



# ============================================================
# ④ 黑马档：A 的另一个来源，不能借「暂定」免责，也不能借「强相关」拔高
#
# 真实数据里第一次出现 A 级就是这个形状：上架 30 天、月销 3000、没有流量词。
# 修 `provisional` 之前，面板显示「A 级 · 强相关（暂定：缺词重合）」——
# 一句话里同时说「高度相关」和「不知道相不相关」。
# ============================================================


def _comp_black_horse(asin: str = "B0HORSE0001") -> UnifiedSnapshot:
    """黑马形状：上架 30 天、月销 3000、同样拿不到流量词。"""
    from datetime import date, timedelta

    return UnifiedSnapshot(
        asin=asin,
        price=24.99,
        category_path=list(MY_PATH),
        keywords=[],
        monthly_units=3000,
        release_date=(date.today() - timedelta(days=30)).isoformat(),
    )


def test_black_horse_is_not_marked_provisional():
    """黑马的 A 是确定的：依据是上架天数 + 月销，两者都不看词维度。

    标成「暂定」会反过来伤它——档位明明有依据，却被说成"凭缺的那一维猜的"。
    """
    res = judge_tier(
        my_price=MY_PRICE,
        my_core_keywords=dict(MY_CORE),
        my_category_path=list(MY_PATH),
        my_daily_units=120,
        comp=_comp_black_horse(),
    )
    assert res.tier == "A", res.tier
    assert res.tier_reasons["black_horse"] is True
    assert res.provisional is False, "黑马不该被标暂定"


def test_black_horse_label_says_black_horse_not_strong_related():
    """标签要说清 A 是哪来的；「强相关」只能用于词重合够高的那个 A。"""
    reasons = {"black_horse": True, "provisional": False}
    label = queries_cap._tier_label("A", reasons)
    assert "黑马" in label, label
    assert "强相关" not in label, label

    # 非黑马、无暂定的 A 仍是原来的措辞（不许把这句一起改掉）
    assert queries_cap._tier_label("A", {"black_horse": False}) == queries_cap.TIER_LABELS["A"]


def test_black_horse_basis_summary_says_why():
    """人话理由的头部要说「新品黑马」+ 依据，别把读者引到「词重合很高」上去。"""
    summary = binding.build_basis_summary(
        {"black_horse": True, "provisional": False, "price_ratio": 0.83,
         "price_band_ok": True, "keyword_overlap": None, "node_match": True},
        tier="A",
    )
    assert "新品黑马" in summary, summary
    assert "上架 90 天内" in summary, summary


def test_can_tier_true_without_keywords_when_node_path_present():
    """锚点没有核心词、但有类目路径时，分层是**能跑**的（价格 + 节点 = 2 维）。

    原来 `can_tier = has_price and has_kw`，于是体检说「分层跑不了」，
    而同一个库里 17 个竞品都分好了层——体检与事实自相矛盾。
    """
    from types import SimpleNamespace

    from app.domain import baseline as baseline_mod

    anchor = SimpleNamespace(
        asin="B0MINE0001",
        current_price=22.99,
        break_even_price=10.58,
        target_margin_rate=0.25,
        core_keywords=None,
        category_path=["Home & Kitchen", "Dish Racks"],
        daily_sales_avg=111.5,
        fba_available_units=None,
        lead_time_days=35,
        sub_category_rank=None,
        rating=4.8,
        review_count=5335,
        title="x",
        image_url=None,
        bullet_points=None,
        assumed_fields=[],
    )
    res = baseline_mod.assess(anchor)
    assert res.can_tier is True
    # 缺词这条告警要跟着改准确：现在只剩「价格带 + 类目节点」两项，而不是「只按价格带」
    kw_warn = [w for w in res.warnings if "缺核心出单词" in w]
    assert kw_warn and "类目节点" in kw_warn[0], kw_warn
