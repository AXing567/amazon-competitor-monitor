"""预警必须发给「盯这个竞品的人」，而不是倒进一个群。

## 这组用例守的是什么

改造前投递层有三处「看起来能用、实际是假的」：

1. `ChannelBinding` 这张表**全项目没有任何代码写入**，只有 `router._targets()` 在读。
   它永远为空，于是每次都回落到 `settings.feishu_default_chat_id`——10 个人共用
   一个租户时，**所有人的所有预警全推进同一个群**。
2. `UserIdentity`（人的飞书 open_id）有写入入口、演示数据也造了，但路由器一次都没用过。
   按人分发所需的数据其实早就在库里。
3. `CompetitorBinding` 有 `user_id` 列，但 `add_competitor` 这一跳把 `ctx.user_id`
   丢了，`binding.upsert()` 用「默认用户」兜底 —— 所有竞品都被记成默认负责人加的。
   这不是「没接线」，是**记了一个假的人**。

所以本文件测的不是「函数返回 True」，而是**消息最终落到了谁的收件人 ID 上**。
FakeChannel 记录每一次 `(target, content)`，断言直接对着这个集合做。

## 必须避开的坑：NullChannel 假装成功

`app/channels/base.py` 的 `NullChannel.send_text()` **返回 True**（渠道没配时它
「假装发送成功」）。一旦把走未实现渠道（企微 / 钉钉 / web）的收件人交给
`_channel()`，会拿到 NullChannel → 返回 True → `push_alert` 认为送达，实际只打了
一行日志。**预警静默消失，而调用方看到的是成功。**
用例 ③ 的前置断言就是先把这个坑证明出来（`NullChannel.send_text(...) is True`），
再断言路由层**根本没有把 wecom 交给任何渠道对象**。

## 关于库

用 `tests/conftest.py` 的每模块独立空库，**不**自己改 `DATABASE_URL`：
那个环境变量在 `app/config.py` 被 import 时就已经定型，测试里再改是无效的
（详见 conftest 模块文档）。所有探测都不碰 `data/competitor_monitor.db`。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.adapters.sellersprite import SellerSpriteAdapter  # noqa: E402
from app.adapters.unified import UnifiedSnapshot  # noqa: E402
from app.capabilities import writes  # noqa: E402
from app.capabilities.types import CapContext  # noqa: E402
from app.channels.base import Channel, NullChannel  # noqa: E402
from app.channels.router import ChannelRouter  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_session, init_db  # noqa: E402
from app.models import Competitor, CompetitorBinding, MyProduct  # noqa: E402
from app.services import binding as binding_service  # noqa: E402
from app.services import identity  # noqa: E402


# ============================================================
# 伪造渠道：不发网络请求，只记录「发给了谁、发了什么」
# ============================================================


class FakeChannel(Channel):
    """记录型渠道。`send_*` 恒成功，因为本文件测的是收件人解析，不是投递重试。"""

    name = "fake"

    def __init__(self, sent: list[tuple[str, Any]]) -> None:
        self.sent = sent

    async def send_text(self, to: str, text: str) -> bool:
        self.sent.append((to, text))
        return True

    async def send_card(self, to: str, card: dict) -> bool:
        self.sent.append((to, card))
        return True

    async def health(self) -> dict:
        return {"channel": self.name, "status": "ok"}


def _make_router(monkeypatch: pytest.MonkeyPatch) -> tuple[list, list[str], ChannelRouter]:
    """返回 `(已发送记录, 被请求过的渠道名, router)`。

    `monkeypatch` 打的是**实例属性** `_channel`，所以 `push_alert` 里的
    `self._channel(name)` 直接命中假实现，绝不会去连真飞书。
    顺带记录被请求的渠道名——这是用例 ③ 证明「wecom 根本没被交给渠道对象」的依据。
    """
    sent: list[tuple[str, Any]] = []
    requested: list[str] = []

    def fake_channel(name: str) -> Channel:
        requested.append(name)
        return FakeChannel(sent)

    router = ChannelRouter()
    monkeypatch.setattr(router, "_channel", fake_channel)
    return sent, requested, router


# ============================================================
# 造数
# ============================================================


def _seed_my_product(tenant: str, asin: str = "B0SELF0001", price: float = 40.0) -> int:
    """自家锚点。价格 / 核心词 / 类目路径都配齐，避免分层判定落到「证据不足」。"""
    init_db()
    with get_session() as s:
        my = MyProduct(
            tenant_id=tenant,
            asin=asin,
            current_price=price,
            core_keywords={"toy car": 0.5, "diecast car": 0.3},
            category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
        )
        s.add(my)
        s.flush()
        return int(my.id)


def _seed_competitor(tenant: str, asin: str) -> tuple[int, int]:
    """一个竞品（直接落库，不走采集）。返回 `(my_product_id, competitor_id)`。"""
    my_id = _seed_my_product(tenant)
    with get_session() as s:
        comp = Competitor(
            tenant_id=tenant,
            my_product_id=my_id,
            asin=asin,
            tier="A",
            status="active",
        )
        s.add(comp)
        s.flush()
        return my_id, int(comp.id)


def _make_user(tenant: str, name: str, channel: str, external_id: str) -> int:
    """建一个用户并绑一个平台身份，返回 user_id。"""
    user = identity.create_user(name, tenant_id=tenant)
    assert user.get("ok") is True, user
    uid = int(user["id"])
    res = identity.upsert_identity(uid, channel, external_id, tenant_id=tenant)
    assert res.get("ok") is True, res
    return uid


def _bind(tenant: str, my_id: int, comp_id: int, user_id: int) -> int:
    """建一条 active 绑定（走生产写入路径，不裸插）。"""
    res = binding_service.upsert(
        competitor_id=comp_id,
        my_product_id=my_id,
        user_id=user_id,
        tenant_id=tenant,
        source=binding_service.SOURCE_MANUAL,
    )
    assert res.get("ok") is True, res
    return int(res["binding_id"])


def _targets_of(sent: list[tuple[str, Any]]) -> set[str]:
    return {to for to, _ in sent}


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


# ============================================================
# ① 只推给绑定人
# ============================================================


def test_alert_goes_only_to_users_bound_to_that_competitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 个用户都有飞书身份，其中 2 人对竞品 X 有 active 绑定 → 只有这 2 人收到。

    改造前这条会失败在「所有人都收到」上：`_targets()` 只读永远为空的
    `channel_bindings`，最终回落到 `settings.feishu_default_chat_id`
    （本用例把它设成 `oc_team_default`，于是断言里会出现这个群 ID）。
    """
    tenant = "t-alert-peruser"
    my_id, comp_x = _seed_competitor(tenant, "B0ALERTX01")

    zhangsan = _make_user(tenant, "张三", "feishu", "ou_zhangsan")
    lisi = _make_user(tenant, "李四", "feishu", "ou_lisi")
    wangwu = _make_user(tenant, "王五", "feishu", "ou_wangwu")

    _bind(tenant, my_id, comp_x, zhangsan)
    _bind(tenant, my_id, comp_x, lisi)

    # 群 ID 故意配上一个值：万一走了旧路径，它就会出现在收件人里，断言立刻红。
    monkeypatch.setattr(settings, "feishu_default_chat_id", "oc_team_default")

    sent, requested, router = _make_router(monkeypatch)
    ok = asyncio.run(router.push_alert(tenant, "X 降价 12%", competitor_id=comp_x))

    assert ok is True
    assert _targets_of(sent) == {"ou_zhangsan", "ou_lisi"}, (
        f"收件人不等于绑定人：{_targets_of(sent)}。"
        f"出现 oc_team_default 说明又退回群广播了；出现 ou_wangwu 说明没按绑定过滤"
    )
    assert "ou_wangwu" not in _targets_of(sent), "没绑这个竞品的人不该收到"
    assert len(sent) == 2, f"每人只该收一条，实际 {len(sent)} 条：{sent}"
    assert set(requested) == {"feishu"}, f"只该走 feishu，实际请求了 {requested}"


def test_same_person_bound_twice_gets_one_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一人对同一竞品有两条绑定（不同自家产品）→ 他只收到一条。

    为什么会有两条：绑定是 `(user, competitor, my_product)` 三元组，
    同一个竞品可能同时对标他两个自家产品（比如两个价位段都做）。
    那不是两个人，不该发两遍。去重键是 `(channel, target)`。
    重复推送在 P0 场景下等同于制造噪音，人会开始忽略它。
    """
    tenant = "t-alert-dedup"
    my_id, comp = _seed_competitor(tenant, "B0ALERTD01")

    # open_id 在全库唯一（`ux_identity_channel_ext` 不含 tenant_id），
    # 所以每个用例的 external_id 都要自带前缀，否则同模块会互相占用。
    zhangsan = _make_user(tenant, "张三", "feishu", "ou_dedup_zhangsan")
    my2 = _seed_my_product(tenant, asin="B0SELF0002", price=40.0)
    _bind(tenant, my_id, comp, zhangsan)
    _bind(tenant, my2, comp, zhangsan)

    sent, _, router = _make_router(monkeypatch)
    asyncio.run(router.push_alert(tenant, "X 断货", competitor_id=comp))

    targets = [to for to, _ in sent]
    assert targets == ["ou_dedup_zhangsan"], (
        f"两条绑定指向同一个人，只该发一条，实际：{targets}"
    )


# ============================================================
# ② 无人绑定 → 退回默认渠道，并且出声
# ============================================================


def test_unbound_competitor_falls_back_to_default_channel_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """竞品 Y 没有任何绑定 → 仍然发送（不静默丢），但必须留一条含「退回」的 WARNING。

    为什么允许退回而不是丢弃：这是监控系统，静默丢弃 = 漏报，而且**没人会发现
    路由数据缺了**。广播 + 显式告警是两害相权；这条 warning 让配置问题可见。
    所以两个断言缺一不可：既要有消息发出去，也要有 WARNING 指名道姓。
    """
    tenant = "t-alert-nobind"
    _, comp_y = _seed_competitor(tenant, "B0ALERTY01")
    monkeypatch.setattr(settings, "feishu_default_chat_id", "oc_team_default")

    sent, _, router = _make_router(monkeypatch)
    with caplog.at_level(logging.WARNING):
        ok = asyncio.run(router.push_alert(tenant, "Y 断货了", competitor_id=comp_y))

    assert ok is True, "无人绑定也不许静默丢弃：预警必须发出去"
    assert _targets_of(sent) == {"oc_team_default"}, (
        f"应退回租户默认渠道，实际收件人：{_targets_of(sent)}"
    )

    msgs = _warnings(caplog)
    assert any(f"竞品 {comp_y}" in m and "退回" in m for m in msgs), (
        f"退款发生时必须有含竞品 id 与「退回」的 WARNING，实际 WARNING：{msgs}"
    )


# ============================================================
# ③ 未实现的渠道被跳过且被点名（防 NullChannel 假装成功）
# ============================================================


def test_unimplemented_channel_is_skipped_and_named(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """某人只有 `channel="wecom"` 的身份、没有 feishu → 他收不到任何东西，
    且路由层**根本没有把 wecom 交给渠道对象**，日志里 WARNING 点名了 wecom。

    前置那段断言是在证明这个坑真实存在：`_channel("wecom")` 拿到 `NullChannel`，
    而 `NullChannel.send_text()` 返回 True。所以如果把它放进投递目标，
    `push_alert` 会返回 True、调用方以为送达，实际只写了一行日志——
    预警就这么消失得无声无息。
    """
    tenant = "t-alert-wecom"
    my_id, comp_z = _seed_competitor(tenant, "B0ALERTZ01")

    lisi = _make_user(tenant, "李四", "wecom", "wx_lisi")
    _bind(tenant, my_id, comp_z, lisi)

    monkeypatch.setattr(settings, "feishu_default_chat_id", "oc_team_default")

    # ---- 前置：这个坑是真的 ----
    trap = ChannelRouter()._channel("wecom")
    assert isinstance(trap, NullChannel), "前置：未实现渠道应落到 NullChannel"
    assert asyncio.run(trap.send_text("wx_lisi", "x")) is True, (
        "前置：NullChannel 会返回 True（「假装发送成功」），这正是必须绕开它的原因"
    )

    sent, requested, router = _make_router(monkeypatch)
    with caplog.at_level(logging.WARNING):
        asyncio.run(router.push_alert(tenant, "Z 降价 8%", competitor_id=comp_z))

    assert "wecom" not in requested, (
        f"未实现的渠道不该被交给渠道对象（会被 NullChannel 吞掉）；实际请求了 {requested}"
    )
    assert "wx_lisi" not in _targets_of(sent), (
        f"只有企微身份的人不该被算作收到预警，实际收件人：{_targets_of(sent)}"
    )

    msgs = _warnings(caplog)
    assert any("wecom" in m for m in msgs), (
        f"未实现的渠道必须被 WARNING 点名，不允许静默丢弃，实际 WARNING：{msgs}"
    )


# ============================================================
# ④ 绑定记的是真人（而不是默认用户兜底）
# ============================================================


def _fake_fetch_snapshot(self, asin: str, marketplace: str = "US") -> UnifiedSnapshot:
    """离线快照：数据配齐，让四同法则能出确定结论（不需要 force 也不会被拒）。"""
    return UnifiedSnapshot(
        asin=asin,
        marketplace=marketplace,
        price=36.0,
        net_price=36.0,
        rank_sub=1200,
        monthly_units=900,
        rating=4.5,
        review_count=320,
        in_stock=True,
        title="Fake competitor title",
        brand="FakeBrand",
        category_path=["Toys & Games", "Toy Vehicles", "Play Trucks"],
        keywords=[
            {"keyword": "toy car", "traffic_share": 0.5},
            {"keyword": "diecast car", "traffic_share": 0.3},
        ],
        data_quality="full",
        source="fake",
    )


async def _fake_fetch_async(self, asin: str, marketplace: str = "US") -> UnifiedSnapshot:
    return _fake_fetch_snapshot(self, asin, marketplace)


def _binding_of(comp_id: int) -> Optional[CompetitorBinding]:
    with get_session() as s:
        rows = list(
            s.execute(
                select(CompetitorBinding).where(CompetitorBinding.competitor_id == comp_id)
            ).scalars()
        )
        if not rows:
            return None
        row = rows[0]
        s.expunge(row)
        return row


def test_binding_records_the_real_operator_not_the_default_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """经 `capabilities.writes.add_competitor(ctx)` 入池后，绑定必须记在张三名下。

    这条用例在**修之前会红**，而且红的样子不是抛异常，是「落库值悄悄等于默认用户」：

    - `writes.add_competitor` 原先是 `pool.add_competitor(ctx.tenant_id, asin, force=force)`，
      `ctx.user_id` 在这一跳就没了；
    - `binding.upsert()` 收到 `user_id=None`，于是走
      `identity.default_user_id(tenant)` 兜底；
    - 结果 `competitor_bindings.user_id` = 老板，而不是操作人张三。

    所以这里造了两个用户：先把「老板」建成 owner（`ensure_default_user`），再建张三。
    `_first_active` 优先 owner，因此兜底值一定是老板——张三不可能是兜底结果，
    断言 `== 张三` 才不可能恒真。
    """
    tenant = "t-alert-realuser"
    boss = identity.ensure_default_user(tenant)          # owner，兜底目标
    _seed_my_product(tenant)
    zhangsan = identity.create_user("张三", tenant_id=tenant)
    zhangsan_id = int(zhangsan["id"])
    default_id = identity.default_user_id(tenant)

    assert default_id == int(boss["id"]), "前置：兜底用户应是 owner 老板"
    assert default_id != zhangsan_id, "前置：兜底用户必须与操作人不同，否则断言恒真"

    monkeypatch.setattr(SellerSpriteAdapter, "fetch_snapshot", _fake_fetch_async)

    res = writes.add_competitor(
        CapContext(tenant_id=tenant, user_id=zhangsan_id), "B0REAL0001", force=True
    )
    assert res.get("ok") is True, res

    row = _binding_of(int(res["competitor_id"]))
    assert row is not None, "入池应同时建立一条对标绑定"
    assert row.user_id == zhangsan_id, (
        f"绑定记成了 user_id={row.user_id}，应为操作人张三（{zhangsan_id}）。"
        f"等于 {default_id} 说明 ctx.user_id 又在中途丢了、被兜底成默认用户"
    )
    assert row.user_id != default_id


def test_unknown_operator_still_falls_back_to_default_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ctx.user_id is None`（不认识的平台身份）是允许的：由下游兜底成默认用户，不报错。

    与上一条配套：修的是「**认识的**操作人被丢掉」，不是「不许有兜底」。
    没有兜底的话，任何身份解析失败都会让「加竞品」这件事直接失败——
    拿一个假的归属人换一次加不进去，不划算。
    """
    tenant = "t-alert-realuser-none"
    boss = identity.ensure_default_user(tenant)
    _seed_my_product(tenant)
    default_id = identity.default_user_id(tenant)
    assert default_id == int(boss["id"])

    monkeypatch.setattr(SellerSpriteAdapter, "fetch_snapshot", _fake_fetch_async)

    res = writes.add_competitor(
        CapContext(tenant_id=tenant, user_id=None), "B0REAL0002", force=True
    )
    assert res.get("ok") is True, res

    row = _binding_of(int(res["competitor_id"]))
    assert row is not None
    assert row.user_id == default_id, "身份缺失时应兜底到默认用户，且必须真的落库"


# ============================================================
# ⑤ 老 REST 端点（POST /api/competitors）也不能丢身份
# ============================================================


def test_rest_add_competitor_records_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """面板/脚本走的 `POST /api/competitors` 同样要把 `X-User-Id` 记进绑定。

    **修之前这条是红的**，而且红的样子很安静：端点原先是
    `pool.add_competitor(settings.tenant_id, body.asin, force=body.force)`，
    请求头里的 `X-User-Id` 从头到尾没人读，绑定又落回默认用户（老板）。
    于是「谁加的竞品」在库里永远是老板——能力层修好了，这条老端点还在记假人。

    为什么必须打桩 `fetch_snapshot`：`pool.add_competitor` 里 `force` **只影响
    是否放行四同法则的否决**，采集发生在它之前（`asyncio.run(SellerSpriteAdapter()
    .fetch_snapshot(...))` 无条件执行），所以 `force=True` 一样会联网。测试不许依赖网络。
    """
    # 延迟导入：`app.api.server` 在 import 期会建 FastAPI 实例并 basicConfig，
    # 放在用例内导，避免影响本模块其它用例的日志与 import 顺序。
    from fastapi.testclient import TestClient

    from app.api.server import app as fastapi_app

    tenant = settings.tenant_id          # 端点的租户来自 resolve_ctx 的默认值
    boss = identity.ensure_default_user(tenant)
    default_id = identity.default_user_id(tenant)
    _seed_my_product(tenant)

    caller = identity.create_user("面板张三", tenant_id=tenant)
    caller_id = int(caller["id"])
    assert default_id == int(boss["id"]), "前置：兜底用户是 owner 老板"
    assert caller_id != default_id, "前置：调用人必须与兜底用户不同，否则断言恒真"

    monkeypatch.setattr(SellerSpriteAdapter, "fetch_snapshot", _fake_fetch_async)

    # 不用 `with TestClient(...)`：不触发 lifespan，因而不启动调度器（同 QA 用例的做法）。
    client = TestClient(fastapi_app)
    resp = client.post(
        "/api/competitors",
        json={"asin": "B0REST0001", "force": True},
        headers={"X-User-Id": str(caller_id)},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data.get("ok") is True, data

    row = _binding_of(int(data["competitor_id"]))
    assert row is not None, "入池应同时建立一条对标绑定"
    assert row.user_id == caller_id, (
        f"REST 端点把绑定记成了 user_id={row.user_id}，应为请求头里的调用人"
        f"（{caller_id}）。等于 {default_id} 说明 X-User-Id 又没被读、被兜底成默认用户"
    )
    assert row.user_id != default_id
