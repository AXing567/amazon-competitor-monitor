"""L4 · 求签类能力（4 个）+ 对话专用（2 个，`agent_only`）。

## 为什么求签和查询分开

查询是「把库里已有的东西拿出来」，求签是「算一个结论」。后者全部委托给 L2 的纯函数
（`domain.decision` / `services.simulator`），本层**一行判断逻辑都没有**。

这不是洁癖：`judge_clearance` 这类判断涉及十几个维度的加权，一旦在面板里复制一份
「简化版」，两份结论迟早不一致，而老板会以哪份为准？没人说得清。
所以求签类能力只做一件事：**把入参补齐成 L2 需要的形状**，然后原样透传结论。

## 为什么 `agent_chat` 是 `agent_only`

`agent_chat` 会进模型自己的工具列表吗？**绝不能**——模型调用"和模型对话"的能力，
就是递归调用自己，一次 token 预算能烧出无限循环。所以：

- 它不在 `TOOL_SCHEMAS` 里（模型看不见）；
- 面板通过 REST 调它一律 403（面板有自己的输入框，没有理由绕道能力层再进模型）。

`reset_context` 同理：清空会话历史是聊天入口自己的事，不该暴露成通用写能力。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import desc, select

from app.capabilities.types import (
    CapContext,
    Capability,
    GROUP_JUDGE,
    ParamSpec,
    SCOPE_AGENT_ONLY,
    SCOPE_SHARED,
    T_BOOL,
    T_ENUM,
    T_INT,
    T_JSON,
    T_STR,
)
from app.db import get_session
from app.domain import baseline as baseline_mod
from app.domain.decision import judge_clearance, judge_price_follow
from app.domain.thresholds import GROUPS, GROUP_TITLES, spec_items, warnings as th_warnings
from app.models import Competitor, MyProduct, Snapshot
from app.services import binding, settings_store, simulator

logger = logging.getLogger(__name__)


def _p(*args, **kwargs) -> ParamSpec:
    return ParamSpec(*args, **kwargs)


# ============================================================
# ① judge_price_follow
# ============================================================


def judge_price_follow_cap(
    ctx: CapContext,
    competitor_new_price: Optional[float] = None,
    competitor_id: Optional[int] = None,
    competitor_is_clearance: bool = False,
    market_players: int = 3,
) -> dict:
    """对手降到某个价位时我方该不该跟。

    `competitor_id` 传了就按绑定解析「对的是我哪个自家产品」——这是多产品卖家
    最容易错的地方：拿 A 产品的保本价去判 B 产品战场的价格，结论必然是错的。
    """
    my_id: Optional[int] = None
    if competitor_id is not None:
        my_id = binding.resolve_my_product(ctx.tenant_id, ctx.user_id, int(competitor_id))

    with get_session() as s:
        if my_id is not None:
            my = s.get(MyProduct, int(my_id))
        else:
            my = s.execute(
                select(MyProduct).where(MyProduct.tenant_id == ctx.tenant_id).limit(1)
            ).scalar_one_or_none()

        if my is None or not my.current_price:
            return {"ok": False, "error": "还没有设置自家售价，无法判断"}

        be_price = my.break_even_price
        if not be_price and my.target_margin_rate:
            be_price = baseline_mod.estimate_break_even_price(
                price=my.current_price, margin_rate=my.target_margin_rate
            )
        if not be_price:
            return {
                "ok": False,
                "error": "缺保本价，无法判断",
                "hint": "直接告诉我保本价，或告诉我目标毛利率我帮你算",
            }

        new_price = competitor_new_price
        if new_price is None:
            # 退化为"最近一次采集到的对手净价"：面板上常有人问"他现在的价我要不要跟"
            stmt = (
                select(Snapshot)
                .join(Competitor, Snapshot.competitor_id == Competitor.id)
                .where(Competitor.tenant_id == ctx.tenant_id)
                .order_by(desc(Snapshot.captured_at))
                .limit(1)
            )
            if competitor_id is not None:
                stmt = (
                    select(Snapshot)
                    .where(Snapshot.competitor_id == int(competitor_id))
                    .order_by(desc(Snapshot.captured_at))
                    .limit(1)
                )
            latest = s.execute(stmt).scalar_one_or_none()
            new_price = (latest.net_price or latest.price) if latest else None
        if not new_price:
            return {"ok": False, "error": "没有可用的对手价格"}

        assessment = baseline_mod.assess(my)

        res = judge_price_follow(
            competitor_new_price=float(new_price),
            my_current_price=my.current_price,
            my_break_even_price=float(be_price),
            my_unit_profit_now=my.current_price * (my.target_margin_rate or 0.2),
            my_daily_units=my.daily_sales_avg or 0,
            competitor_is_clearance=bool(competitor_is_clearance),
            market_players=int(market_players or 3),
            my_dos=assessment.days_of_supply,
        )
        data = res.to_dict()
        # 注意：必须在本会话内把 ORM 字段读出来拼进返回值。
        # 会话关闭后访问 `my.current_price` 会触发 DetachedInstanceError，
        # 而这个异常只在"有数据"时才出现——空库跑一遍是发现不了的。
        data["ok"] = True
        data["inputs"] = {
            "competitor_new_price": new_price,
            "my_current_price": my.current_price,
            "my_break_even_price": be_price,
            "my_dos": assessment.days_of_supply,
            "my_product_id": my.id,
        }
        return data


# ============================================================
# ② judge_clearance
# ============================================================


def judge_clearance_cap(
    ctx: CapContext,
    asin: str,
    ads_stopped: Optional[bool] = None,
    brand_shrinking: Optional[bool] = None,
) -> dict:
    """竞品是不是在清仓。能从历史快照自动提取的信号自动提取，提取不到的**明确标"未知"**。"""
    asin = (asin or "").strip().upper()

    with get_session() as s:
        comp = s.execute(
            select(Competitor).where(
                Competitor.tenant_id == ctx.tenant_id, Competitor.asin == asin
            )
        ).scalar_one_or_none()
        if comp is None:
            return {"ok": False, "error": f"{asin} 不在池中"}

        snaps = list(
            s.execute(
                select(Snapshot)
                .where(Snapshot.competitor_id == comp.id)
                .order_by(desc(Snapshot.captured_at))
                .limit(20)
            ).scalars()
        )

    if len(snaps) < 2:
        return {
            "ok": False,
            "error": "历史数据不足两条，无法判断趋势",
            "hint": "先观察几天，或对它跑一次 collect_now 补数据",
        }

    prices = [x.net_price or x.price for x in snaps if (x.net_price or x.price)]
    price_drop_pct = None
    if len(prices) >= 2 and prices[-1]:
        peak = max(prices)
        price_drop_pct = round((1 - prices[0] / peak) * 100, 1) if peak else None

    # 库存单调下降：用 stock_hint 序列（IN_STOCK > LOW_STOCK > OOS）
    hints = [(x.stock_hint or "").upper() for x in snaps if x.stock_hint]
    stock_declining = None
    if len(hints) >= 3:
        order = {"IN_STOCK": 2, "LOW_STOCK": 1, "OOS": 0}
        vals = [order.get(h) for h in hints if h in order]
        if len(vals) >= 3:
            stock_declining = all(vals[i] >= vals[i + 1] for i in range(len(vals) - 1)) and (
                vals[0] > vals[-1]
            )

    # Listing 停维护：标题长期不变（粗略代理指标）
    titles = {x.title for x in snaps if x.title}
    listing_frozen = len(titles) == 1 and len(snaps) >= 5

    res = judge_clearance(
        price_drop_pct=price_drop_pct,
        stock_declining=stock_declining,
        no_restock=None,          # 补货行为从公开数据看不出来
        ads_stopped=ads_stopped,
        listing_frozen=listing_frozen,
        brand_shrinking=brand_shrinking,
        historical_seasonal=None,
        deep_coupon=None,
        variants_delisted=None,
    )

    data = res.to_dict()
    data["ok"] = True
    data["auto_extracted"] = {
        "price_drop_pct": price_drop_pct,
        "stock_declining": stock_declining,
        "listing_frozen": listing_frozen,
        "snapshots_used": len(snaps),
    }
    data["unknown_signals"] = [
        "是否停止补货（公开数据看不到）",
        "广告位是否消失（需人工观察或关键词广告位数据）",
        "品牌其他 ASIN 是否同步收缩",
        "变体是否逐步下架",
    ]
    data["advice"] = (
        "清仓判断里最硬的一条是「库存只降不补」——公开数据拿不到全貌，"
        "但连续几次采集看到 LOW_STOCK 一直不回到 IN_STOCK，基本就能定性。"
    )
    return data


# ============================================================
# ③ simulate_thresholds
# ============================================================


def simulate_thresholds(
    ctx: CapContext,
    values: Optional[Any] = None,
    days: int = 7,
    asin: Optional[str] = None,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
) -> dict:
    """阈值试算（**dry-run，零写入**）。

    复用生产链路上同一套 `DiffEngine`：如果试算另写一份判定，那它算出来的
    「改完会多几条 P0」就只是在骗人。所以这里只做转发，判定全在 L2。

    ## 为什么不把 `ctx.user_id` 当默认值（QA 的 D5）

    原实现是 `uid = user_id if user_id is not None else ctx.user_id`——面板每点一次试算
    都会带上**个人层**；而定时采集走 `resolve_diff_thresholds(tenant, my_product_id)`，
    只有 company + product 两层。于是只要有人存过个人覆盖，
    「试算说 3 条、实际推了 12 条」就必然发生，而这正是 simulator 模块文档里最怕的事。

    现在：**只有显式传 `user_id` 才启用个人层**（供未来的 per-user 模拟用）；
    面板不传，于是试算与采集同口径。`ctx.user_id` 只作为 `personal_user_id` 传给 L3，
    用来告诉用户"你有 N 项个人覆盖，它不参与试算也不参与推送"——只报告，不参与计算。
    """
    if values is not None and not isinstance(values, (dict, list)):
        return {"ok": False, "error": "values 必须是对象，例如 {\"diff\": {\"price_p0_pct\": 0.05}}"}

    return simulator.simulate_diff_thresholds(
        ctx.tenant_id,
        values if isinstance(values, dict) else None,
        days=days,
        asin=(asin or "").strip().upper() or None,
        # `user_id` 只认显式参数，**绝不**兜底成 `ctx.user_id`：试算必须与生产同口径。
        # 继承 ctx.user_id 会让面板的数字系统性比生产更敏感，而用户正是拿它做决策
        # （"要不要收紧阈值"）——那次决策会基于一个不存在的触发频率。
        user_id=user_id,
        my_product_id=my_product_id,
        # ctx.user_id 只当"你有 N 项个人覆盖"的提示，不参与计算（见 docstring）
        personal_user_id=ctx.user_id,
    )


# ============================================================
# ④ explain_thresholds
# ============================================================


#: 个人层**不生效**的分组。这些组决定的是「信号/分层到底存不存在」——全局唯一的事实，
#: 一轮采集只跑一遍（100 个竞品≈25 分钟、受数据源额度约束），不可能按人各跑一套。
#: 个人差异的正确落点是 `notify`（决定「这条信号要不要推给这个人」），不是这里。
GROUP_USER_LAYER_EXCLUDED: tuple[str, ...] = ("diff", "tiering")


def explain_thresholds(
    ctx: CapContext,
    group_name: str,
    my_product_id: Optional[int] = None,
) -> dict:
    """把「当前生效阈值」翻译成一段人话，供面板空态/帮助气泡展示。

    这里没有任何判断：只是把 SPEC 的中文标签 + 生效值 + 来源层拼起来。
    之所以要有它，是因为面板上如果只显示一串数字，老板不会知道
    `price_p1_pct = 0.03` 到底意味着「降 3% 会进日报」还是「会推送飞书」。

    ## 为什么 diff / tiering 组要剔掉个人层（D5 的第二个出口）

    原来这里一律 `resolve(tenant, user_id=ctx.user_id)`，于是只要库里有一条
    `scope="user"` 的 diff 覆盖，就会输出「价格 P0 阈值：0.03（来自个人覆盖）」，
    而标题写的是「当前生效」——但 diff 组的个人层**根本不生效**（生产采集只读
    company + product）。这和 D5 是同一个谎，只是换了出口：试算已经不说谎了，
    这段人话还在说谎，而它正是面板拿来当帮助文案的。

    所以：`diff` / `tiering` 里被个人层接管的键，**回落到下一层**（company / default）
    并在 `personal_overrides_not_applied` 里单列出来，别让它们静默消失；
    `notify` 组**保留**个人层（它才是个人差异该落的地方）。
    """
    if group_name not in GROUPS:
        return {"ok": False, "error": f"group_name 只能是 {'/'.join(GROUPS)}，收到 {group_name!r}"}

    uid = ctx.user_id
    values, sources = settings_store.resolve(
        ctx.tenant_id, user_id=uid, my_product_id=my_product_id
    )
    current = dict(values.get(group_name) or {})
    source = dict(sources.get(group_name) or {})

    # ---- 剔掉不生效的个人层（只对 diff / tiering）----
    personal_overrides_not_applied: list[dict] = []
    if group_name in GROUP_USER_LAYER_EXCLUDED and uid is not None:
        base_values, base_sources = settings_store.resolve(
            ctx.tenant_id, my_product_id=my_product_id      # 故意不传 user_id
        )
        base_group = base_values.get(group_name) or {}
        base_group_sources = base_sources.get(group_name) or {}
        for key, layer in list(source.items()):
            if layer != "user":
                continue
            personal_overrides_not_applied.append({"key": key, "value": current.get(key)})
            current[key] = base_group.get(key, current.get(key))
            source[key] = base_group_sources.get(key, "default")

    lines = [f"{GROUP_TITLES.get(group_name, group_name)}（当前生效）"]
    for item in spec_items(group_name):
        value = current.get(item.key, item.default)
        layer = source.get(item.key, "default")
        unit = f" {item.unit}" if item.unit else ""
        lines.append(f"· {item.label}：{value}{unit}（来自{_layer_title(layer)}）")
        if item.help:
            lines.append(f"    {item.help}")

    notes = th_warnings(group_name, current)
    if notes:
        lines.append("")
        lines += [f"⚠ {n}" for n in notes]

    if personal_overrides_not_applied:
        labels = {item.key: item.label for item in spec_items(group_name)}
        detail = "、".join(
            f"{labels.get(o['key'], o['key'])} {o['value']}"
            for o in personal_overrides_not_applied
        )
        title = GROUP_TITLES.get(group_name, group_name)
        lines.append("")
        lines.append(
            f"⚠ 你有 {len(personal_overrides_not_applied)} 项个人覆盖（{detail}）"
            f"不参与{title}的判断——{title}对所有人生效的是同一套阈值，"
            "个人层只影响「打扰节奏」（这条信号要不要推给你）。"
            f"想让 {personal_overrides_not_applied[0]['value']} 真正生效，"
            "请改产品层或公司层。"
        )

    return {
        "ok": True,
        "group_name": group_name,
        "group_title": GROUP_TITLES.get(group_name, group_name),
        "my_product_id": my_product_id,
        "effective": current,
        "sources": source,
        # `notify` 组传了个人层就是真的生效，这里如实标出来
        "personal_layer_applied": uid is not None and group_name not in GROUP_USER_LAYER_EXCLUDED,
        "personal_overrides_not_applied": personal_overrides_not_applied,
        "warnings": notes,
        "lines": lines,
        "text": "\n".join(lines),
    }


def _layer_title(source: str) -> str:
    return {
        "company": "公司默认",
        "user": "个人覆盖",
        "product": "产品覆盖",
        "default": "系统默认",
    }.get(source, source)


# ============================================================
# ⑤ agent_chat（agent_only）
# ============================================================


def agent_chat(ctx: CapContext, message: str, session_id: str = "web") -> dict:
    """把一句话交给模型，跑完工具调用循环后返回最终答复。

    为什么用同步的 `llm.chat` 而不是 `llm.achat`：能力层是同步契约（所有 handler 都是
    `def`），REST 层再把整次调用丢进线程池。在这里 `asyncio.run` 会撞上"已有事件循环"
    而炸掉，而"能力层有些 handler 是异步的"会让上层每次调用都要判断一次能不能 await。

    为什么 `llm` 用延迟导入：`app.agent.tools` 反过来依赖本包（工具层是本包的薄适配层），
    模块级导入会形成 `capabilities → llm → tools → capabilities` 的循环。
    这个循环只在 `POST /api/cap/*` 先于 Agent 被导入时才会炸（即生产启动顺序），
    单测里反而看不出来——所以结构上就该断开，而不是靠"先 import 谁"的约定。
    """
    from app.agent import llm

    result = llm.chat(message, session_id=session_id)
    if not isinstance(result, dict):
        return {"ok": True, "reply": str(result)}
    result.setdefault("ok", True)
    result["session_id"] = session_id
    return result


# ============================================================
# ⑥ reset_context（agent_only）
# ============================================================


def reset_context(ctx: CapContext, session_id: str) -> dict:
    """清空某个会话的历史。会话历史在进程内存里，所以这是纯粹的 agent 侧能力。"""
    from app.agent import llm

    llm.reset_history(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "history_size": llm.history_size(session_id),
    }


# ============================================================
# 注册表
# ============================================================


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        name="judge_price_follow",
        scope=SCOPE_SHARED,
        group=GROUP_JUDGE,
        title="跟价判定",
        description="对手降到某价位时我方该不该跟：保本线、清仓、库存、盈亏平衡单量、玩家数五道关卡。",
        params=(
            _p("competitor_new_price", "对手新价格", "float", minimum=0, unit="美元",
               help="不传则用他当前净价"),
            _p("competitor_id", "竞品 ID", T_INT, minimum=1,
               help="传了才知道该拿你哪个自家产品来比"),
            _p("competitor_is_clearance", "已判定清仓", T_BOOL, default=False),
            _p("market_players", "战场玩家数", T_INT, default=3, minimum=1, maximum=50),
        ),
        handler=judge_price_follow_cap,
    ),
    Capability(
        name="judge_clearance",
        scope=SCOPE_SHARED,
        group=GROUP_JUDGE,
        title="清仓判定",
        description="从历史快照自动提取可得信号，缺的信号明确标「未知」，不瞎猜。",
        params=(
            _p("asin", "竞品 ASIN", T_STR, required=True),
            _p("ads_stopped", "核心词广告位消失", T_BOOL,
               help="你观察到的就传，拿不到就留空"),
            _p("brand_shrinking", "同品牌其他 ASIN 也在收缩", T_BOOL),
        ),
        handler=judge_clearance_cap,
    ),
    Capability(
        name="simulate_thresholds",
        scope=SCOPE_SHARED,
        group=GROUP_JUDGE,
        title="阈值试算",
        description="拿历史快照把候选阈值跑一遍，看 P0/P1/P2 会怎么变。dry-run，零写入。",
        params=(
            _p("values", "候选阈值", T_JSON,
               help="如 {\"diff\": {\"price_p0_pct\": 0.05}}；也接受扁平写法"),
            _p("days", "回看天数", T_INT, default=7, minimum=1, maximum=90),
            _p("asin", "只试算某个竞品", T_STR),
            _p("user_id", "按哪个用户解析当前值", T_INT, minimum=1,
               help="不传则用本次调用携带的身份"),
            _p("my_product_id", "自家产品 ID", T_INT, minimum=1),
        ),
        handler=simulate_thresholds,
    ),
    Capability(
        name="explain_thresholds",
        scope=SCOPE_SHARED,
        group=GROUP_JUDGE,
        title="阈值说明",
        description="把当前生效阈值翻译成人话，说明每个值意味着什么、来自哪一层。",
        params=(
            _p("group_name", "阈值分组", T_ENUM, required=True, enum=list(GROUPS)),
            _p("my_product_id", "自家产品 ID", T_INT, minimum=1,
               help="不传则看不到产品层覆盖"),
        ),
        handler=explain_thresholds,
    ),
    Capability(
        name="agent_chat",
        scope=SCOPE_AGENT_ONLY,
        group=GROUP_JUDGE,
        title="对话（Agent 专用）",
        description="把一句话交给模型处理。**不在 TOOL_SCHEMAS 里**，面板调用一律 403。",
        params=(
            _p("message", "用户这句话", T_STR, required=True),
            _p("session_id", "会话标识", T_STR, default="web"),
        ),
        handler=agent_chat,
    ),
    Capability(
        name="reset_context",
        scope=SCOPE_AGENT_ONLY,
        group=GROUP_JUDGE,
        title="清空会话（Agent 专用）",
        description="清空某个会话的对话历史。面板不可调用。",
        params=(
            _p("session_id", "会话标识", T_STR, required=True),
        ),
        handler=reset_context,
    ),
)
