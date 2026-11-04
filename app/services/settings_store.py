"""L3 · 阈值配置仓库：三层继承、保存、版本、回滚、生效解析。

## 三层分工，一句话讲清

    L0  threshold_configs 表   只存「被覆盖的键」（稀疏），不存全量
    L3  本文件 resolve()       公司默认 → 个人覆盖 → 产品覆盖，合并出一份全量
    L2  DiffEngine(thresholds) 只认传进来的对象，RULES 降级为默认值来源

## 为什么存稀疏而不是存全量

存全量的话，`THRESHOLD_SPEC` 里加一个新参数，所有历史配置行都缺这个键，
行为就取决于「读的时候怎么兜底」——这是最容易埋雷的地方。
存稀疏 + 读取时用 SPEC 默认值兜底，则「新增参数」对所有历史配置天然兼容，
而且能明确回答「这个键的值是谁改的」（`sources` 标注每个键来自哪一层）。

## 为什么回滚是「追加」而不是「改回」

`threshold_versions` 只 append。回滚 = 拿历史 values 走一次正常保存流程，
`version = max + 1`。如果回滚去覆盖原行，「谁在什么时候把它改回去了」就丢了，
而这恰恰是配置类表最需要留痕的地方。

## 为什么采集路径只走两层（company + product）

定时采集是**系统行为，没有自然人**，所以 `user_id=None`。个人覆盖只在
「面板试算」和「这个人在飞书里问的结论」上生效。要按人分发日报是另一轮的工作量。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.domain.thresholds import (
    GROUP_DIFF,
    GROUP_NOTIFY,
    GROUP_TIERING,
    GROUPS,
    DiffThresholds,
    NotifyThresholds,
    TieringThresholds,
    defaults,
    for_group,
    validate,
)
from app.domain.thresholds import warnings as threshold_warnings
from app.models import ThresholdConfig, ThresholdVersion

logger = logging.getLogger(__name__)

SCOPE_COMPANY = "company"
SCOPE_USER = "user"
SCOPE_PRODUCT = "product"

#: 合并顺序：后面的覆盖前面的。顺序本身就是业务规则，不要随手调。
SCOPE_ORDER: tuple[str, ...] = (SCOPE_COMPANY, SCOPE_USER, SCOPE_PRODUCT)

SCOPE_TITLES: dict[str, str] = {
    SCOPE_COMPANY: "公司默认",
    SCOPE_USER: "个人覆盖",
    SCOPE_PRODUCT: "产品覆盖",
}

SOURCE_DEFAULT = "default"


# ============================================================
# 生效解析（读）
# ============================================================


def resolve(
    tenant_id: Optional[str] = None,
    *,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    """合并三层，返回 `(全量值, 每键来源)`。

    `user_id=None` 或 `my_product_id=None` 时对应层被**跳过**（不是当空值）：
    采集路径不传 user_id，于是天然只走 company + product 两层。
    """
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        return _resolve_in_session(s, tenant_id, user_id=user_id, my_product_id=my_product_id)


def resolve_diff_thresholds(
    tenant_id: Optional[str] = None,
    my_product_id: Optional[int] = None,
    *,
    user_id: Optional[int] = None,
) -> DiffThresholds:
    """给采集/判定用。**采集时不要传 user_id**（系统行为没有自然人）。"""
    values, _ = resolve(tenant_id, user_id=user_id, my_product_id=my_product_id)
    return DiffThresholds.from_dict(values[GROUP_DIFF])


def resolve_tiering_thresholds(
    tenant_id: Optional[str] = None,
    my_product_id: Optional[int] = None,
    *,
    user_id: Optional[int] = None,
) -> TieringThresholds:
    values, _ = resolve(tenant_id, user_id=user_id, my_product_id=my_product_id)
    return TieringThresholds.from_dict(values[GROUP_TIERING])


def resolve_notify_thresholds(
    tenant_id: Optional[str] = None,
    my_product_id: Optional[int] = None,
    *,
    user_id: Optional[int] = None,
) -> NotifyThresholds:
    values, _ = resolve(tenant_id, user_id=user_id, my_product_id=my_product_id)
    return NotifyThresholds.from_dict(values[GROUP_NOTIFY])


def describe_layers(
    tenant_id: Optional[str] = None,
    *,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
) -> list[dict]:
    """当前参与合并的层（含版本号）。面板「这个值是哪一层来的」直接用它。"""
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        rows = _load_configs(s, tenant_id, user_id=user_id, my_product_id=my_product_id)
    return [
        {
            "scope": row.scope,
            "scope_title": SCOPE_TITLES.get(row.scope, row.scope),
            "scope_id": row.scope_id,
            "group_name": row.group_name,
            "version": row.version,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "note": row.note,
            "values": dict(row.values or {}),
        }
        for row in rows
    ]


def effective_version(
    tenant_id: Optional[str] = None,
    *,
    group_name: str,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
) -> dict:
    """该组「生效版本」的快照：`{"version": max, "layers": {scope: version}}`。

    为什么 basis 里要存版本号：半年后质疑「为什么当初把这个 ASIN 判成 S」，
    阈值已经改过三版了，没有当时的阈值就无法复现。
    """
    tenant_id = tenant_id or settings.tenant_id
    with get_session() as s:
        rows = _load_configs(s, tenant_id, user_id=user_id, my_product_id=my_product_id)
    layers = {r.scope: r.version for r in rows if r.group_name == group_name}
    return {
        "version": max(layers.values()) if layers else 0,
        "layers": layers,
    }


# ============================================================
# 保存 / 回滚（写）
# ============================================================


def save(
    *,
    scope: str,
    group_name: str,
    values: Mapping[str, Any],
    scope_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    operator: Optional[str] = None,
    note: str = "",
    replace: bool = False,
    action: Optional[str] = None,
) -> dict:
    """写入一组稀疏覆盖值。返回 `{"ok", "version", "changed_keys"}`。

    - `replace=False`（默认）：**合并**本次提交的键，未提交的键保持原样。
      面板只改一个输入框时不会把其它键冲掉。
    - `replace=True`：用本次提交的 values **替换**掉整行的覆盖值。
      回滚用它——回滚的语义是「回到那一版长什么样」，包括该版没有的键要消失。

    为什么返回值是 dict 而不是设计文档里写的 `int`：调用方需要 `changed_keys`
    和错误原因（只有 dict 能一次带回），返回 int 会逼着调用方再查一次库。
    这是一处刻意的偏离，已在交付报告里说明。
    """
    tenant_id = tenant_id or settings.tenant_id

    if scope not in SCOPE_ORDER:
        return {"ok": False, "error": f"未知作用域 {scope!r}（可选：{'/'.join(SCOPE_ORDER)}）"}
    if group_name not in GROUPS:
        return {"ok": False, "error": f"未知阈值分组 {group_name!r}（可选：{'/'.join(GROUPS)}）"}
    if scope == SCOPE_COMPANY and not scope_id:
        scope_id = tenant_id
    if not scope_id:
        return {
            "ok": False,
            "error": f"scope={scope} 时必须提供 scope_id",
            "hint": "user 层填用户 id，product 层填自家产品 id",
        }
    scope_id = str(scope_id)

    cleaned, errors = validate(group_name, values)
    if errors:
        return {
            "ok": False,
            "error": errors[0],
            "errors": errors,
            "hint": "按 /api/config/thresholds/schema 给出的类型与范围修正后重试",
        }
    if not cleaned and not replace:
        return {"ok": False, "error": "没有可保存的键", "hint": "values 不能为空"}

    now = datetime.now()
    with get_session() as s:
        row = _get_config(s, tenant_id, scope, scope_id, group_name)
        before = dict(row.values or {}) if row is not None else {}
        after = dict(cleaned) if replace else {**before, **cleaned}

        changed = [k for k in sorted(cleaned) if before.get(k) != after.get(k)]
        removed = sorted(k for k in before if k not in after)

        if row is not None and not changed and not removed:
            return {
                "ok": True,
                "version": row.version,
                "changed_keys": [],
                "removed_keys": [],
                "action": "noop",
                "note": "值与当前一致，未产生新版本",
            }

        if row is None:
            row = ThresholdConfig(
                tenant_id=tenant_id,
                scope=scope,
                scope_id=scope_id,
                group_name=group_name,
                values=after,
                version=1,
                updated_by=operator,
                updated_at=now,
                note=note,
            )
            s.add(row)
            act = "create"
        else:
            row.values = after
            row.version = int(row.version or 0) + 1
            row.updated_by = operator
            row.updated_at = now
            row.note = note
            act = action or "update"
        s.flush()

        # 只 append，永不改历史行。回滚也是往这里再写一条。
        s.add(
            ThresholdVersion(
                tenant_id=tenant_id,
                scope=scope,
                scope_id=scope_id,
                group_name=group_name,
                version=row.version,
                # 存「该版本生效的累积稀疏值」而不是本次增量：
                # 回滚要能一次复现出那一版的样子，靠增量重放要跑一遍历史，还怕中间缺行。
                values=after,
                action=act,
                operator_id=operator,
                created_at=now,
                note=note,
            )
        )
        s.flush()

        return {
            "ok": True,
            "version": row.version,
            "changed_keys": changed,
            "removed_keys": removed,
            "action": act,
            "scope": scope,
            "scope_id": scope_id,
            "group_name": group_name,
            "values": after,
            # 非阻断提示（例如「P1 门槛比 P0 还高，降价方向的 P1 永不触发」）：
            # 面板要显示出来，但不该因此判保存失败
            "warnings": threshold_warnings(group_name, after),
        }


def rollback(
    *,
    scope: str,
    group_name: str,
    version: int,
    scope_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    operator: Optional[str] = None,
    note: str = "",
) -> dict:
    """回滚到历史某一版。**结果是一条新版本，不是覆盖历史。**

    这就是「谁在什么时候把它改回去了」能留痕的原因。
    """
    tenant_id = tenant_id or settings.tenant_id
    if scope == SCOPE_COMPANY and not scope_id:
        scope_id = tenant_id
    if not scope_id:
        return {"ok": False, "error": f"scope={scope} 时必须提供 scope_id"}

    with get_session() as s:
        hist = s.execute(
            select(ThresholdVersion).where(
                ThresholdVersion.tenant_id == tenant_id,
                ThresholdVersion.scope == scope,
                ThresholdVersion.scope_id == str(scope_id),
                ThresholdVersion.group_name == group_name,
                ThresholdVersion.version == int(version),
            )
        ).scalar_one_or_none()
        if hist is None:
            return {
                "ok": False,
                "error": f"找不到 {scope}/{scope_id}/{group_name} 的 v{version}",
                "hint": "先查版本列表确认版本号",
            }
        snapshot_values = dict(hist.values or {})

    result = save(
        scope=scope,
        group_name=group_name,
        values=snapshot_values,
        scope_id=scope_id,
        tenant_id=tenant_id,
        operator=operator,
        note=note or f"回滚到 v{version}",
        replace=True,
        action="rollback",
    )
    result["from_version"] = int(version)
    if result.get("ok"):
        result["new_version"] = result["version"]
    return result


def list_versions(
    *,
    scope: str,
    group_name: str,
    scope_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    limit: int = 20,
) -> list[dict]:
    """版本列表（新→旧）。只读。"""
    tenant_id = tenant_id or settings.tenant_id
    if scope == SCOPE_COMPANY and not scope_id:
        scope_id = tenant_id

    with get_session() as s:
        rows = list(
            s.execute(
                select(ThresholdVersion)
                .where(
                    ThresholdVersion.tenant_id == tenant_id,
                    ThresholdVersion.scope == scope,
                    ThresholdVersion.scope_id == str(scope_id),
                    ThresholdVersion.group_name == group_name,
                )
                .order_by(ThresholdVersion.version.desc())
                .limit(max(1, int(limit)))
            ).scalars()
        )
    return [
        {
            "version": r.version,
            "action": r.action,
            "operator": r.operator_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "note": r.note,
            "values": dict(r.values or {}),
        }
        for r in rows
    ]


# ============================================================
# 内部
# ============================================================


def _resolve_in_session(
    s: Session,
    tenant_id: str,
    *,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
) -> tuple[dict[str, dict], dict[str, dict]]:
    rows = _load_configs(s, tenant_id, user_id=user_id, my_product_id=my_product_id)

    values: dict[str, dict] = {group: defaults(group) for group in GROUPS}
    sources: dict[str, dict] = {
        group: {key: SOURCE_DEFAULT for key in values[group]} for group in GROUPS
    }

    for row in rows:                       # 已按 SCOPE_ORDER 排好，后写覆盖先写
        if row.group_name not in values:
            continue
        cleaned = _clean_on_read(row.group_name, row.values)
        values[row.group_name].update(cleaned)
        for key in cleaned:
            sources[row.group_name][key] = row.scope

    # 统一再规范化一次：JSON 里可能存着 "0.05" 这种字符串（历史数据 / 手工改库）
    for group in GROUPS:
        values[group] = for_group(group, values[group]).to_dict()

    return values, sources


def _load_configs(
    s: Session,
    tenant_id: str,
    *,
    user_id: Optional[int] = None,
    my_product_id: Optional[int] = None,
) -> list[ThresholdConfig]:
    """按 SCOPE_ORDER 返回参与合并的配置行（同层同组各一行）。"""
    wanted: dict[str, str] = {SCOPE_COMPANY: tenant_id}
    if user_id is not None:
        wanted[SCOPE_USER] = str(user_id)
    if my_product_id is not None:
        wanted[SCOPE_PRODUCT] = str(my_product_id)

    rows = list(
        s.execute(
            select(ThresholdConfig).where(ThresholdConfig.tenant_id == tenant_id)
        ).scalars()
    )
    picked: list[ThresholdConfig] = []
    for scope in SCOPE_ORDER:
        sid = wanted.get(scope)
        if sid is None:
            continue
        for group in GROUPS:
            match = [
                r for r in rows
                if r.scope == scope and r.scope_id == sid and r.group_name == group
            ]
            picked.extend(match)
    return picked


def _clean_on_read(group_name: str, raw: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """读路径只做「键是否属于本组」的过滤，**不做范围校验**。

    范围校验属于写路径。读路径若因为「某行是旧版本、当时范围更宽松」而丢掉这个键，
    会出现「配置在库里、但就是不生效」这种没人能查的怪事。
    """
    if not raw:
        return {}
    allowed = set(defaults(group_name))
    return {k: v for k, v in raw.items() if k in allowed and v is not None}


def _get_config(
    s: Session, tenant_id: str, scope: str, scope_id: str, group_name: str
) -> Optional[ThresholdConfig]:
    return s.execute(
        select(ThresholdConfig).where(
            ThresholdConfig.tenant_id == tenant_id,
            ThresholdConfig.scope == scope,
            ThresholdConfig.scope_id == str(scope_id),
            ThresholdConfig.group_name == group_name,
        )
    ).scalar_one_or_none()
