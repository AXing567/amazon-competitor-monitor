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

    # --- 5. Listing 基线 ---
    title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    image_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bullet_points: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

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
    tier: Mapped[str] = mapped_column(String(2), default="C")   # S/A/B/C
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
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="competitor")


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
    __table_args__ = (Index("ix_action_verify_due", "verify_due_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), index=True)
    my_product_id: Mapped[int] = mapped_column(ForeignKey("my_products.id"))
    decision_id: Mapped[Optional[int]] = mapped_column(ForeignKey("decisions.id"), nullable=True)

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
