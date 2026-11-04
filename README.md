# 竞品监控服务（competitor-monitor）

亚马逊竞品监控系统。把《竞品监控业务全景手册》里的 8 步框架，落成一套能天天跑的系统。

**一句话定位**：采集在后台静默跑，判断由代码算，结论从飞书推给你，你只负责决定「跟不跟」。

---

## ⚠️ 部署前必读：面板没有登录鉴权

> **公网部署前必须加口令（口令层不在本仓库内，需在反代或应用层自行实现）。**
> 管理面板（`/` 与 `/panel`）不做任何身份验证，`X-User-Id` 只是"以谁的身份操作"的标记。

管理面板（`/` 与 `/panel`）**不做任何身份验证**。`X-User-Id` 头只是"以谁的身份操作"的标记，
不是凭证——它没有签名、没有过期时间，任何人写一个值就能用。

这是有意的选择：单租户自用、跑在自己电脑或公司内网时，加一道"请输入密码"只是心理安慰，
而真正的防护应该在网络层。

**所以：在把它放到公网之前，必须先加一层口令/登录**，例如：

- 反代加 HTTP Basic（Nginx `auth_basic` / Caddy `basicauth`）
- 或者只监听 `127.0.0.1`，通过 SSH 隧道 / Tailscale 访问
- 或者反代上做 IP 白名单

**不要**直接把 `--host 0.0.0.0` 暴露到公网。面板能读到的东西就是这个库里的全部内容：
竞品价格、自家保本价、采购周期、动作记录、以及所有阈值配置。

同时提醒：`/api/actions/callback`（飞书卡片按钮回调）用 HMAC 校验，
未配置 `ACTION_TOKEN_SECRET` 时**直接拒绝服务**（fail-closed），不会"跳过校验"。

---

## 它解决什么问题

运营每天手工看竞品，有四个绕不过去的坑：

| 坑 | 本系统的做法 |
|---|---|
| 看不全 —— 十几个 ASIN，一天看一次只能看价格 | 四类信号全量采集（价格/供给/排名口碑/内容），按字段定频率 |
| 看漏 —— 改价了但没人发现，三天后才知道 | P0 信号 90 分钟内推到你手机 |
| 判不准 —— 对手降价了，跟着降，结果自己亏 | 五道关卡算完才给结论：保本线→清仓→库存→盈亏平衡单量→战场玩家数 |
| 说不清 —— 报了个数字，不知道该干嘛 | 每条信号都带「然后呢」，附具体动作 |

---

## 它明确做不到的

先讲清楚边界，省得后面失望：

- **拿不到对手的广告预算、出价、ACOS** —— 这是平台不公开的，任何工具都拿不到
- **拿不到对手的真实库存和成本结构** —— 只能通过「库存只降不补」这类行为反推
- **拿不到对手的补货计划** —— 同上
- **不替你操作亚马逊后台** —— 只给建议，改价、开广告要你自己动手
- **判断会错** —— 所有结论都带确定性标签，低于 60% 的会明说是猜的

---

## 架构

六层，依赖只能往下：

```
L5 交互层   飞书推送 / Agent 对话 / 管理面板（5 个视图，单文件 HTML，零外链）
              ↓
L4 能力层   33 个能力，唯一入口 call(name, *, ctx, **kwargs)   ← 面板与 Agent 共用同一份实现
              ↓
L3 编排层   pool / collector / briefer / scheduler / settings_store / binding / action_log / health
              ↓
L2 认知层   tiering(四同法则) · diff(信号判定) · decision(决策) · baseline(锚点) · thresholds(阈值)
             ★ 纯函数：不碰库、不联网、不读时钟
              ↓
L1 取数层   卖家精灵转接桥（sellersprite-api-bridge），拿不到就留 None
              ↓
L0 存储层   SQLite / PostgreSQL（10 张表，幂等 + 原子的迁移）
```

定时任务（APScheduler）挂在 L3 上，走的是同一套编排与判断：

```
├─ 采集 60min / 旺季 15min
├─ P0 预警（采集后立刻，90 分钟内推到你手机）
├─ 日报 09:00
├─ 池子维护 每周一
└─ 快照清理 每日 03:30
```

分层的细节与每条红线的理由见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

**关键设计决策**：

1. **采集与推送解耦**。采集可以一小时一次甚至 15 分钟一次，但推送必须克制——中间的闸门是 `diff.should_push()`。跑多密都不会吵到你。
2. **判断在代码里，不在模型里**。模型只负责听懂人话、**选能力**、说人话。盈亏平衡点让模型算，它会算错，而且错得很自信。
3. **字段级降级**。数据源拿不到的字段一律留 `None`，快照上标 `data_quality`。缺数据时系统会说「判断不了」，而不是编一个数。
4. **采集失败绝不产生信号**。宁可不报，也不能拿空值比出一个假信号——误报比漏报更快摧毁信任。
5. **拿不到证据 ≠ 证据表明不成立**。四同法则的判定维度不足 2 个时，分层是 `unknown`（面板显示「待判定」并说明缺什么），不是 C 级、也不是"不合格"。

---

## 快速开始

### 0. 前置：先准备数据源

本系统不直接调卖家精灵，而是通过一个本地 HTTP bridge 取数。
bridge 需要单独启动，默认监听 `127.0.0.1:8000`，对外提供 5 个端点：

| 端点 | 取的字段 |
|---|---|
| `GET /api/v1/competitor/core-info` | 标题、品牌、类目路径、上架日期、Buybox、库存状态 |
| `GET /api/v1/competitor/sales-rank` | BSR 主/子类目排名、月销估算 |
| `GET /api/v1/competitor/promotions` | 现价、优惠券、Deal 类型、净价 |
| `GET /api/v1/competitor/reviews` | 星级、评论数、月增评论 |
| `GET /api/v1/competitor/traffic-keywords` | 流量词、搜索量、自然/广告排名、流量占比 |

如果你用别的取数方式（自建爬虫、第三方 API），只需要写一个
`DataAdapter` 子类实现这 5 类字段即可，其余代码不用动。

确认能访问：<http://127.0.0.1:8000/health>

### 1. 装依赖

```bash
cd competitor-monitor
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. 配置

```bash
copy .env.example .env
```

至少要填：`SELLERSPRITE_BRIDGE_URL`（一般默认就对）。
想用 Agent 对话就填 `DEEPSEEK_API_KEY`；想收推送就填飞书那五项。**都不填也能跑**，日报会打到日志里。

### 3. 初始化 + 填锚点

```bash
python scripts/init_db.py --asin B0你的ASIN
python scripts/set_baseline.py --price 29.99 --margin 0.25 --daily 40 --stock 800 --lead 35
```

填完看一眼体检结果：

```bash
python scripts/set_baseline.py --show
```

### 4. 启动

```bash
python run.py
```

- 管理面板：<http://127.0.0.1:8100/panel>（五个标签：数据看板 / 阈值配置 / 竞品池 / 动作记录 / 数据健康）
- 旧只读看板：<http://127.0.0.1:8100/legacy>
- 接口文档：<http://127.0.0.1:8100/docs>
- 健康检查：<http://127.0.0.1:8100/health>

**没有数据源也能先看面板**（造 8 个竞品的完整假数据 + 起服务）：

```bash
python scripts/demo_offline.py            # 只造数据并打印一份日报
python scripts/demo_offline.py --serve    # 造数据 + 起服务，打开 /panel
python scripts/demo_offline.py --clean    # 清掉演示数据（只删 B0DEMO* 竞品与 [演示] 动作）
```

演示脚本**写的是独立库 `data/demo_offline.db`，不碰主库 `data/competitor_monitor.db`**。
每次运行开头会打印一行「演示目标库：…」，并在造数/清场前校验目标库：只要里面有
你自己的自家锚点（ASIN 不是 `B0MINE0001`）或非 `B0DEMO*` 的竞品，脚本就**拒绝运行**
并提示换库 —— 所以不需要、也不应该手工改 `DATABASE_URL` 去"借"真库演示。

### 5. 加第一个竞品

飞书里发「盯一下 B0XXXXXXXX」，或者：

```bash
curl -X POST http://127.0.0.1:8100/api/competitors ^
  -H "Content-Type: application/json" ^
  -d "{\"asin\":\"B0XXXXXXXX\"}"
```

系统会自动跑四同法则：同价格带、同流量池（加权词重合>40%）、同功能形态、同节点。**没通过会明确拒绝并说明原因**——这是故意的，防止池子变成垃圾场。

---

## 飞书接入

### 1. 建自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app) → 创建企业自建应用
2. 记下 **App ID** 和 **App Secret**，填进 `.env`
3. 「添加应用能力」→ 开启 **机器人**
4. 「权限管理」→ 开启：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message:send_as_bot`（以应用身份发消息）
   - `im:message.group_at_msg`（接收群聊中 @机器人 消息）
   - `im:message.p2p_msg`（接收单聊消息）
5. 「事件订阅」→ 请求地址填：`https://你的域名/feishu/webhook`
   - 如果你在本地，用一个内网穿透（cloudflared / frp / ngrok）映射到 8100 端口
   - 添加事件：`im.message.receive_v1`（接收消息）
6. 「版本管理与发布」→ 创建版本并发布，等管理员审批

### 2. 拿 chat_id

把机器人拉进群，然后：

```bash
# 用 app_id/app_secret 换 token
curl -X POST https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal ^
  -H "Content-Type: application/json" ^
  -d "{\"app_id\":\"cli_xxx\",\"app_secret\":\"xxx\"}"

# 用 token 查群列表，找到 oc_ 开头的 chat_id
curl https://open.feishu.cn/open-apis/im/v1/chats ^
  -H "Authorization: Bearer t-xxx"
```

填进 `FEISHU_DEFAULT_CHAT_ID`。

### 3. 验证渠道通不通

```bash
curl -X POST http://127.0.0.1:8100/api/run/daily-brief
```

群里收到卡片就说明通了。

> **本地调试技巧**：先不填飞书，`/api/run/daily-brief` 会把日报打到控制台日志里，业务逻辑照样能验证。

---

## 目录结构

```
competitor-monitor/
├── run.py                      启动入口
├── requirements.txt
├── .env.example
├── app/
│   ├── config.py               全局配置（唯一配置入口）
│   ├── db.py                   SQLAlchemy 引擎与会话
│   ├── models.py               10 张表
│   ├── migrations.py           幂等 + 原子的迁移（补表补列，不改数据）
│   ├── adapters/               ★ L1 多平台在这里扩展
│   │   ├── unified.py          UnifiedSnapshot 统一模型（只描述竞品）
│   │   ├── base.py             DataAdapter 抽象基类
│   │   └── sellersprite.py     卖家精灵桥适配器
│   ├── domain/                 ★ L2 判断逻辑，纯函数，好测
│   │   ├── baseline.py         步骤 0 锚点体检
│   │   ├── tiering.py          步骤 1 四同法则 + 加权词重合（证据不足 → unknown）
│   │   ├── diff.py             步骤 2 差异判定 + P0/P1/P2 分级
│   │   ├── decision.py         步骤 3 跟价/清仓/进攻窗口算法
│   │   └── thresholds.py       THRESHOLD_SPEC：全部可调阈值的唯一真相
│   ├── services/               L3 业务编排（碰 DB、发网络）
│   │   ├── pool.py             竞品池管理
│   │   ├── collector.py        采集 → 快照 → 信号（成功与失败都记 collect_runs）
│   │   ├── briefer.py          日报与预警组装
│   │   ├── scheduler.py        APScheduler 定时任务
│   │   ├── settings_store.py   阈值三层继承 + append-only 版本
│   │   ├── binding.py          竞品 ↔ 自家产品的对标绑定（带 basis 快照）
│   │   ├── action_log.py       动作记录 + 卡片按钮回调 token
│   │   ├── identity.py         用户与身份映射（飞书 open_id → 人）
│   │   ├── health.py           数据健康：采集轮次/失败/价格可信度/新鲜度
│   │   └── simulator.py        阈值试算（dry-run，零写入）
│   ├── capabilities/           ★ L4 能力层（面板与 Agent 共用同一份实现）
│   │   ├── types.py            CapContext / ParamSpec / Capability / 错误
│   │   ├── queries.py          查询类能力
│   │   ├── writes.py           写入类能力
│   │   └── judgments.py        判断类能力
│   ├── agent/                  L5A 对话入口
│   │   ├── tools.py            工具 schema + 转发（薄适配层，无实现）
│   │   └── llm.py              DeepSeek Function Calling
│   ├── channels/               ★ L5A 多渠道在这里扩展
│   │   ├── base.py             Channel 抽象 + NullChannel 降级
│   │   ├── feishu.py           飞书客户端 + 事件解析 + 解密
│   │   ├── render.py           业务数据 → 卡片 JSON
│   │   └── router.py           渠道路由
│   ├── web/                    L5B 管理面板
│   │   ├── layout.py           外壳：CSS + 核心 JS + 故障隔离
│   │   ├── views/              五个视图（每视图一个文件）
│   │   └── panel.py            拼装成单文件 HTML
│   └── api/
│       ├── server.py           FastAPI 入口 + 飞书 webhook
│       ├── deps.py             X-User-Id → CapContext（唯一解析身份的地方）
│       ├── dashboard.py        旧只读看板（/legacy）
│       └── routes/             REST 出口：panel / config / capabilities / identity
├── scripts/
│   ├── init_db.py              建库建表（可重复执行）
│   ├── set_baseline.py         命令行填锚点
│   └── demo_offline.py         离线演示：造 8 个竞品的假数据 + 起服务
└── tests/
    ├── test_domain.py          纯函数测试（不需要 pytest）
    ├── test_thresholds.py      阈值与迁移测试
    └── test_qa_acceptance.py   验收测试
```

### 两个扩展点

- **加平台**：写一个 `DataAdapter` 子类，把数据映射进 `UnifiedSnapshot`。`tiering` / `diff` / `decision` 一行都不用改。
- **加渠道**：写一个 `Channel` 子类，实现 `send_text` / `send_card` / `health`，然后在 `router._channel()` 里注册。
- **加能力**：在 `capabilities/` 里加一个 `Capability` + handler。**面板和 Agent 同时就有了**，不需要写第二遍。

---

## 能力清单

能力层共 **33 个能力**（查询 14 / 写入 13 / 判断 6），全部返回结构化 JSON，用 `GET /api/cap` 可自描述查询。
**模型能调其中的 12 个**——`agent/tools.py` 的 `TOOL_SCHEMAS` 就是这 12 个，另外 21 个不对模型开放。
为什么要收窄：工具描述会占模型的上下文，也会占它的选择空间。给模型 33 个选项，
它在这 33 个里挑错一个的概率，比在 12 个里挑错要高——而"调错工具"在业务上等于答错问题。
面板调的是同一份实现，只是 `ctx.source` 不同，所以给面板加能力不会顺带扩大模型的能力面。

下面是最常用的 12 个（就是模型能调的那 12 个）：

| 能力 | 用途 |
|---|---|
| `get_daily_brief` | 过去 N 小时的 P0/P1 变化，每条带动作 |
| `list_competitors` | 池子列表，可按层/状态过滤（`unknown` = 待判定） |
| `get_competitor_detail` | 单个竞品档案：趋势 + 历史最低价 + 全部信号 |
| `add_competitor` | 加竞品，自动跑四同法则判分层 |
| `archive_competitor` | 移出池子（保留历史） |
| `check_anchor` | 锚点体检：缺什么、导致哪个判断失灵 |
| `set_baseline` | 设置自家锚点数据 |
| `judge_price_follow` | 跟不跟价的五道关卡判断 |
| `judge_clearance` | 清仓识别（自动提取可得信号） |
| `collect_now` | 立刻采集一次，不等定时任务 |
| `set_season_phase` | 设置旺季日历，自动加密采集 |
| `get_my_status` | 自家状态总览 |

### 跟 Agent 可以这么说

```
今天有什么要处理的
他降到 19.99 我要不要跟
B0XXXXXXXX 是不是在清仓
盯一下 B0YYYYYYYY
我配置齐了吗，缺什么
我现在什么情况
10 到 12 月是旺季，给我盯紧点
```

---

## 采集频率怎么定

按**字段的物理上限**定，不是按商品一刀切：

| 字段 | 物理上限 | 本系统频率 | 理由 |
|---|---|---|---|
| 价格、优惠券、库存状态 | 准实时 | 1h（旺季 15min） | 价格战以小时为单位 |
| BSR、销量估算 | 每日 1 次 | 1 天 1 次 | 数据源本身一天只更新一次，多采无用 |
| 评论数、星级 | 每日 1 次 | 1 天 1 次 | 同上 |
| 流量词、广告位 | 每周 1 次 | 1 周 1 次 | 变化慢，采多了浪费配额 |
| Listing 内容 | 每日 1 次 | 1 天 1 次 | 改文案是低频动作 |

旺季调频率的正确姿势不是「无脑加密」，而是**同时做两件事**：
采集间隔从 1h → 15min，DiffEngine 阈值收紧 20%（`diff.phase_scale_blast = 0.8`，
改它走面板「阈值配置」页，或改 `app/domain/thresholds.py` 里 `THRESHOLD_SPEC` 的默认值）。
旺季错过的代价远高于误报的代价，所以宁可能多报几条。

---

## 调试

```bash
# 测试依赖（pytest）单独一个文件，生产不需要装
pip install -r requirements-dev.txt

# 全量测试：一个进程跑完，149 条，约 20 秒
python -m pytest tests/ -q

# 跑纯函数测试（不需要装 pytest）
python tests/test_domain.py

# 立刻采一轮，看拿到什么
curl -X POST http://127.0.0.1:8100/api/collect -H "Content-Type: application/json" -d "{}"

# 采单个
curl -X POST http://127.0.0.1:8100/api/collect -H "Content-Type: application/json" -d "{\"asin\":\"B0XXXXXXXX\"}"

# 不推送，只看日报内容
curl http://127.0.0.1:8100/api/brief?hours=24

# 强制推一次 P0（测试告警链路）
curl -X POST http://127.0.0.1:8100/api/run/p0-check

# 面板的五视图数据（不打开浏览器也能看）
curl http://127.0.0.1:8100/api/panel/board
curl http://127.0.0.1:8100/api/panel/health?days=7

# 只跑 API，不跑定时任务（调试时用）
python run.py --no-scheduler
```

数据都在 `data/competitor_monitor.db`，用任何 SQLite 客户端都能看。

**测试不会碰这个库**：`tests/conftest.py` 给每个测试模块一份独立临时库，
演示脚本则默认写到 `data/demo_offline.db`。两条路都有硬闸——演示脚本按路径拒绝
写入 `competitor_monitor.db`，测试里有一条用例每次比对主库指纹。

**注意**：面板与 `/api/*` 写端点会改这个库。想拿真库的副本试东西，先 `copy` 一份再用
`DATABASE_URL="sqlite:///绝对路径/副本.db"` 指过去——不要在生产库上试。

---

## 分阶段路线

| 阶段 | 范围 | 验收 | 状态 |
|---|---|---|---|
| **Phase 1** | 美国站单平台 · 单租户 · 飞书 · 采集/判变化/日报/P0 预警 | 加竞品 → 次日收日报 → 对方改价收预警 | 已完成 |
| **Phase 1.5** | 能力层 33 个能力 · 阈值三层作用域 + 试算 · 动作记录与复盘 · 管理面板五视图 · 数据健康 | 面板上能配置、能看数、能录动作；试算能预估触发频率 | 已完成（T01–T05） |
| Phase 2 | 自家数据接 SP-API · 对标学习 · 动作验证闭环 | 每个动作都有验证结论 | 待排 |
| Phase 3 | 采集快慢双通道（按字段物理更新频率分组）· 多站点 | 一轮采集压到 15 分钟内 | 待 M0 压测结果 |
| Phase 4 | 钉钉 / 企业微信 / 邮件多渠道 · 按人路由 | 加渠道不改业务代码 | 待排 |

> 上表只描述**能力范围**，详细里程碑、阻塞项与勾选状态以 **[ROADMAP.md](ROADMAP.md)** 为准。
> 两者不是一回事：本表说"这块代码做完了没有"，ROADMAP 说"这个里程碑的验收过了没有"——
> 例如 M5「配置外置」在 ROADMAP 里仍未勾（它的完成标志含"改阈值不用重启"这类运维验收），
> 而阈值三层作用域 + 试算的代码已经落地。
> **当前唯一阻塞项是 M0 的采频压测**：卖家精灵的频率上限没测出来之前，采集策略、
> 快慢双通道怎么分组、产品形态是"实时预警"还是"每日巡检"全都建立在假设上。

### 已知缺口（不要当成已完成）

- `snapshots.keywords` 还没落库 → 分层判断的流量词维度恒为缺失，`decidable_dims` 常年 < 3，
  所以部分竞品的层级会是 `unknown`（待判定）。这是**故意的**：拿不到证据就不判，
  比猜一个 C 级好——猜错的代价是漏掉一个真正的 S 级对手。
- `my_products.category_path` 还是硬编码 `None` → 类目维度同样无法判定。
- 上述两条补齐后 `decidable_dims` 会从 2 变 3，`unknown` 会大幅减少。`app/domain/tiering.py`
  里那段兜底分支**不要删**，它是缺数据时的正确行为。


---

## 架构说明

分层、模块边界、接口契约的详细说明见 **[ARCHITECTURE.md](ARCHITECTURE.md)**；
带"变化原因"标注的六层图和一条变化的完整触发链路见 **[docs/architecture-preview.html](docs/architecture-preview.html)**（浏览器直接打开）。

一句话版本：

```
L5 交互   ┌ 5A 推送（飞书/钉钉/企微卡片）   ┌ 5B 管理面板（单文件 HTML，只读+配置）
L4 能力   │ 33 个 Capability：查询 14 / 写入 13 / 判断 6
L3 编排   │ 一轮采集 → 判变化 → 分级 → 落库 → 按人路由
L2 认知   │ DiffEngine / Tiering / 判定纯函数     ← 唯一不碰 IO 的一层
L1 取数   │ 卖家精灵（竞品）+ SP-API（自家），Adapter 各自映射
L0 存储   │ SQLite / PostgreSQL
```

**三条红线**（改代码前先看这三条，比看目录结构有用）：

1. **L2 认知层是纯函数**——不读库、不发网络、不看时钟、不读环境变量。
   所以它既能被单元测试直接覆盖，也能被 Agent 反复调用而不产生副作用。
   `tests/` 里有一条用例扫 `app/domain/` 下的 import 来守这条线。
2. **自家数据不塞进 `UnifiedSnapshot`**。自家和竞品的字段几乎不重叠，硬合并会让一半字段
   永远为空，而"空"在这里的含义是"该维度不适用"——两者混起来，所有下游判断都会把
   "不适用"读成"没数据"。
3. **大模型只转述，不判断**。主动推送的正文由 L2 的结构化输出拼装；模型只在**你追问之后**
   才出现。原因很直接：判断错了要能追到是哪条规则、哪个阈值，而模型给的归因追不到。

---

## 管理面板

`http://127.0.0.1:8100/panel` —— **单个 HTML，零外链**（没有一个 `<script src>`，没有一个
http 地址），断网、内网、拷到 U 盘都能打开。五个标签：

| 标签 | 回答什么问题 | 读 | 写 |
|---|---|---|---|
| 数据看板 | 我在盯谁、他现在什么价、最近动了什么、属于哪个战场 | `GET /api/panel/board`、`GET /api/panel/competitors/{asin}/changes` | — |
| 阈值配置 | 现在的阈值是多少、谁改的、改完会多报几条 | `GET /api/config/thresholds`、`/versions`、`/explain`、`/schema` | `POST /api/config/thresholds`、`/simulate`、`/rollback` |
| 竞品池 | 谁在盯、谁暂停了、谁归档了，加/停/归档 | `GET /api/panel/pool` | `POST /api/panel/pool/{asin}/status` |
| 动作记录 | 我做过什么、到期该复盘哪条 | `GET /api/panel/actions` | `POST /api/panel/actions` |
| 数据健康 | 采集是不是在跑、哪一环在失败 | `GET /api/panel/health` | — |

（面板还有 `GET /api/panel/context` 提供当前租户/自家产品/操作人，五个视图都靠它。）
`/api/config/*` 是独立于 `/api/cap/*` 的前缀：阈值配置页有自己的 7 个端点，
因为它们要处理"作用域 + 版本 + 试算"这三件能力层接口不该承担的事。

**看板口径**（这一条容易搞错）：
- `active` / `observing` → 正常显示
- `paused` → **仍然进看板**，但整行压暗并标「已暂停」，最近变化那一栏改写为
  「已暂停采集 · 最后 N 前」。原因是"对手没动静"和"我们没在采"在业务上完全相反，
  不标注就会把后者读成前者。
- `archived` → **不进看板**（只在竞品池里能看见并恢复）

面板**没有登录鉴权**，`/api/*` 的写端点谁访问都能改。部署到能被人访问的地址之前必须加一层
反向代理鉴权——见本文档开头的部署前必读。

---

## 阈值：三层作用域

`company → user → product`，存**稀疏覆盖**而不是全量：只存被改过的键，没改的键继续往下层取。
回滚是"追加一个新版本"而不是覆盖旧版本，所以历史上谁在哪天把 `price_p0_pct` 从 0.05 改成
0.03 永远查得到（`GET /api/config/thresholds/versions`）。

三个分组**支持的作用域**一样（都是三层），但**运行时实际生效的层**不一样，这是刻意的：

| 分组 | 决定什么 | 支持的作用域 | 运行时实际生效 | 为什么 |
|---|---|---|---|---|
| `diff` | 一条变化算不算信号、算 P0 还是 P2 | company / user / product | **company + product** | 一轮采集 100 个竞品 ≈ 25 分钟（卖家精灵 bridge 拟人限速 2–4 秒/请求 × 每竞品 5 个接口，串行），**不可能按人各跑一遍**。所以"信号存不存在"是全局唯一的事实，不能因人而异 |
| `tiering` | 竞品分到 S/A/B/C 哪层 | company / user / product | **company + product** | 同上。分层是对竞品属性的判断，跟谁在看无关 |
| `notify` | 这条信号要不要推给这个人 | company / user / product | **代码支持三层，但当前没接线** | 这是"一人一个"该落的地方：有人只要 P0、有人不想半夜被叫醒。但按人分发日报还没做（见"已知缺口"），所以现在唯一的调用方 `collector.py` 不传 `user_id` |

**"一人一个规则"现在到底能到什么程度**（这句话别跳过）：
- 各自的**品不同** → `product` 层覆盖，现在就能用，这是 10 人场景的真实差异来源
- 各自的**打扰容忍度不同** → `notify` 层的 `user` 覆盖，代码支持但尚未接线，等"按人分发日报"那一轮
- 各自的**判断逻辑不同**（不是参数不同，是规则不同）→ **做不到**。要做得把规则插件化 + 版本化，
  代价比现在这版高一个量级。别把 `product` 层能解决的事当成"需要插件化"

**面板试算与生产采集同口径**（company + product）。试算页会回显本次实际踩了哪几层
（返回值里的 `scope.layers`）——如果你在 API 里显式传了 `user_id`，它会显示「含个人层覆盖」
并用警示色标出来，因为那个数字和生产口径不同，不能拿来预估真实触发频率。

个人层存了 `diff` 覆盖但没参与计算时，`/api/config/thresholds/explain` 会单列
`personal_overrides_not_applied` 并明说"想让这个数字生效，请改产品层或公司层"——
存了不生效的值必须自己说出来，否则用户会以为系统按他的标准在判。

