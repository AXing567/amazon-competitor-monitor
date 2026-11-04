"""ORM 模型。对应《系统架构设计》第八章数据模型。

单租户自用，但仍保留 tenant_id 字段——后期切多租户不需要改表结构。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, now


# ============================================================
# 租户与渠道绑定
# ============================================================


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ChannelBinding(Base):
    __tablename__ = "channel_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32))          # feishu / web
    platform_user_id: Mapped[str] = mapped_column(String(128))
    # 推送级别：p0_only / daily / all
    notify_level: Mapped[str] = mapped_column(String(16), default="daily")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ============================================================
# 步骤 0：自家锚点
# ============================================================


class MyProduct(Base):
    """自家产品 + 五大维度锚点。"""

    __tablename__ = "my_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str] = mapped_column(String(16), default="amazon")
    marketplace: Mapped[str] = mapped_column(String(8), default="US")
    asin: Mapped[str] = mapped_column(String(16), index=True)

    # --- 1. 供给锚点 ---
    fba_available_units: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    daily_sales_avg: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    lead_time_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- 2. 财务锚点 ---
    current_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    break_even_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    target_margin_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- 3. 排位与口碑 ---
    sub_category_rank: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # --- 4. 核心出单词 {keyword: trafficPercentage} ---
    core_keywords: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # --- 4b. 自家类目路径 ["Toys & Games", "Toy Vehicles", "Play Trucks"] ---
    # 分层判断的「节点」维度只认这个：竞品和自家落在同一节点下，才算同一个战场。
    # 没有它，`node_match` 恒为 None，交叉品类判断就只能靠价格和词重合硬猜。
    category_path: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # --- 5. Listing 基线 ---
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bullet_points: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # --- 6. 溯源：这些数是从哪来的 ---
    # 锚点里的字段**信誉不一样**：`current_price` / `rating` / `review_count` /
    # `sub_category_rank` 可以是查来的真实公开数据；`break_even_price` /
    # `fba_available_units` / `lead_time_days` 是卖家自己的账，任何外部数据源都给不出，
    # 只能先填假设值。两者混在同一张表里、面板上一视同仁地显示，一个月后没人分得清
    # 哪个数是查来的、哪个是拍的——而「由拍出来的保本价推导出的提价建议」
    # 会看起来和真的一样。
    #
    # `source`：整个锚点的来源档位。manual / sellersprite_proxy / sp_api。
    # pilot 期没有 SP-API，用 `sellersprite_proxy` 明确标出「这是拿别人家的公开数据冒充自家」。
    source: Mapped[str] = mapped_column(String(24), default="manual")

    # `assumed_fields`：哪几个字段是**假设值**而不是查到的值（字段名列表，
    # 如 ["break_even_price", "fba_available_units"]）。
    # 为什么不靠 `source` 一栏包打天下：同一个锚点里 price 是真的、break_even 是假的，
    # 单一 source 会把两者混为一谈，而它们承载的结论完全不是一个可信度。
    # 刻意只到这一粒度：不做逐字段血缘表——pilot 要的是「这句话能不能拿去决策」，
    # 不是一套审计系统。
    assumed_fields: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    competitors: Mapped[list["Competitor"]] = relationship(back_populates="my_product")

    @property
    def days_of_supply(self) -> Optional[float]:
        """可售天数 DOS。数据不全时返回 None，绝不瞎算。"""
        if not self.fba_available_units or not self.daily_sales_avg:
            return None
        return self.fba_available_units / max(self.daily_sales_avg, 1)

    @property
    def inventory_status(self) -> str:
        """库存状态：alert / normal / surplus / unknown。"""
        dos = self.days_of_supply
        if dos is None:
            return "unknown"
        if dos < 20:
            return "alert"
        if dos <= 60:
            return "normal"
        return "surplus"


# ============================================================
# 步骤 1：竞品池
# ============================================================


class Competitor(Base):
    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    my_product_id: Mapped[int] = mapped_column(ForeignKey("my_products.id"), index=True)
    platform: Mapped[str] = mapped_column(String(16), default="amazon")
    marketplace: Mapped[str] = mapped_column(String(8), default="US")
    asin: Mapped[str] = mapped_column(String(16), index=True)

    # --- 分层 ---
    # S/A/B/C/unknown。`unknown` = 待判定（证据不足：流量词或类目路径缺失），不是不合格。
    # 列宽从 String(2) 放宽到 String(8) 只为容纳 unknown：
    # SQLite 不强制长度，老库无需迁移；新库直接建成 VARCHAR(8)，将来迁 PG 不会截断。
    tier: Mapped[str] = mapped_column(String(8), default="C")
    tier_reasons: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    tier_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # --- 四同法则判定结果 ---
    price_ratio: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    keyword_overlap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    functional_match: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    node_match: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # --- 可观测价格底线 ---
    all_time_low_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_coupon_depth: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    deal_frequency: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    follow_price_stop: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # --- 基础画像（便于列表展示，不必每次查快照） ---
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    brand: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # --- 生命周期 ---
    status: Mapped[str] = mapped_column(String(16), default="active")  # active/observing/archived
    added_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    my_product: Mapped["MyProduct"] = relationship(back_populates="competitors")
    # `order_by` 不是美观问题，是语义问题：调用方用 `snapshots[-1]` 取「最新快照」
    # （见 scheduler.job_pool_maintenance），而没有 order_by 时 `[-1]` 拿到的是
    # **数据库返回顺序**的最后一条——它今天恰好等于时间顺序，只因为采集是顺序插入、
    # 主键自增。补采历史、CSV 回填、修数据缺口这三种写入都会打破这个巧合，
    # 之后周维护就会拿过期快照重算整池，且全程无报错。
    #
    # 次键 `Snapshot.id` 不能省：同一天重采会产生两条 `captured_at` 相同的快照，
    # 只按时间排序时 `[-1]` 在两者间不确定——表现为「同一份数据跑两次结论不同」。
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="competitor",
        order_by="(Snapshot.captured_at, Snapshot.id)",
    )


# ============================================================
# 步骤 2：快照与信号
# ============================================================


class Snapshot(Base):
    """竞品采集快照。所有字段允许为空——数据源没有就留空，绝不编造。"""

    __tablename__ = "snapshots"
    __table_args__ = (
        Index("ix_snapshot_competitor_time", "competitor_id", "captured_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    # 价格
    price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    coupon_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    net_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    deal_type: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # 排名与销量
    rank_main: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rank_sub: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rank_label: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    monthly_units: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    monthly_revenue: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # 口碑
    rating: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    review_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 供给
    in_stock: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    stock_hint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    buybox_owner: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    # 内容
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # 上架日期，ISO 字符串 `YYYY-MM-DD`。
    # 为什么用 String(10) 而不是 Date：源头（卖家精灵选品库 / SP-API）给的本来就
    # 是字符串，`tiering._is_new_black_horse()` 也只做 `[:10]` 解析。转成
    # `datetime.date` 会多出一条「解析失败怎么办」的静默分支——而这个字段的失败
    # 模式恰好是**安静地不命中 A 级黑马**（判据见 tiering 的 `_is_new_black_horse`），
    # 没有任何报错。列宽 10 只为装下 `YYYY-MM-DD`。
    # 不写这一列的时候，「采完立刻判」是对的、**从库里重算就全错**：
    # 周维护、面板改阈值后重算都走重算那条路。
    release_date: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    # 类目路径：["Toys & Games", "Toy Vehicles", "Play Trucks"]
    # 为什么要落库：分层判断的「节点」维度靠它。不存的话每次重算都要重新采一遍，
    # 而它一天只变一次——为同一个值反复消耗采集额度是最容易忽略的浪费。
    category_path: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # 流量词：[{keyword, search_volume, organic_rank, sponsored_rank, traffic_share}]
    # 为什么要落库：`judge_tier` 的「流量池」维度靠它算加权词重合度。
    # 这是慢通道数据（单次 5~15s、有配额），采到不存 = 每轮都白采一次。
    # 存**原始列表**而不是算好的权重：权重算法（`core_keyword_weights` 的 coverage）
    # 将来会调，存结果就没法回溯重算了。
    keywords: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

    # 元信息
    data_quality: Mapped[str] = mapped_column(String(16), default="partial")
    source: Mapped[str] = mapped_column(String(32), default="sellersprite")

    competitor: Mapped["Competitor"] = relationship(back_populates="snapshots")


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signal_level_time", "level", "detected_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)

    category: Mapped[str] = mapped_column(String(16))  # price/supply/content/reputation
    signal_type: Mapped[str] = mapped_column(String(32))  # price_drop / out_of_stock / ...
    level: Mapped[str] = mapped_column(String(4), default="P2")  # P0/P1/P2

    before_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    after_value: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    change_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")

    detected_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    pushed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ============================================================
# 步骤 3：决策
# ============================================================


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[Optional[int]] = mapped_column(ForeignKey("signals.id"), nullable=True)
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"), index=True)

    # attack / defend / learn / no_action
    decision: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    suggested_actions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    verify_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# ============================================================
# 步骤 5：动作与验证
# ============================================================


class Action(Base):
    __tablename__ = "actions"
    __table_args__ = (
        Index("ix_action_verify_due", "verify_due_at"),
        Index("ix_action_user_time", "user_id", "executed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    my_product_id: Mapped[int] = mapped_column(ForeignKey("my_products.id"))
    decision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("decisions.id"), nullable=True)

    # ---- 本轮新增的 5 列（老库由 app/migrations.py 以 ADD COLUMN 补齐）----
    # 刻意不写 ForeignKey：SQLite 的 ALTER TABLE ADD COLUMN 对带外键约束的列行为受限，
    # 且迁移必须能在「列已存在」时安全跳过。约束放应用层（写前 session.get() 确认对象存在）。
    # 看到这里没有 REFERENCES 时不要当成疏漏「修」掉 —— 见 app/migrations.py 模块文档。
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)        # 谁做的
    competitor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # 针对哪个竞品
    signal_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)      # 依据哪条信号
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)            # 人话依据
    source: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)      # panel/feishu_card/agent/system

    action_type: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text, default="")

    executed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    baseline_metrics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    verify_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    result_metrics: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # effective / ineffective / inconclusive
    verdict: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)


# ============================================================
# 配置
# ============================================================


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    rule_type: Mapped[str] = mapped_column(String(32))
    threshold: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class SeasonCalendar(Base):
    __tablename__ = "season_calendars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64))
    # warmup / blast / cooldown
    phase: Mapped[str] = mapped_column(String(16))
    start_md: Mapped[str] = mapped_column(String(8))   # "11-20"
    end_md: Mapped[str] = mapped_column(String(8))     # "12-20"
    interval: Mapped[str] = mapped_column(String(16))  # "4h"


# ============================================================
# 用户与平台身份（本轮新增：最小实现，不做登录系统）
#
# 为什么不做登录：单机内网自用，加一套登录（密码、会话、过期、找回）
# 的维护成本远超收益。身份只在两处需要——「动作是谁录的」和
# 「飞书这条消息是谁发的」——用 open_id 映射 + 面板选择器就够了。
# ============================================================


class User(Base):
    """用。`role` 只区分 owner / member：owner 是默认操作人，member 只能看和记录动作。"""

    __tablename__ = "users"
    __table_args__ = (Index("ix_users_tenant", "tenant_id", "active"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), default="member")   # owner / member
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    identities: Mapped[list["UserIdentity"]] = relationship(back_populates="user")


class UserIdentity(Base):
    """平台身份映射：飞书 open_id / 企微 userid / 钉钉工号 / web。

    刻意**不加** `tenant_id`：它天然属于某个用户，租户可由
    `user_id → users.tenant_id` 反查。多存一份就会出现「身份表说 A 租户、
    用户表说 B 租户」的双真相，而这类不一致没人会去校。
    """

    __tablename__ = "user_identities"
    __table_args__ = (
        Index("ux_identity_channel_ext", "channel", "external_id", unique=True),
        Index("ix_identity_user", "user_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    channel: Mapped[str] = mapped_column(String(16))        # feishu / wecom / dingtalk / web
    external_id: Mapped[str] = mapped_column(String(128))   # open_id / userid / 工号
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped["User"] = relationship(back_populates="identities")


# ============================================================
# 阈值配置（本轮新增：跨 L0/L3/L2）
#
# 只存**被覆盖的键**（稀疏），不存全量。
# 存全量的话，THRESHOLD_SPEC 里加一个新参数，所有历史配置行都缺这个键，
# 行为取决于「读的时候怎么兜底」——这是最容易埋雷的地方。
# 存稀疏 + 读取时用 SPEC 默认值兜底，「新增参数」就对历史配置天然兼容。
# ============================================================


class ThresholdConfig(Base):
    """某个作用域下某组的生效覆盖值。"""

    __tablename__ = "threshold_configs"
    __table_args__ = (
        Index(
            "ux_threshold_scope_group",
            "tenant_id",
            "scope",
            "scope_id",
            "group_name",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16))       # company / user / product
    scope_id: Mapped[str] = mapped_column(String(64))    # company:tenant_id / user:id / product:my_product_id
    group_name: Mapped[str] = mapped_column(String(32))  # diff / tiering / notify
    values: Mapped[dict] = mapped_column(JSON)           # 稀疏：只存改过的键
    version: Mapped[int] = mapped_column(Integer, default=1)
    # 存姓名而不是 user_id：留痕是给人看的，半年后没人记得 id=3 是谁
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class ThresholdVersion(Base):
    """阈值变更历史。**只 append，回滚也是 append 一条新版本。**

    为什么回滚不覆盖原行：如果回滚去改 v3 的 values，「谁在什么时候把它改回去了」
    这件事就丢了。而这恰恰是配置表最需要留痕的地方——出问题时第一个要回答的就是
    「这个阈值是什么时候、被谁改成这样的」。
    """

    __tablename__ = "threshold_versions"
    __table_args__ = (
        Index(
            "ix_threshold_ver",
            "tenant_id",
            "scope",
            "scope_id",
            "group_name",
            "version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(16))
    scope_id: Mapped[str] = mapped_column(String(64))
    group_name: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    values: Mapped[dict] = mapped_column(JSON)           # 该版本写入时提交的稀疏覆盖值
    action: Mapped[str] = mapped_column(String(16))      # create / update / rollback
    operator_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ============================================================
# 对标绑定（本轮新增）
#
# 为什么是三元组（user + competitor + my_product）而不是二元组：
# 同一个竞品，A 拿自家 A1 对标、B 拿自家 A2 对标（同时做两个价位段）。
# 二元组会把两人强行挤到一行，后写覆盖先写。
# ============================================================


class CompetitorBinding(Base):
    """谁（user）认为「这个竞品」与「这个自家产品」在同一个战场。"""

    __tablename__ = "competitor_bindings"
    __table_args__ = (
        Index("ux_binding_triplet", "user_id", "competitor_id", "my_product_id", unique=True),
        Index("ix_binding_lookup", "tenant_id", "competitor_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    competitor_id: Mapped[int] = mapped_column(ForeignKey("competitors.id"))
    my_product_id: Mapped[int] = mapped_column(ForeignKey("my_products.id"))

    # 判定快照，不是布尔结论：tier_reasons + 当时阈值版本号 + 原始值 + 判定时间。
    # 存阈值版本号的原因：半年后质疑「为什么当初把这个 ASIN 判成 S」，
    # 没有当时的阈值就无法复现——阈值已经改过三版了。
    basis: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    # 一句话人话理由。面板直接显示，不必每次解析 JSON（列表一次渲染几十行）。
    basis_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # auto_tiering / manual
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / disabled
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


# ============================================================
# 采集运行日志（本轮新增）
#
# 为什么必须新建这张表：现在采集失败时 collector 只 `_touch_checked()` 然后 return，
# **失败这件事没有任何落库痕迹**。数据健康视图要回答「哪一轮采集失败」，
# 没有这张表就只能靠猜。这是本轮唯一为了「看得见」而新增的存储。
# ============================================================


class CollectRun(Base):
    __tablename__ = "collect_runs"
    __table_args__ = (
        Index("ix_collect_runs_time", "tenant_id", "started_at"),
        Index("ix_collect_runs_comp", "competitor_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    # 可空且不加 REFERENCES：竞品被硬删时这行日志仍然有价值（那是排查线索本身）
    competitor_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    asin: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    # full / partial / estimated / failed / skipped
    data_quality: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    signal_count: Mapped[int] = mapped_column(Integer, default=0)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")  # schedule / manual / panel / agent
    errors: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
