# 架构说明

写这份文档的原因：代码会变，但**边界不会随便变**。
搞清楚「谁能干什么、谁不能干什么」，比记住某个函数叫什么重要得多。

这一版把原来的七层收敛成**六层**。收敛的原因不是"层太多不好看"，而是旧的分法里
「接口层」与「渠道层」都在干"和外面说话"这件事，「智能体层」又同时承担了
"和外面说话"和"调用工具"两件事——结果是加一个入口要改三个地方，而且没人说得清
"面板该放哪一层"。现在按**职责**分：说话的（L5）、选能力的（L4）、编排的（L3）、
判断的（L2）、取数的（L1）、存的（L0）。

---

## 一、分层总览

```
┌────────────────────────────────────────────────────────────────────┐
│ L5 交互层                                                          │
│   L5A 交互·推送   app/channels/ + app/agent/ + 飞书 webhook        │
│                   发卡片、发文本、听懂人话 → 选能力 → 说人话        │
│   L5B 交互·面板   app/web/ + app/api/routes/                       │
│                   六个视图、单文件 HTML、REST 出口                  │
├────────────────────────────────────────────────────────────────────┤
│ L4 能力层   app/capabilities/                                      │
│             37 个能力 + 唯一入口 call(name, *, ctx, **kwargs)       │
│             参数校验 · 身份携带 · 返回包装 —— 不写业务判断          │
├────────────────────────────────────────────────────────────────────┤
│ L3 编排层   app/services/                                          │
│             pool · collector · briefer · scheduler · settings_store │
│             binding · identity · action_log · health · simulator    │
├────────────────────────────────────────────────────────────────────┤
│ L2 认知层   app/domain/            ★ 纯函数：无 IO、无时钟、无环境   │
│             baseline · tiering · diff · decision · thresholds       │
├────────────────────────────────────────────────────────────────────┤
│ L1 取数层   app/adapters/                                          │
│             unified(数据契约) · base(抽象) · sellersprite           │
├────────────────────────────────────────────────────────────────────┤
│ L0 存储层   app/db.py · models.py · migrations.py                   │
│             SQLAlchemy ORM · 10 张表 · 幂等迁移（DDL 也在事务里）   │
└────────────────────────────────────────────────────────────────────┘
```

### 每层归谁（按文件看）

| 层 | 目录 / 文件 | 一句话 |
|---|---|---|
| L5A | `app/channels/` | 渠道契约 `base` 、卡片渲染 `render`、分发 `router`、飞书实现 |
| L5A | `app/agent/` | `llm.py` 听懂→选能力→说人话；`tools.py` 薄适配层（schema + 转发） |
| L5A | `app/api/server.py` 的 `/feishu/webhook`、`/api/chat` | 对话入口；只做验证 + 塞后台 |
| L5B | `app/web/` | `layout` 外壳、`views/` 六视图、`panel.py` 拼装 |
| L5B | `app/api/routes/` | `panel` / `config` / `capabilities` / `identity` 四组 REST 出口 |
| L5B | `app/api/deps.py` | `X-User-Id` → `CapContext`，**全项目唯一解析身份的地方** |
| L4 | `app/capabilities/` | `types` `queries` `writes` `judgments` + 注册表与 `call()` |
| L3 | `app/services/` | 开会话、调适配器、调领域、编排多步、写库 |
| L2 | `app/domain/` | 判断与计算；`diff` 判信号、`tiering` 判分层、`decision` 判动作 |
| L1 | `app/adapters/` | HTTP 取数 + 字段映射；拿不到就留 `None` |
| L0 | `app/db.py` `models.py` `migrations.py` | 引擎/会话、表定义、幂等迁移 |

### 依赖方向：只能往下，不能往上

```
L5A ─┐
     ├─→ L4 能力层 ─→ L3 编排层 ─→ L2 认知层
L5B ─┘                  ↓              ↑
                        L1 取数层 ─────┘
                        ↓
                        L0 存储层
```

两条**刻意的例外**，都写了注释，别"顺手修掉"：

1. `services/scheduler.py` 会调 `channels/router`（推日报）。
   定时任务必须能主动发消息；为避免循环依赖用**延迟导入**：
   ```python
   # app/services/scheduler.py
   async def job_daily_brief() -> None:
       from app.channels.router import get_router   # 延迟导入，避免循环
       ...
   ```
2. `capabilities` 里有几处**只读穿透到 L0**（如 `_my_product_row`）。
   它们是为了让"锚点体检"能在一次会话里拿到 ORM 对象。这是**已知分层债**，
   修法是等 `pool` 拆分后下沉成 `pool.assess_my_product()`——记在交付报告的遗留里。

---

## 二、每层的边界

边界写成「允许 / 禁止」，比写成职责描述好执行。

### L5A 交互·推送（`channels/` `agent/`）

| | |
|---|---|
| **允许** | 调平台 API、渲染卡片、失败降级、**选**能力并转述结果、维护会话上下文 |
| **禁止** | 查数据库、做业务判断、自己算数、自己补数据、绕过能力层直调服务层 |
| **硬约束** | ① 发送失败不能影响采集；未配置渠道时降级 `NullChannel` 写日志 ② 工具返回永远是带 `ok` 的 dict，异常被收成 `ok:false`，**绝不把堆栈丢给模型** ③ 飞书要求 3 秒内响应，webhook 只做「验证 + 去重 + 塞后台任务」 |

`agent/tools.py` 现在只剩 `TOOL_SCHEMAS` + 一次 `safe_call` 转发。
它**不再持有任何实现**——这是 T04 那次重构的全部意义：
"面板显示的竞品数"和"Agent 回答的竞品数"不可能再分叉。

### L5B 交互·面板（`web/` `api/routes/`）

| | |
|---|---|
| **允许** | 把已有结论摆出来、把配置收进去、格式化（`money`/`pct`/`ago`）、展示错误与 `hint` |
| **禁止** | 写判定逻辑（加权、阈值比较、"这个算不算异常"）、直连数据库、`import app.services`、引用任何外部资源（CDN / 字体 / `<script src>`） |
| **硬约束** | ① 产物是**单文件 HTML**（断网必须完整渲染） ② **单 tab 故障隔离**：一个接口挂了只有对应视图显示错误卡片 ③ 所有请求走 `/api/panel/*` 或 `/api/cap/*` |

为什么禁止前端写判定：判据一旦在前端复制一份，就会和 L2 的那份在下次改阈值时悄悄分叉。
分叉的结果是"面板说有 5 条异常、飞书只推了 3 条"——这种账最后会算到模型头上。

### L4 能力层（`capabilities/`）

| | |
|---|---|
| **允许** | 参数校验与类型转换、装配上下文、调下游、包装返回、参数自描述（给面板渲染表单） |
| **禁止** | 写业务判断、直接写 SQL、在模块里存"当前用户"之类的可变全局状态 |
| **硬约束** | ① **唯一入口** `call(name, *, ctx, **kwargs)` ② 身份**入参**携带（`CapContext`），不是进程内单例 ③ 业务失败一律 `200 + {"ok": false, ...}`；只有"能力不存在/无权限"才是 404/403 ④ 启动期硬断言注册表（重名、参数类型、scope、label 非空） |

为什么身份必须是入参：面板上切用户之后，动作记到谁名下、阈值按谁的覆盖层解析，取决于"当前用户"。
用模块级单例会同时踩两个坑——多 worker 下进程内状态分叉（改完自己阈值，下一个请求可能落到别人的上下文），
以及出问题时无法还原"这个请求当时认为是哪个用户"。

### L3 编排层（`services/`）

| | |
|---|---|
| **允许** | 开数据库会话、调适配器、调领域函数、编排多步骤流程、写库、记动作日志 |
| **禁止** | 自己实现业务算法（必须调 L2）、直接拼飞书卡片 JSON（必须走 `channels/render`）、在 web 层能算的东西这里不算 |
| **硬约束** | 返回给上层的都是 **JSON 友好 dict**；失败返回 `{"ok": false, "error", "hint"}`，不抛业务异常 |

服务层是「编排者」，不是「实现者」。发现服务层里出现业务公式，就是分层漏了。

### L2 认知层（`domain/`）—— 最重要的一层

| | |
|---|---|
| **允许** | 计算、判断、打分、排序 |
| **禁止** | 碰数据库、发网络请求、读环境变量、读时钟（`datetime.now()`）、写日志到外部、抛业务异常 |
| **输入** | 普通 Python 对象 / dataclass / pydantic 模型 |
| **输出** | dataclass / pydantic 模型 |
| **硬约束** | 缺数据返回 `None`，**不返回默认值**；结论必须带 `reasons` |

为什么连时钟都不许读：`DiffEngine` 的金丝雀一变，同一个输入就会在不同时刻给出不同结论，
那样的函数既不能单测也不能复现。时间由 L3 传进来。

**验证方式**：`tests/test_domain.py` 31 个用例，全部不碰 DB 不联网，秒级跑完。

### L1 取数层（`adapters/`）

| | |
|---|---|
| **允许** | HTTP 请求、字段映射、容错重试、数据质量判定 |
| **禁止** | 做任何业务判断（比如"这个算不算降价"） |
| **硬约束** | 拿不到的字段一律留 `None`，**绝不估算填充**；失败返回 `data_quality="failed"`，不抛异常 |

**加一个平台 = 写一个子类**。L2 / L3 / L4 / L5 一行都不用改。

### L0 存储层（`db.py` `models.py` `migrations.py`）

| | |
|---|---|
| **允许** | 引擎与会话管理、表定义、索引、幂等迁移 |
| **禁止** | 业务逻辑、跨表业务计算、在迁移里改数据（只补结构） |
| **硬约束** | ① 迁移**必须幂等**：靠 `PRAGMA table_info` 判断，不用 `try/except` ② 迁移**必须原子**：显式 `BEGIN IMMEDIATE` → DDL → `COMMIT`，失败 `ROLLBACK` 并原样抛出 ③ 缺列/缺表就跳过并记进 `skipped`，**不许静默** |

为什么幂等不能用 `try/except`：`ALTER TABLE ADD COLUMN` 在列已存在时报的错和
"表不存在"报的错是同一个异常类型，靠异常分不清"已经好了"和"根本没做"，
第二次启动就会把一次真失败当成已完成。

为什么原子性要靠 DBAPI：pysqlite 只在 DML 前隐式 `BEGIN`，**DDL 走 autocommit**，
所以 `engine.begin()` 包不住 `ALTER TABLE`。不显式开事务的话，一个失败会留下
半迁移状态，而下一次启动又因为"列已存在"而跳过——迁移从此自我锁定。

---

## 三、接口契约

### 3.1 数据契约：`UnifiedSnapshot`（**只描述竞品**）

位置：`app/adapters/unified.py`

这是全系统的「普通话」。所有**平台上的别人**的数据都必须翻译成它，认知层只认它。

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
2. **`data_quality` 是必填**：`full` / `partial` / `estimated` / `failed`。下游据此决定话术。
3. **`errors` 保留原始错误**，便于排查是哪个接口挂了。
4. `core_keyword_weights(coverage=0.8)` 返回累计贡献 80% 流量的头部词 `{keyword: traffic_share}`。

### 3.2 自家数据**不走** `UnifiedSnapshot`

位置：`app/models.py` 的 `MyProduct` + `domain/baseline.py` 的 `assess()`

自家产品的数据结构是**另一套**，理由不是"懒得统一"：

1. **语义冲突**。竞品的 `net_price` 是"页面上看到的到手价"；自家的售价背后还要有
   保本价、目标毛利率、可售天数。把这些塞进同一个结构，`price` 就变成一份数据两种含义。
2. **空值含义不同**。竞品 `net_price=None` 表示"当轮价格不可知"；自家没填售价表示
   "还没配置"。同一个 `None` 在两条链上要做完全不同的事，共用一个结构必定有人搞错。
3. **归属不同**。竞品会被归档、会被换绑；自家锚点只有一个，是判断的基准。
   混在一张结构里，"这条是别人还是自己"要靠调用方记得，而调用方一定会忘。

所以：`MyProduct` → `baseline.assess()` → 能力边界（`can_judge_price_follow` 等），
竞品数据走 `UnifiedSnapshot` → `DiffEngine`。两条链在 L2 里**只通过显式参数**相遇
（`my_price=` / `my_dos=`），不通过共享对象。

### 3.3 适配器契约：`DataAdapter`

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

### 3.4 认知层函数契约

全部是**关键字参数 + 关键字返回**，没有位置参数，避免调用时顺序搞错。

```python
# 步骤 0：锚点体检
baseline.assess(my_product) -> BaselineAssessment
baseline.prefill_from_snapshot(my_product, snap) -> list[str]        # 返回被填的字段名
baseline.estimate_break_even_price(*, price, margin_rate) -> float | None

# 步骤 1：四同法则分层（证据不足 → unknown，不是 C）
tiering.judge_tier(*, my_price, my_core_keywords, my_category_path,
                   my_daily_units, comp, functional_match=None) -> TieringResult
                   # TieringResult.decidable_dims / .insufficient
tiering.calc_price_ratio(my_price, comp_price) -> float | None
tiering.calc_keyword_overlap(my_core, comp_keywords) -> float | None

# 步骤 2：差异判定
DiffEngine(phase="warmup", thresholds=None).compare(
    prev, curr, *, tier="C", my_price=None, oos_confirmed=True) -> list[SignalDraft]

# 步骤 3：决策
decision.judge_price_follow(*, competitor_new_price, my_current_price,
                            my_break_even_price, my_unit_profit_now, my_daily_units,
                            competitor_is_clearance=False, market_players=1,
                            my_dos=None) -> DecisionResult
decision.judge_clearance(*, price_drop_pct=None, stock_declining=None, ...) -> DecisionResult
decision.build_out_of_stock_decision(*, competitor_asin, my_dos,
                                     is_low_stock=False) -> DecisionResult
decision.calc_break_even_units(*, current_price, current_daily_units,
                               unit_profit_now, new_price, unit_cost) -> dict
```

**三条通用规则**

1. **缺数据返回 `None`，不返回默认值**。`calc_price_ratio(None, 25)` → `None`，不是 `1.0`。
2. **未知信号不计分**。`judge_clearance` 里拿不到的信号不参与打分，但会记进 `metrics["unknowns"]`。
3. **结论必须带理由**。所有返回都含 `reasons`，没有理由的结论等于没有结论。

`judge_tier` 的补充约定：返回 5 个值 `S/A/B/C/unknown`。
`unknown` = **证据不足，暂不判定**（`decidable_dims < 2` 或 `insufficient=True`），
不是"不合格"、也不是 C 级的别名。拿不到证据时判成一个等级，
等于用"没看到"冒充"看到了"——这是这套系统里最贵的一类错误。

### 3.5 阈值契约：`THRESHOLD_SPEC` 是唯一真相

位置：`app/domain/thresholds.py`（被 L0/L2/L4 共用）

```python
THRESHOLD_SPEC: dict[str, dict[str, ParamSpec]]   # {group: {key: spec}}
DiffThresholds(**values)                          # 默认值来自 SPEC
validate(group, values) -> list[str]               # 范围 + 类型，中文人话
spec_groups() -> list[dict]                        # 给面板渲染表单
```

- **加一个可调参数 = 改 SPEC 一处**。面板表单由 `spec_groups()` 驱动，前端不认识任何字段名。
- **三层继承**：`company`（默认）→ `user`（个人）→ `product`（产品），稀疏覆盖，未提交的键不动。
- **版本 append-only**：每次保存追加一条；**回滚也是追加一条新版本**，历史不动。
  所以"谁在什么时候把它改回去了"永远有痕迹。
- 本轮**只开放两层**（company / product）：个人层在生产里不生效——定时采集与飞书推送
  都是"系统行为，没有自然人"，让用户在面板上改一层不影响推送的值等于骗人。

### 3.6 能力契约

位置：`app/capabilities/__init__.py`

```python
call(name: str, *, ctx: CapContext, **kwargs) -> dict        # 唯一入口
safe_call(name: str, *, ctx: CapContext, **kwargs) -> dict   # 不抛版（Agent 用）
list_capabilities(*, scope=None, group=None) -> list[Capability]
to_schema(name) -> dict                                      # 面板/OpenAPI 自描述
```

**返回约定（所有入口依赖它，不能破）**

| 场景 | 返回 | HTTP |
|---|---|---|
| 成功 | `{"ok": True, ...业务字段}` | 200 |
| 业务失败 | `{"ok": False, "error": "人话原因", "hint": "怎么办"}` | 200 |
| 参数不合法 | `{"ok": False, "error": "...", "error_code": "invalid_params"}` | 200 |
| 能力不存在 | 抛 `CapabilityError(code="not_found")` | 404 |
| 无权限（`agent_only` 被面板调用） | 抛 `CapabilityError(code="forbidden")` | 403 |

为什么异常也要收成 dict：模型看到堆栈会进入「猜」的模式；
不如明确告诉它「这条路不通」，它反而会老实说"这个我查不到"。

`trigger` 由来源决定（`TRIGGER_BY_SOURCE`）：`panel`→`panel`、`agent`→`agent`、
`feishu_card`→`agent`、`system`→`schedule`。`agent` 单独成一档而不是并进 `manual`：
健康页要能回答"这次采集是人点的，还是模型自己决定采的"，两者的失败模式完全不同。

### 3.7 渠道契约：`Channel`

位置：`app/channels/base.py`

```python
class Channel(ABC):
    name: str
    async def send_text(self, to: str, text: str) -> bool: ...
    async def send_card(self, to: str, card: dict) -> bool: ...
    async def reply_text(self, message_id: str, text: str) -> bool: ...
    async def health(self) -> dict: ...
```

**加钉钉 = 写一个子类 + 在 `router._channel()` 注册一行**，业务层零改动。
降级链：`send_card 失败 → send_text`；渠道未配置 → `NullChannel`（写日志不报错）。

### 3.8 面板契约

- **产物**：`render_panel()` 返回一个完整 HTML（CSS/JS 全内联，零外链），`lru_cache` 一份。
- **数据面**：只用 `/api/panel/*` 与 `/api/cap/*`，不直连库、不 import `app.services`。
- **故障隔离**：`safeLoad(name)` 是唯一加载入口，异常只写进该视图自己的 `.view-body`。
- **不鉴权**：单租户自用。`X-User-Id` 的语义是"以谁的身份操作"，不是"我是谁的证明"。
  **公网部署前必须加口令层**（见 README 顶部）。

### 3.9 REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/panel` | 面板（六视图，单文件 HTML） |
| GET | `/health` | 服务 / 调度 / 渠道 / 数据源 / LLM 全量自检 |
| GET | `/api/dataset` | 当前数据模式（live/mock）与两个库的概况 |
| POST | `/api/dataset/mock/rebuild` | 重建 Mock 数据集 |
| GET | `/api/panel/context` | 当前租户、操作人、自家产品（产品层阈值要用 `my_product_id`） |
| GET | `/api/panel/board` | 看板：四个数字卡 + 竞品表（含每个竞品的最近一次变化） |
| GET | `/api/panel/competitors/{asin}/changes` | 单个竞品的四条时间线（快照/信号/判断/动作） |
| GET | `/api/panel/pool` | 池子清单（可多选状态过滤） |
| POST | `/api/panel/pool/{asin}/status` | 改状态：active / paused / archived |
| GET | `/api/panel/actions` | 动作时间线（默认只看 `X-User-Id` 本人，`user_id=all` 看全团队） |
| POST | `/api/panel/actions` | 手工补记一条动作（兜底入口） |
| GET | `/api/panel/health` | 数据健康五块（一次打包） |
| GET | `/api/config/thresholds/schema` | 阈值参数字典（面板表单的唯一来源） |
| GET | `/api/config/thresholds` | 生效值 + 每键来源层 + 层版本 |
| POST | `/api/config/thresholds` | 保存一组稀疏覆盖 |
| GET | `/api/config/thresholds/versions` | 版本历史 |
| POST | `/api/config/thresholds/rollback` | 回滚（追加新版本） |
| POST | `/api/config/thresholds/simulate` | 历史快照试算（dry-run，零写入） |
| GET | `/api/config/thresholds/explain` | 当前阈值的人话说明 |
| GET | `/api/cap` | 能力清单（37 个，自描述） |
| POST | `/api/cap/{name}` | 调用任意 `shared` 能力 |
| GET | `/api/users` `/api/bindings` | 用户 / 身份 / 绑定 |
| POST | `/api/actions/callback` | 飞书卡片按钮回调（未配密钥 → fail-closed） |
| GET | `/api/brief` `/api/competitors` … | **老端点，保留不动**：外部脚本还在用 |
| POST | `/feishu/webhook` | 飞书事件订阅 |
| POST | `/api/run/daily-brief` `/api/run/p0-check` | 手动触发（测渠道用） |

---

## 四、三条数据流

### 流 1：定时采集（静默，不打扰）

```
APScheduler (1h / 旺季 15min)
  → collector.collect_all(tenant_id)
      → SellerSpriteAdapter.fetch_snapshot(asin)      [5 接口并发，单失败不影响其他]
      → 落 Snapshot + CollectRun（成功与失败都写，健康页要靠它）
      → DiffEngine.compare(prev, curr)                [纯函数]
      → 冷却去重（P0: 4h / P1: 12h / P2: 48h）
      → 落 Signal
  → 若存在 P0 → push_p0_alerts()                      [唯一的打断通道]
```

**关键闸门**：`diff.should_push(signals, notify_level)`。
采集可以一小时一次甚至 15 分钟一次，但推送必须克制——多打扰会让人开始忽略推送。

### 流 2：日报

```
APScheduler (09:00)
  → briefer.build_daily_brief(hours=24, user_id=...)
      → 读 Signal + Competitor + MyProduct
      → 每条信号走 _attach_action() → 调 decision.* 产出动作
      → 断货优先级覆盖：同竞品有断货 → 价格信号让路
  → render_daily_card(brief)  → Channel.send_card()
  → 失败则降级 send_text(brief.to_text())
```

`user_id` 本轮**不参与任何计算**，只是伏笔：下一轮做"10 个人各按自己的阈值收不同日报"时，
调用方签名不用再改一遍。

### 流 3：交互（两个入口，同一条实现）

```
飞书对话                                          管理面板
  /feishu/webhook（3 秒内 200，塞后台）             app/web 单文件 HTML
    → llm.achat(text, session_id)                     → api('/api/panel/...')
        → DeepSeek Function Calling                   → 后端 resolve_ctx()
        → execute_tool(name, args)                        → CapContext(source="panel")
            → safe_call(name, ctx)  ────────┬──────────→ safe_call(name, ctx)
                                            │
                                     L4 能力层（同一份实现）
                                            │
                                  L3 编排 → L2 认知 → L1 取数
```

两个入口**唯一的差别**是 `ctx.source`（`agent` / `panel`）。
这个差别是显式可见的：它决定 `collect_runs.trigger` 与 `actions.source`，
所以"这条动作是模型替你记的还是你亲手记的"永远分得清。

为什么 webhook 必须异步：飞书 3 秒超时。同步调模型 + 工具几乎必然超时，
飞书会重试，导致重复回复。所以只做去重 + 塞任务，立刻返回。

---

## 五、扩展点

| 要加什么 | 改哪里 | 要动几处 |
|---|---|---|
| 新平台（Walmart） | `adapters/` 加子类 | 1 个文件 |
| 新渠道（钉钉） | `channels/` 加子类 + `router._channel()` 注册 | 2 处 |
| 新信号类型 | `diff.py` 加一个 `_diff_xxx` + `SPEC` 阈值 | 2 处（阈值也要进 SPEC） |
| 新决策算法 | `decision.py` 加纯函数 | 1 个文件 |
| 新能力（面板+Agent 同时可用） | `capabilities/` 加 `Capability` + handler | 1 处（两个入口自动都有） |
| 新面板视图 | `web/views/` 加一个文件 + `VIEWS` 加一行 | 2 处 |
| 调阈值 | 面板「阈值配置」页（或直接改 SPEC 默认值） | 0 行代码 |

---

## 六、三条不能破的红线

### 1. L2 必须是纯函数

不许查库、不许联网、不许读时钟、不许读环境变量。
一旦它开始查库，就会变成"只有连上数据库才能测"，然后所有测试都会退化成人肉点页面；
一旦它读时钟，同一个输入就会在不同时刻给出不同结论，那样的函数既不能单测也不能复现。

**检验方法**：新写的 `domain/` 函数，如果单测里必须 `mock` 任何东西才能跑，那就是漏了。

### 2. 自家数据不共用结构（`UnifiedSnapshot` 只描述竞品）

竞品数据（`UnifiedSnapshot`）与自家数据（`MyProduct` + `baseline.assess()`）是两套结构，
只通过 L2 的显式参数（`my_price=` / `my_dos=`）相遇。

共用的代价不是"多写几个字段"，而是**空值语义被混掉**：
竞品的 `net_price=None` 是"当轮价格不可知"，自家的"没填售价"是"还没配置"。
同一个 `None` 在两条链上要做完全不同的事——共用结构必定有人在某一侧写错，
而错法不会报错，只会让结论悄悄偏掉。

### 3. 大模型只转述，不判断

模型可以决定**调哪个能力**、**怎么把结果的结论说成人话**。
它不可以自己算数、不可以改写等级、不可以在没有工具结果时编一个结论。

这条红线的落地方式是结构性的：`agent/tools.py` 里没有实现，只有 schema 和转发；
模型能拿到的只有能力层返回的字段。它想"改判"就必须自己构造一个不存在的字段——
而那样前端/飞书会显示不出来，而不是显示一个错的结论。

**代价是模型看起来"不够聪明"**：问它"该不该跟价"，它得先调 `judge_price_follow`。
但这正是我们要的——判断错了要能定位到 L2 的那一行，而不是归因成"模型今天状态不好"。
