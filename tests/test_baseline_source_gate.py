"""自家数据来源的分档：真实模式只认 SP-API 与手工，反推只活在 Mock。

## 这组用例守的是什么

「自家锚点」这个位置以前有一条**没有报错的错路**：

    pool.update_baseline() → _autofill() → SellerSpriteAdapter
                          → 把别人家商品页上的公开数据写进自家锚点

它的输出和「真的接入了自己后台」长得一模一样：13 个字段全有值、就绪度 100%、
四维能力全绿。面板上分不出差别，下游每个判断（该不该跟价、进攻窗口、毛利测算）
也分不出——它们只知道"有个数"，不知道这个数是谁的账。

所以要守的不是某个函数的返回值，而是**三件事同时成立**：

1. 真实模式下，写入口就拒掉反推（`source=sellersprite_proxy`）；
2. 自动补全在真实模式下整段不跑（它是那条错路的起点）；
3. Mock 里允许反推，但每一格都带得出自己的出身。

第 3 条容易被当成"测试洁癖"，其实它是第 1 条能成立的前提：
如果 Mock 里的数没有逐格标注，真实模式那套闸门就没有对照物，
下次有人把 Mock 数据拷进真实库，没有任何一处会响。

## 为什么这些用例大多要显式站在某个模式下

`get_data_mode()` 的默认值是 `live`——**失败要往「当真」的方向失败**。
所以"真实模式拒绝反推"的用例不需要夹具，而"Mock 接受反推"的必须显式切过去。
反过来的写法（默认为 mock）会让漏测的那条用例在真实模式里悄悄放行。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.self_snapshot import SelfSnapshot                   # noqa: E402
from app.db import ensure_mode_ready, get_session, using_data_mode   # noqa: E402
from app.domain import baseline as bl                                # noqa: E402
from app.models import MyProduct                                     # noqa: E402
from app.services import pool                                        # noqa: E402
from app.services import self_sync                                   # noqa: E402

TS = "t-gate"


def _row(tenant: str) -> Optional[MyProduct]:
    with get_session() as s:
        return s.query(MyProduct).filter(MyProduct.tenant_id == tenant).one_or_none()


@pytest.fixture()
def mock_mode():
    """切到 Mock 库。反推是 Mock 的构造方式，只能在那边验。"""
    ensure_mode_ready("mock")
    with using_data_mode("mock"):
        yield


# ============================================================
# ① FIELD_ORIGINS 必须覆盖全部锚点字段
# ============================================================


def test_field_origins_covers_every_anchor_field():
    """维度表里出现的每个字段，都要在 FIELD_ORIGINS 里有出身说明。

    漏一个的症状不是报错，而是面板上那一格显示「来源未定义（FIELD_ORIGINS 漏了
    这个字段）」，以及真实模式下**没有接入指引**——人看到"缺"却不知道去哪补。
    这类缺口只在新增字段时出现，而新增字段的人通常不会主动回来改这张表。
    """
    declared = {name for dim in bl.DIMENSIONS for name, _ in dim.fields}
    # category_path 不进五维（不计入就绪度），但它是 extras，面板照样品示
    declared.add("category_path")

    missing = declared - set(bl.FIELD_ORIGINS)
    assert not missing, f"这些字段没有出身说明：{sorted(missing)}"

    extra = set(bl.FIELD_ORIGINS) - declared
    assert not extra, (
        f"FIELD_ORIGINS 里有维度表里不存在的字段：{sorted(extra)}——"
        "多半是字段改名后旧条目没删，它会让「该从哪来」指向一个不存在的位置"
    )


def test_every_field_origin_has_a_manual_path():
    """每个字段都必须有「手工怎么填」——包括 SP-API 能给的。

    理由：SP-API 可能一直配不上（本项目当前就是），而锚点不能因此永远空着。
    `spapi=None` 的字段更依赖这句话，它们是接口**结构上给不了**的那部分。
    """
    empty = [name for name, o in bl.FIELD_ORIGINS.items() if not o.manual.strip()]
    assert not empty, f"这些字段没写手工补法：{empty}"


def test_origin_labels_cover_all_origin_values():
    """四个 origin 都要有中文名。少一个，面板上就会露出英文枚举值。"""
    for origin in (
        bl.ORIGIN_SP_API,
        bl.ORIGIN_MANUAL,
        bl.ORIGIN_SELLERSPRITE,
        bl.ORIGIN_SIMULATED,
    ):
        assert origin in bl.ORIGIN_LABELS, origin


# ============================================================
# ② 闸门：真实模式拒反推，Mock 放行
# ============================================================


def test_live_mode_rejects_sellersprite_anchor(clean_db):
    """真实模式下写一整批反推数据：拒掉，而且库里一个字都不该动。"""
    tenant = f"{TS}-reject"

    out = pool.update_baseline(
        tenant,
        asin="B0GATE0001",
        current_price=22.99,
        source=bl.SOURCE_SELLERSPRITE,
    )

    assert out["ok"] is False
    # 必须能区分"参数错"和"来源错"：调用方要据此给不同的提示，
    # 都塞进 `error` 一个字段，前端只能显示一句话，而这两件事的补法完全不同。
    assert out["error_kind"] == "source", out

    # 关键：拒在写库之前。若先写后判，数据已经在库里躺着了，
    # 面板刷新一次就能看到——而这次调用报的是失败。
    assert _row(tenant) is None, "被拒的写入不该建出锚点行"


@pytest.mark.parametrize("source", [bl.SOURCE_MANUAL, bl.SOURCE_SP_API])
def test_live_mode_accepts_manual_and_spapi(clean_db, source):
    """手工与 SP-API 是真实模式的两个合法来源，不能连它们一起拒。"""
    tenant = f"{TS}-ok-{source}"

    out = pool.update_baseline(
        tenant, asin="B0GATE0002", current_price=19.99, source=source
    )

    assert out["ok"] is True, out
    assert _row(tenant).source == source


def test_mock_mode_accepts_sellersprite_anchor(clean_db, mock_mode):
    """同一个调用在 Mock 下必须放行——否则演示数据根本造不出来。"""
    tenant = f"{TS}-mock-ok"

    out = pool.update_baseline(
        tenant,
        asin="B0GATE0003",
        current_price=22.99,
        source=bl.SOURCE_SELLERSPRITE,
    )

    assert out["ok"] is True, out
    assert _row(tenant).source == bl.SOURCE_SELLERSPRITE


def test_live_block_reason_speaks_to_a_human():
    """拒绝理由要能直接显示给人看：说清「为什么不行」和「那该怎么办」。

    只返回 `False` 或 `"invalid source"`，面板就只能显示一句话，
    而人真正需要知道的是"我该去哪拿这个数"。
    """
    reason = bl.live_block_reason(bl.SOURCE_SELLERSPRITE)
    assert reason
    assert "卖家精灵" in reason

    # 合法来源不该被说成有问题
    assert bl.live_block_reason(bl.SOURCE_MANUAL) is None
    assert bl.live_block_reason(bl.SOURCE_SP_API) is None
    # 没标来源的历史数据不算脏：它只是不知道，不该被当成"错的"
    assert bl.live_block_reason(None) is None


@pytest.mark.parametrize(
    "origin,blocked",
    [
        (bl.ORIGIN_SP_API, False),
        (bl.ORIGIN_MANUAL, False),
        (bl.ORIGIN_SELLERSPRITE, True),
        (bl.ORIGIN_SIMULATED, True),
        (None, False),
    ],
)
def test_live_field_block_reason(origin, blocked):
    """字段级同一套规则。两层都要拦：历史数据可能只填了 source 没填 field_sources。"""
    reason = bl.live_field_block_reason(origin)
    assert bool(reason) is blocked, (origin, reason)


# ============================================================
# ③ 字段级出身：谁写的就标谁
# ============================================================


def test_update_baseline_marks_written_fields_as_manual(clean_db):
    """走写入函数的人填字段，自动标 `manual`。

    为什么不让调用方自己拼：调用方有四个（能力层、api/server.py、
    scripts/set_baseline.py、pilot 脚本）。少标一个地方，那批数在面板上
    就显示成「来源未标注」——比标错更难查，因为标错还能顺着错的值追下去。
    """
    tenant = f"{TS}-mark"

    out = pool.update_baseline(
        tenant, asin="B0GATE0004", current_price=21.5, break_even_price=10.58
    )
    assert out["ok"] is True, out

    sources = _row(tenant).field_sources
    assert sources["current_price"] == bl.ORIGIN_MANUAL
    assert sources["break_even_price"] == bl.ORIGIN_MANUAL
    # 没写的字段不该被顺手标上来源：那会让「缺」看起来像「有」
    assert "rating" not in sources


def test_update_baseline_keeps_untouched_field_origins(clean_db):
    """二次写入只改本次碰过的格子，别把上一批的出身抹平。

    抹平的后果：先用 SP-API 同步一次（标 sp_api），再手工改一个售价，
    其余字段的 sp_api 标记全被清掉，面板上整行变成「手工录入」——
    而事实是其中大部分数还是后台来的。
    """
    tenant = f"{TS}-keep"
    pool.update_baseline(
        tenant,
        asin="B0GATE0005",
        current_price=19.99,
        field_sources={"current_price": bl.ORIGIN_SP_API},
    )

    pool.update_baseline(tenant, break_even_price=10.58)

    sources = _row(tenant).field_sources
    assert sources["current_price"] == bl.ORIGIN_SP_API, "上一批的出身被抹平了"
    assert sources["break_even_price"] == bl.ORIGIN_MANUAL


# ============================================================
# ④ 自动补全在真实模式下整段不跑
# ============================================================


def test_autofill_does_nothing_in_live_mode(clean_db):
    """`_autofill` 才是「真实页显示反推数据」的真凶，直接钉住它。

    为什么不只测闸门：闸门拦的是**调用方**传 `source=sellersprite_proxy`，
    而 `_autofill` 根本不走调用方——它在 `add_my_product` 里被顺手调用，
    自己构造快照、自己写库，整条路径上一个 `source` 参数都不过。
    """
    tenant = f"{TS}-autofill"
    out = pool.update_baseline(tenant, asin="B0GATE0006", current_price=19.99)
    assert out["ok"] is True

    my_id = _row(tenant).id
    # 真实模式下无论库里有什么、网络通不通，都必须直接返回空。
    # 这条断言不依赖网络：函数第一行就返回了，没有走到 adapter。
    assert pool._autofill(my_id) == []


# ============================================================
# ⑤ SP-API 同步：配置缺失 / 预览 / 价格口径
# ============================================================


class _FakeAdapter:
    """替掉真适配器，让同步逻辑能在没有凭据的环境里被验证。

    只替**取数**这一段（`fetch_self_snapshot`），映射、跳过、落库全部走真代码——
    要验的正是后者。把整段同步都 mock 掉，测的就只剩 mock 自己了。
    """

    configured = True
    region = "na"

    def __init__(self, snap: Optional[SelfSnapshot] = None, missing: Optional[list] = None):
        self._snap = snap
        self._missing = missing or []
        self.configured = not self._missing

    def missing_credentials(self) -> list:
        return list(self._missing)

    async def fetch_self_snapshot(self, asin: str, marketplace: str = "US") -> Any:
        return self._snap


def _install(monkeypatch, adapter: _FakeAdapter) -> None:
    monkeypatch.setattr(self_sync, "SpApiAdapter", lambda: adapter)


def _snap(**kw) -> SelfSnapshot:
    base = dict(asin="B0GATE0007", marketplace="US")
    base.update(kw)
    return SelfSnapshot(**base)


def test_sync_reports_missing_env_names(monkeypatch, clean_db):
    """没配凭据时报的是**环境变量名**，不是内部字段名。

    报 "refresh_token" 让人无从下手；报 `SPAPI_REFRESH_TOKEN` 才能照着去 .env 里加。
    """
    pool.update_baseline(f"{TS}-nocfg", asin="B0GATE0007", current_price=19.99)
    _install(monkeypatch, _FakeAdapter(missing=["refresh_token", "client_id"]))

    out = self_sync.sync(f"{TS}-nocfg")

    assert out["ok"] is False
    assert out["status"] == "not_configured"
    assert "SPAPI_REFRESH_TOKEN" in out["missing_env"]
    assert out["hint"], "缺凭据时要给出补法，否则人只知道失败不知道去哪配"


def test_sync_dry_run_writes_nothing(monkeypatch, clean_db):
    """预览只取数不落库——否则「先看看能拿到什么」等于直接改了库。"""
    tenant = f"{TS}-dry"
    pool.update_baseline(tenant, asin="B0GATE0008", current_price=19.99, rating=4.2)
    before = _row(tenant)

    _install(
        monkeypatch,
        _FakeAdapter(
            _snap(
                available_units=2600,
                daily_units_30d=111.5,
                buybox_price=22.99,
                title="Real Title",
            )
        ),
    )

    out = self_sync.sync(tenant, dry_run=True)

    assert out["ok"] is True and out["status"] == "dry_run", out
    assert out["written"]["current_price"] == 22.99

    after = _row(tenant)
    assert after.current_price == before.current_price, "预览把价格写进库了"
    assert after.fba_available_units is None
    assert after.title is None


def test_sync_writes_only_what_spapi_can_give(monkeypatch, clean_db):
    """落库时：接口有的写、接口给不了的留在原处、逐格出身改 sp_api。"""
    tenant = f"{TS}-write"
    pool.update_baseline(
        tenant,
        asin="B0GATE0009",
        current_price=19.99,
        rating=4.2,
        break_even_price=10.58,
        assumed_fields=["break_even_price"],
    )

    _install(
        monkeypatch,
        _FakeAdapter(
            _snap(
                available_units=2600,
                daily_units_30d=111.5,
                buybox_price=22.99,
                listing_price=24.99,
                title="Real Title",
                image_url="https://example.com/a.jpg",
                category_path=["Home & Kitchen", "Dish Racks"],
                bsr_rank=1850,
            )
        ),
    )

    out = self_sync.sync(tenant)
    assert out["ok"] is True and out["status"] == "synced", out

    row = _row(tenant)
    assert row.fba_available_units == 2600
    assert row.daily_sales_avg == 111.5
    assert row.current_price == 22.99
    assert row.title == "Real Title"

    # 接口结构上给不了的，**保持原值**而不是被覆盖成空。
    # 覆盖成空的后果是「接口跑了一次，锚点反而更差了」，而且没人会想到是这个原因。
    assert row.rating == 4.2
    assert row.break_even_price == 10.58

    # 大类排名拿到了也不写：它和竞品的子类 BSR 不是同一个口径
    assert row.sub_category_rank is None
    assert any("bsr" in n.lower() or "排名" in n for n in out["notes"]), out["notes"]

    # 出身：写进去的改 sp_api，没动的还是 manual
    assert row.field_sources["current_price"] == bl.ORIGIN_SP_API
    assert row.field_sources["rating"] == bl.ORIGIN_MANUAL

    # 被 SP-API 覆盖掉的字段不再是假设值
    assert "break_even_price" in (row.assumed_fields or [])   # 没被覆盖，仍是假设
    assert "current_price" not in (row.assumed_fields or [])

    # 整批来源变成 sp_api——真实模式下这批数确实来自后台
    assert row.source == bl.SOURCE_SP_API


def test_sync_refuses_listing_price_when_buybox_missing(monkeypatch, clean_db):
    """只拿到标价时**不写价格**，留给人确认。

    这个项目已经因为「拿标价顶替到手价」出过一次事故：标价 $24.99 挂 10% 券，
    到手 $22.49，用标价去比会得出方向相反的结论。宁可空着。
    """
    tenant = f"{TS}-listing"
    pool.update_baseline(tenant, asin="B0GATE0010", current_price=19.99)
    _install(monkeypatch, _FakeAdapter(_snap(listing_price=24.99, available_units=100)))

    out = self_sync.sync(tenant)

    assert out["ok"] is True, out
    assert "current_price" not in out["written"]
    skipped = {s["field"]: s["reason"] for s in out["skipped"]}
    assert "current_price" in skipped
    assert "标价" in skipped["current_price"]

    assert _row(tenant).current_price == 19.99, "只拿到标价却把锚点价格改了"


def test_sync_with_nothing_returns_error_and_keeps_values(monkeypatch, clean_db):
    """一个字段都没拿到 → 报错并保持库里的值。

    把「接口挂了」写成「锚点为空」是最难查的一类事故：面板上看起来像
    "锚点被清空了"，实际是接口失败，而下次同步又正常，现场早就没了。
    """
    tenant = f"{TS}-empty"
    pool.update_baseline(tenant, asin="B0GATE0011", current_price=19.99)
    _install(monkeypatch, _FakeAdapter(_snap()))

    out = self_sync.sync(tenant)

    assert out["ok"] is False
    assert out["status"] == "nothing_written", out
    assert _row(tenant).current_price == 19.99


def test_sync_without_anchor_says_so(monkeypatch, clean_db):
    """还没有锚点就同步 → 说清「先建锚点」，而不是抛异常或建个空壳。"""
    _install(monkeypatch, _FakeAdapter(_snap()))

    out = self_sync.sync(f"{TS}-noanchor")

    assert out["ok"] is False
    assert out["status"] == "no_anchor", out
    assert _row(f"{TS}-noanchor") is None
