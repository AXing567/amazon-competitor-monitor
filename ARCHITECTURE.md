# 架构说明

写这份文档的原因：代码会变，但**边界不会随便变**。
搞清楚「谁能干什么、谁不能干什么」，比记住某个函数叫什么重要得多。

---

## 一、分层总览

```
┌──────────────────────────────────────────────────────────┐
│ L1 接入层   app/api/server.py · dashboard.py              │
│            飞书 webhook · REST API · 只读看板             │
├──────────────────────────────────────────────────────────┤
│ L2 Agent 层 app/agent/llm.py · tools.py                   │
│            听懂人话 → 选工具 → 说人话                     │
├──────────────────────────────────────────────────────────┤
│ L3 渠道层   app/channels/                                 │
│            base(契约) · feishu · render · router          │
├──────────────────────────────────────────────────────────┤
│ L4 服务层   app/services/                                 │
│            pool · collector · briefer · scheduler         │
├──────────────────────────────────────────────────────────┤
│ L5 领域层   app/domain/          ★ 纯函数，无副作用        │
│            baseline · tiering · diff · decision           │
├──────────────────────────────────────────────────────────┤
│ L6 适配层   app/adapters/                                 │
│            unified(数据契约) · base(抽象) · sellersprite   │
├──────────────────────────────────────────────────────────┤
│ L7 存储层   app/db.py · models.py                         │
│            SQLAlchemy ORM · 10 张表                       │
└──────────────────────────────────────────────────────────┘
```

### 依赖方向：只能往下，不能往上

```
接入 → Agent → 渠道
  ↓       ↓
  服务 ──────────→ 领域
  ↓                  ↑
适配 ────────────────┘
  ↓
存储
```

**唯一的例外**：`scheduler` 会调 `channels/router`（推日报）。
这是刻意的——定时任务必须能主动发消息。为避免循环依赖，代码里用**延迟导入**实现：

```python
# app/services/scheduler.py
async def job_daily_brief() -> None:
    from app.channels.router import get_router   # 延迟导入，避免循环
    ...
```

---

## 二、每层的边界

边界写成「允许 / 禁止」，比写成职责描述好执行。

### L5 领域层（最重要的一层）

| | |
|---|---|
| **允许** | 计算、判断、打分、排序 |
| **禁止** | 碰数据库、发网络请求、读环境变量、写日志到外部、抛业务异常 |
| **输入** | 普通 Python 对象 / dataclass / Pydantic 模型 |
| **输出** | dataclass / Pydantic 模型 |

为什么这么严：让模型算盈亏平衡点，它会算错而且错得很自信。
把判断锁在纯函数里，才能单测、才能复现、才能在出错时定位到具体一行。

**验证方式**：`tests/test_domain.py` 29 个用例，全部不碰 DB 不联网，跑完 0.1 秒。

### L4 服务层

| | |
|---|---|
| **允许** | 开数据库会话、调适配器、调领域函数、编排多步骤流程 |
| **禁止** | 自己实现业务算法（必须调领域层）、直接拼飞书卡片 JSON（必须走 channels/render） |

服务层是「编排者」，不是「实现者」。发现服务层里出现业务公式，就是分层漏了。

### L6 适配层

| | |
|---|---|
| **允许** | HTTP 请求、字段映射、容错重试、数据质量判定 |
| **禁止** | 做任何业务判断（比如「这个算不算降价」） |
| **硬约束** | 拿不到的字段一律留 `None`，**绝不估算填充**；失败返回 `data_quality="failed"`，不抛异常 |

### L3 渠道层

| | |
|---|---|
| **允许** | 调平台 API、渲染卡片、失败降级 |
| **禁止** | 查数据库、做业务判断 |
| **硬约束** | 发送失败不能影响采集；未配置渠道时降级到 `NullChannel` 写日志 |

### L2 Agent 层

| | |
|---|---|
| **允许** | 理解意图、选工具、调工具、组织语言 |
| **禁止** | 自己算数、自己编数据、绕过工具直接查库 |
| **硬约束** | 工具返回永远是 dict，含 `ok` 字段；异常必须被收成 `ok=false`，绝不把堆栈丢给模型 |

### L1 接入层

| | |
|---|---|
| **允许** | 参数校验、鉴权、转发、渲染页面 |
| **禁止** | 写业务逻辑 |
| **硬约束** | 飞书要求 3 秒内响应，所以 webhook 只做「验证 + 塞后台任务」，绝不在请求里调模型 |

---

## 三、接口契约

### 3.1 数据契约：`UnifiedSnapshot`

位置：`app/adapters/unified.py`

这是全系统的「普通话」。所有平台的数据都必须翻译成它，领域层只认它。

| 字段组 | 关键字段 | 可空 |
|---|---|---|
| 价格 | `price` `original_price` `coupon_value` `net_price` `deal_type` | ✅ 全部可空 |
| 排名 | `rank_main` `rank_sub` `rank_label` | ✅ |
| 销量 | `monthly_units` `monthly_revenue` `daily_units` | ✅ |
| 口碑 | `rating` `review_count` `monthly_review_increment` | ✅ |
| 供给 | `in_stock` `stock_hint` `buybox_owner` `fulfillment_type` | ✅ |
| 内容 | `title` `brand` `image_url` `release_date` `category_path` | ✅ |
| 流量 | `keywords[]` `organic_ratio` `sponsored_ratio` `traffic_keywords_total` | ✅ |
| 元信息 | `data_quality` `source` `captured_at` `errors[]` | ❌ 必须填 |

**契约要点**

1. **所有业务字段可空**。空就是空，不许用 0 或平均值冒充。
2. **`data_quality` 是必填**：`full` / `partial` / `estimated` / `failed`。
   下游据此决定话术——`partial` 时结论要加「仅供参考」。
3. **`errors` 保留原始错误**，便于排查是哪个接口挂了。
4. 附一个方法 `core_keyword_weights(coverage=0.8)`，返回累计贡献 80% 流量的头部词
   `{keyword: traffic_share}`，这是加权词重合度的输入。

### 3.2 适配器契约：`DataAdapter`

位置：`app/adapters/base.py`

```python
class DataAdapter(ABC):
    name: str

    @abstractmethod
    async def fetch_snapshot(self, asin: str, marketplace: str = "US") -> UnifiedSnapshot:
        """失败时返回 data_quality='failed' 的对象，不抛异常。"""

    @abstractmethod
    async def fetch_keywords(self, asin: str, marketplace: str = "US") -> list[dict]:
        """只取关键词（省成本，用于算重合度）。"""

    async def health(self) -> dict: ...
```

**加一个平台 = 写一个子类**。领域层、服务层、Agent 层一行都不用改。

### 3.3 领域函数契约

全部是**关键字参数 + 关键字返回**，没有位置参数，避免调用时顺序搞错。

```python
# 步骤 0：锚点体检
baseline.assess(my_product) -> BaselineAssessment
baseline.prefill_from_snapshot(my_product, snap) -> list[str]        # 返回被填的字段名
baseline.estimate_break_even_price(*, price, margin_rate) -> float | None

# 步骤 1：四同法则分层
tiering.judge_tier(*, my_price, my_core_keywords, my_category_path,
                   my_daily_units, comp, functional_match=None) -> TieringResult
tiering.calc_price_ratio(my_price, comp_price) -> float | None
tiering.calc_keyword_overlap(my_core, comp_keywords) -> float | None

# 步骤 2：差异判定
DiffEngine(phase="warmup").compare(prev, curr, *, tier="C",
                                   my_price=None, oos_confirmed=True) -> list[SignalDraft]

# 步骤 3：决策
decision.judge_price_follow(*, competitor_new_price, my_current_price,
                            my_break_even_price, my_unit_profit_now, my_daily_units,
                            competitor_is_clearance=False, market_players=1,
                            my_dos=None) -> DecisionResult
decision.judge_clearance(*, price_drop_pct=None, stock_declining=None,
                         no_restock=None, ads_stopped=None, ...) -> DecisionResult
decision.build_out_of_stock_decision(*, competitor_asin, my_dos,
                                     is_low_stock=False) -> DecisionResult
decision.calc_break_even_units(*, current_price, current_daily_units,
                               unit_profit_now, new_price, unit_cost) -> dict
```

**三条通用规则**

1. **缺数据返回 `None`，不返回默认值**。`calc_price_ratio(None, 25)` → `None`，不是 `1.0`。
2. **未知信号不计分**。`judge_clearance` 里拿不到的信号不参与打分，但会记进 `metrics["unknowns"]`。
3. **结论必须带理由**。所有返回都含 `reasons`，没有理由的结论等于没有结论。

### 3.4 领域返回结构

```python
@dataclass
class SignalDraft:              # 一条待落库的信号
    category: str               # price / supply / rank / reputation / content
    signal_type: str            # price_drop / out_of_stock / ...
    level: str                  # P0 / P1 / P2
    description: str            # 给人看的一句话
    before_value: str | None
    after_value: str | None
    change_pct: float | None    # 带符号
    mainline: str               # 归属哪条判断主线，供步骤 3 路由
    meta: dict                  # 附加上下文，不落库

@dataclass
class DecisionResult:           # 一个判断结论
    decision: str               # attack / defend / learn / no_action
    headline: str               # 一句话结论
    reasons: list[str]          # 为什么这么判
    actions: list[str]          # 然后呢（必须有）
    metrics: dict               # 中间计算过程，便于复核
    verify_days: int            # 多少天后回看效果
    confidence: float | None    # 0~1，确定性
```

`confidence` 不是装饰。低于 0.6 的结论，`briefer` 会在推送里加「仅供参考」。

### 3.5 服务层契约

```python
# 采集
collector.acollect_competitor(competitor_id) -> CollectResult
collector.collect_all(tenant_id) -> list[CollectResult]
collector.acollect_competitor_by_asin(tenant_id, asin) -> dict
collector.row_to_unified(row, marketplace) -> UnifiedSnapshot      # DB 行 → 统一模型
collector.resolve_season_phase(session, my_product) -> str         # warmup/blast/cooldown

@dataclass
class CollectResult:
    competitor_id: int
    asin: str
    ok: bool
    data_quality: str
    signals: list[SignalDraft]
    errors: list[str]
    is_first_snapshot: bool        # 首次采集没可比对象，不产生信号

# 简报
briefer.build_daily_brief(tenant_id=None, *, hours=24, include_p1=True) -> DailyBrief
briefer.build_alerts_since(tenant_id, minutes=90) -> list[BriefItem]
briefer.build_alert_text(item) -> str

# 竞品池
pool.add_competitor(tenant_id, asin, *, functional_match=None, force=False) -> dict
pool.list_competitors(tenant_id, tier=None) -> list[dict]
pool.get_competitor_detail(tenant_id, asin, history=14) -> dict
pool.update_baseline(tenant_id, **fields) -> dict
```

**关键契约**：`pool.*` 和 `briefer.*` 的返回值都是 **JSON 友好的 dict**，
因为它们的下游是「要发给模型的工具结果」。让 dict 一路到底，省一次转换就少一处出错。

### 3.6 Agent 工具契约

位置：`app/agent/tools.py`

```python
TOOL_SCHEMAS: list[dict]                    # OpenAI/DeepSeek function calling 格式
execute_tool(name: str, args: dict) -> dict # 统一入口
tool_names() -> list[str]
```

**返回约定（模型依赖这个约定，不能破）**

| 场景 | 返回 |
|---|---|
| 成功 | `{"ok": True, ...业务字段}` |
| 业务失败 | `{"ok": False, "error": "人话原因", "hint": "怎么办"}` |
| 异常 | `{"ok": False, "error": "TypeError: ..."}`（异常被捕获，不抛给模型） |

**为什么异常也要收成 dict**：模型看到堆栈会进入「猜」的模式，
不如明确告诉它「这条路不通」，它反而会老老实实换个方式或直接告诉用户。

### 3.7 渠道契约：`Channel`

位置：`app/channels/base.py`

```python
class Channel(ABC):
    name: str

    @abstractmethod
    async def send_text(self, to: str, text: str) -> bool: ...
    @abstractmethod
    async def send_card(self, to: str, card: dict) -> bool: ...
    async def reply_text(self, message_id: str, text: str) -> bool: ...
    @abstractmethod
    async def health(self) -> dict: ...
```

**加钉钉 = 写一个子类 + 在 `router._channel()` 注册一行**，业务层零改动。

降级链：`send_card 失败 → send_text`；渠道未配置 → `NullChannel`（写日志不报错）。

### 3.8 REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务 / 调度 / 渠道 / 数据源 / LLM 全量自检 |
| GET | `/api/brief?hours=24` | 日报（含 `text` 字段，纯文本版） |
| GET | `/api/competitors?tier=S` | 竞品池 |
| POST | `/api/competitors` | 加竞品 `{asin, force}` |
| POST | `/api/baseline` | 设锚点 |
| POST | `/api/collect` | 立刻采集 `{asin?}` |
| POST | `/api/chat` | Agent 对话 `{message, session_id}` |
| POST | `/feishu/webhook` | 飞书事件订阅 |
| POST | `/api/run/daily-brief` | 手动推一次日报（测渠道） |
| POST | `/api/run/p0-check` | 手动推一次 P0（测告警） |

---

## 四、三条数据流

### 流 1：定时采集（静默，不打扰）

```
APScheduler (1h / 旺季 15min)
  → collector.collect_all(tenant_id)
      → SellerSpriteAdapter.fetch_snapshot(asin)      [5 接口并发，单失败不影响其他]
      → 落 Snapshot
      → DiffEngine.compare(prev, curr)                [纯函数]
      → 冷却去重（P0: 4h / P1: 12h / P2: 48h）
      → 落 Signal
  → 若存在 P0 → push_p0_alerts()                      [唯一的打断通道]
```

**关键闸门**：`diff.should_push(signals, notify_level)`。
采集可以一小时一次甚至 15 分钟一次，但推送必须克制。

### 流 2：日报

```
APScheduler (09:00)
  → briefer.build_daily_brief(hours=24)
      → 读 Signal + Competitor + MyProduct
      → 每条信号走 _attach_action() → 调 decision.* 产出动作
      → 断货优先级覆盖：同竞品有断货 → 价格信号让路
  → render_daily_card(brief)  → Channel.send_card()
  → 失败则降级 send_text(brief.to_text())
```

### 流 3：Agent 对话

```
飞书事件 → /feishu/webhook（3 秒内返回 200，塞后台任务）
  → llm.achat(text, session_id)
      → DeepSeek Function Calling
      → execute_tool(name, args) → 服务层 → 领域层
      → 最多 5 轮工具调用
  → router.reply(message_id, reply)
```

**为什么必须异步**：飞书 3 秒超时。同步调模型 + 工具几乎必然超时，
飞书会重试，导致重复回复。所以 webhook 只做去重 + 塞任务，立刻返回。

---

## 五、扩展点

| 要加什么 | 改哪里 | 要动几处 |
|---|---|---|
| 新平台（Walmart） | `adapters/` 加子类 | 1 个文件 |
| 新渠道（钉钉） | `channels/` 加子类 + `router._channel()` 注册 | 2 处 |
| 新信号类型 | `diff.py` 加一个 `_diff_xxx` + `RULES` 阈值 | 1 个文件 |
| 新决策算法 | `decision.py` 加纯函数 | 1 个文件 |
| 新 Agent 能力 | `tools.py` 加 schema + 实现 + 注册 | 1 个文件 |
| 调阈值 | 各领域文件顶部常量 | 1 行 |

---

## 六、三条不能破的红线

1. **领域层不许有副作用**。一旦它开始查库，就会变成「只有连上数据库才能测」，
   然后所有测试都会退化成人肉点页面。
2. **判断不许交给模型**。模型可以决定「调哪个工具」，但不能决定「该不该跟价」。
3. **采集失败不许产生信号**。宁可不报，也不能拿 `None` 去比出假信号。
   误报比漏报更快摧毁信任——漏报只是少赚，误报会让你做错决策。
