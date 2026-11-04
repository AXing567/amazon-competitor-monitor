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

- 管理面板：<http://127.0.0.1:8100/panel>（六个标签：自家基线 / 数据看板 / 阈值配置 / 竞品池 / 动作记录 / 数据健康）
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
│   │   ├── views/              六个视图（每视图一个文件）
│   │   └── panel.py            拼装成单文件 HTML
│   └── api/
│       ├── server.py           FastAPI 入口 + 飞书 webhook
│       ├── deps.py             X-User-Id → CapContext（唯一解析身份的地方）
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

- **分层判断的三个维度现在都能拿到数据了**（价格带 / 流量池 / 节点）：
  `snapshots.keywords`、`snapshots.category_path`、`my_products.category_path`
  三列已经落库，采集时写入、重算时还原。演示数据实测 8 个竞品里 7 个 `decidable_dims = 3`。
- **但 `unknown` 不会消失，也不该消失**：卖家精灵的流量词是慢通道（单次 5~15 秒、
  有配额上限），批量采集时仍可能拿不到——那时 `keywords` 就是 `None`，
  `decidable_dims` 掉到 1，层级如实标成「待判定」。这是**故意的**：
  拿不到证据就不判，比猜一个 C 级好——猜错的代价是漏掉一个真正的 S 级对手。
- `app/domain/tiering.py` 里 `insufficient` 那段兜底**不要删**。它是"维度不足以判定"的
  护栏，不是这次补数据的临时补丁——以后再加维度，同样会出现"某几维暂时没有"的情况。
- 自家锚点不填 `core_keywords` / `category_path` 时，对应维度仍然判不了
  （`check_anchor` 会直接告诉你缺哪一项、导致哪个判断失灵）。

**投递层（这一块刚接通，剩下的缺口要点名）**

- **只有飞书实现了**。`router._IMPLEMENTED_CHANNELS` 目前是 `("feishu",)`。
  一个人如果只绑了企微/钉钉身份，**他收不到任何预警**——因为没实现的渠道会被拦在解析阶段，
  不会交给 `NullChannel`（那个类的 `send_text()` 返回 `True`，会把"没发出去"伪装成成功）。
  漏收这件事只体现在 WARNING 日志里，`push_alert()` 仍返回 `True`。要彻底消掉得让它返回
  结构化结果（送达几人 / 漏几人），那会动调用方契约，留在下一轮。
- **`channel_bindings` 表没有写入方**，所以日报 `push_daily()` 和"竞品无人绑定"的退回路径
  仍然落到 `.env` 的 `feishu_default_chat_id`（一个群）。日报是团队级汇总，符合预期；
  退回路径则有 WARNING 可见，不再是静默。
- **`diff.should_push()` 零调用**，它读的 `notify_level` 长在那张没人写的表上。
  也就是说"按级别筛推送"这个闸门现在是死代码——真正的闸门是 P0 才推（`settings.p0_push_enabled`）
  加上采集期的同信号冷却。

**外部依赖卡住、代码侧做不了的**

- **采集频率上限没测过**（卖家精灵 bridge 的风控）。这是唯一真正的阻塞项：
  上限没测出来之前，"准实时预警"和"每日巡检"哪个是产品形态全是假设。
  `docs/` 里的 25 分钟/100 竞品是**估算**，不是实测值。
- **SP-API 是终局拿不到的，不是"还没配"**（2026-11-04 确认）：
  手上没有真实店铺，就没有可授权的卖家账号，凭据这件事到此为止。
  `app/adapters/self_snapshot.py`（独立类型）+ `app/adapters/spapi.py`（分段取数）
  + 固定 fixture 测试都保留着，**哪天有了店铺，填三个键就能用**；但不要把它当成待办。

  它也不构成瓶颈。自家锚点里真正需要自动化的只有库存数量，
  而 `fba_available_units` **不进就绪度评分的任何能力位**——填不填都不影响
  「能不能分层 / 能不能跟价 / 能不能判进攻窗口」。其余字段（保本价、目标毛利率、
  补货周期、售价、日均销量）本来就是卖家自己知道的静态知识，任何 API 都给不出答案。
  所以正确路径是面板上的**录入表单**（已可用），不是等 SP-API。
- **`release_date`（上架日期）现在会落库了**，所以「A 级黑马」这条判据在重算时不再失效。
  在此之前它只在采集当刻有效——从库里重算就变 `None`，黑马永不命中，而且不报错。
- **`traffic_keywords_total` 也是同样的问题，2026-11-04 才补上**：不加这一列时，
  「采集时判」和「从库里重算」用两套判据（重算路径上总量恒为 `None`，
  抽样守卫退化成只看"条数 < 50"），同一批数据两条路可能给出不同档位，且不报错。

**业务闭环缺的两块（不是 bug，是没做）**

- **`decisions` 表零写入**。`app/domain/decision.py` 里有完整的判定逻辑（跟价/进攻/学习/
  不动作），`briefer._apply()` 也确实把结果放进了日报的内存对象——但**没有一行落库**。
  后果：日报发出去就散了，回看不了"当时为什么建议跟价"。
- **动作与复盘没有闭环**：`actions` 表在写（面板手记 + 飞书卡片回调），
  但 `verify_due_at` 到期后的复盘没有实现——记了"7 天后回看他的 BSR"，
  7 天后没有任何东西来提醒你回看。系统现在**能看、能想，不能闭环**。


---

## 真实数据 pilot（`scripts/pilot_real.py`）

演示数据只证明算法自洽，不证明它判得对。pilot 把数据源换成**卖家精灵选品库的真实返回**，
拿一个真实 ASIN 当「自家产品」，走同一套 `judge_tier` + 落库代码，让分层第一次对着真实市场分布产生。

```
python scripts/pilot_real.py            # 造 pilot 数据 + 打印报告（写 data/pilot.db）
python scripts/pilot_real.py --serve    # 造完直接起面板

python scripts/collect_keywords.py      # 补采流量词（断点续跑，已采的跳过）
python scripts/collect_keywords.py --limit 5   # 先采 5 个验通道通不通
```

`collect_keywords.py` 和 `pilot_real.py` 分工：采集要 2~4 秒/个、可以断点续跑，
判定是纯计算、随时可重跑。混在一起会让「重跑一次判定」变成几分钟，
而重跑判定是开发期最频繁的动作——分开之后额度也不会被重复消费。

- **默认锚点** `B0BYVBMPTD`（Dish Racks 节点里一个中腰部 listing：$22.99、月销 3345、
  BSR 2440、4.8 星 5335 条评论、2023-03 上架、FBA）。挑头部（JASIWAY，月销 18844、62 个变体）
  会让「跟价压力」这个前提失真。
- **候选来自同一个类目节点**（`node_id_path` 由 `/market/research` 给出），所以
  `node_match` 是**构造保证**而不是猜的。选品库不返回商品的类目路径（`category_path` 恒空，
  三个 ASIN 交叉确认过），这条是唯一正当的解法。
- **候选不按价格带筛**。价格带是要判的东西，先按它筛再报「全在带内」等于把结论写进前提。
  带外的照样进判定，被判 C 并在报告里列出来。
- **候选也不按数量截断**（`--limit` 默认 0 = 不限）。第一版默认取 BSR 前 20，补了低价段数据
  之后池子从 17 掉到 **12**——数据变多、竞品变少，因为 $6~$15 那批便宜货 BSR 更好，
  把带内的真候选挤出了前 20。截断点是采样窗口，不是判定规则；非 0 时报告会明写
  「另有 N 个未评估」，因为「没评估」和「不合格」在报告里长得一模一样。

**采样窗口（决定了哪些话不能说）**

| 拉取 | 排序 | 覆盖 |
|---|---|---|
| `filter_dishracks_100` | 月销降序 | 头部 listing（变体占满整页） |
| `filter_dishracks_band` + `p2..p5` | BSR 升序 + 价格窗 $18~$36 | 中腰部 |
| `filter_band_lowprice` + `p2` | **价格升序**，不带价格窗 | $1.41~$16.99 |

补齐低价段后发现这个节点的价格结构是**双峰**的：$1.4~$13 一大簇（100 条里 95 条价格比
0.30~0.57，全部在四同法则的带外）+ $16~$36 一簇（锚点在这簇里），中间 $13~$16 很薄。
所以「池子里没有低价冲击品」现在是句**有依据的话**（它们存在且量大，但形态不同档），
而不是当初那句「窗口里没有」。仍然不能说的是「$13~$16 这条窄缝里没有对手」——
那得按价格窗专门再拉一次。

**哪些是真、哪些不是**

| 字段 | 真假 |
|---|---|
| 标价 / 月销 / BSR / 评分 / 评论数 / 上架日期 / 品牌 / 标题 | 真（公开数据） |
| 流量词 / 词重合度 | 真，但**每个 ASIN 只拿到 20 个词**（上游只回一页）→ 见下面「词重合的边界」 |
| 到手价、优惠券 | **拿不到**（选品库只给标价，绝不填进 `net_price`） |
| 保本价 / 目标毛利率 / FBA 可售库存 / 补货周期 / 日均销量 | **假设值**，写进 `assumed_fields` |
| 退货率 | **无处可落**（模型没这列）→ 不参与任何判断 |

**词重合的边界（这一节比结论重要）**

流量词靠 `scripts/collect_keywords.py` 采，69 个 ASIN 拿到 697 个词。但每 ASIN 只有
**20 条**（上游 `/competitor/traffic-keywords` 只回一页），而真实词库可以到上千——
实测 Kitsure（月销 14749）总词数 **2016**，我们拿到 20 条，等于 **1% 抽样**。

在 1% 的样本上算出「重合 0」，含义是「这一千个词里恰好没撞上锚点那两三个」，
**不是**「他不抢这个词」。32 个同节点、月销上千的竞品里有 28 个会算出 0——
照这个数判档，等于用抽样把真实对手逐个降级，且没有任何报错。

所以 `calc_keyword_overlap` 有一条不对称守卫（`is_thin_keyword_sample`）：
**命中 > 0 照用**（薄样本里也能证明「他在抢这个词」）；
**命中 == 0 且样本薄于 50 条 → 返回 None**（判不了，不是不重合）。
面板上这两件事分开写：「词库抽样薄」vs「缺词重合」——前者查上游返回条数，
后者查采集，是两个不同的修法。

**这一轮跑出来的基线**：原始 720 行 → 归并 138 个独立 listing → 过观察价值门槛（月销 ≥300、
BSR ≤20000）134 个 → **入池 68 个**，出池 66 个：
50 个价格比低于下限（$1.41~$15.99，0.06~0.696）、16 个高于上限（1.44~1.76），
**带内被拒的一个都没有**——这一轮拒收 100% 由价格带一项造成，理由逐条写在报告里。

入池的 68 个分档：**A × 6**（4 个靠加权重合度 ≥60%，2 个是黑马）、**B（暂定）× 62**
（34 个「词库抽样薄」+ 28 个「缺词重合」）。

由此这一轮能信的：同战场识别、价格带是否重叠、**正面**的词重合（他确实抢了这个词）。
不能信的：**否定**的词重合判断（样本太薄）、断货预警、提价/降价空间、补货建议
（依赖假设值）。这一轮也没有变化信号——只有一轮快照，`DiffEngine` 要两次才能判「他降价了」。

> 从「暂定」走向「可判」还差一步：**拿到每个 ASIN 的完整词库**（分页取，而不是只取第一页）。
> 那是 bridge 侧要加的能力，不是本仓库能决定的。

pilot 写的 `data/pilot.db` 和 `data/pilot_raw/` 都在 `.gitignore` 里：
换台机器要先用 `scripts/bridge_probe.py` 重拉一轮原始返回（命令见该脚本顶部说明）。


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
http 地址），断网、内网、拷到 U 盘都能打开。六个标签：

| 标签 | 回答什么问题 | 读 | 写 |
|---|---|---|---|
| 自家基线 | 我自己的价/成本/库存/词是什么、这些数从哪来、缺了哪一项、缺了会导致什么判断失灵 | `GET /api/cap/get_baseline`、`GET /api/dataset` | `POST /api/cap/set_baseline`、`POST /api/dataset/mock/rebuild` |
| 数据看板 | 我在盯谁、他现在什么价、最近动了什么、属于哪个战场 | `GET /api/panel/board`、`GET /api/panel/competitors/{asin}/changes` | — |
| 阈值配置 | 现在的阈值是多少、**每个数字什么意思**、谁改的、改完会多报几条 | `GET /api/config/thresholds`、`/versions`、`/explain`、`/schema` | `POST /api/config/thresholds`、`/simulate`、`/rollback` |
| 竞品池 | 谁在盯、谁暂停了、谁归档了，加/停/归档 | `GET /api/panel/pool` | `POST /api/panel/pool/{asin}/status` |
| 动作记录 | 我做过什么、到期该复盘哪条 | `GET /api/panel/actions` | `POST /api/panel/actions` |
| 数据健康 | 采集是不是在跑、哪一环在失败 | `GET /api/panel/health` | — |

（面板还有 `GET /api/panel/context` 提供当前租户/自家产品/操作人，各视图都靠它。）

### 自家基线页：为什么它排第一

竞品监控的每一次判断都是「和谁比、比什么、比多少算异常」。自家锚点缺一个数，
后面就有一整类判断变成「判不了」——而这个事实在别的视图里只表现为
"某个数字是破折号"，看不出根因。

所以这一页摊开的是**字段级**明细，不是一句"就绪度 73%"：

- 每一格显示**实际值**、值类型（金额 / 比例 / 名次 / 词表）、以及它是不是**假设值**
- 假设值会连带说出「由它推导出的哪些结论不可信」——没有这句，一个拍出来的
  保本价推导出的提价建议，会看起来和真的一样
- 四个能力位直接来自 L2：分层 / 跟价判断 / 进攻窗口 / 抄 listing 检测，能做就绿
- 缺的那一维会说明「缺了导致哪个判断失灵」，而不是只说"未填"

### 数据模式：真实 / Mock

面板顶部可以在两个**物理隔离的库**之间切换：

| 模式 | 库 | 用途 |
|---|---|---|
| 真实数据 | `DATABASE_URL`（默认 `data/competitor_monitor.db`） | 你采来的竞品。这里的结论可以被当真 |
| Mock 数据 | `MOCK_DATABASE_URL`（默认 `data/mock.db`） | 一套自洽的演示集，可随时重建。结论只用于验证链路 |

切换是**按请求生效**的：前端每个请求带 `X-Data-Mode`，服务端把它钉进该请求的
上下文（`app/api/middleware.py`），所以切过去之后所有标签一起换库，不是只换一屏。
拼错的模式值会直接返回 400，而不是悄悄退回某一个模式——两个方向都有害
（把真数据当演示 / 拿假数据做决定）。

Mock 库的默认内容（`python scripts/seed_mock.py`，或在面板上点「生成 Mock 数据」）：

| ASIN | 档位 | 演示的是 |
|---|---|---|
| B0MOCK1001 | S | 高重合 + 价格带内 + 节点一致 + 体量咬合 |
| B0MOCK1002 | A | 词重合 80%，但体量是自家的 11 倍 → 不判 S |
| B0MOCK1003 | A | 新品黑马（上架 45 天 + 月销 1500），走的是不看词的那条判据 |
| B0MOCK1004 | B | 只命中权重最低的那个词 → 20% |
| B0MOCK1005 | C | 60 个无关词，同节点同价位但**真的不抢词**（这个 0 是结论） |
| B0MOCK1006 | C | 价格 2 倍 + 类目不同 → 拒收；已暂停 |
| B0MOCK1007 | B（暂定） | 没采到词 |
| B0MOCK1008 | B（暂定） | 20 个词 / 总量 800 → 抽样太薄，算不出结论；今天断货 |
| B0MOCK1009 | unknown | 价格和类目都拿不到，可判维度只剩 1 维 |

**档位一个都不是写死的**：`mock_data._rescore()` 调生产路径的 `judge_tier()` 从库里的
最新快照重算，算完与上表的"期望档位"逐条核对，不一致就抛错。
写死 `tier="S"` 会产生「C 级对手 · 词重合 71%」这类自相矛盾的报告——
它不报错、不崩溃，只是安静地教人相信一份假数据。
`/api/config/*` 是独立于 `/api/cap/*` 的前缀：阈值配置页有自己的 7 个端点，
因为它们要处理"作用域 + 版本 + 试算"这三件能力层接口不该承担的事。

**「这段什么意思？」折叠区**：阈值配置页每个分组下面有一个（默认收起，展开时才请求
`/explain`，不会为一个屏预拉三次）。它回答的是"`price_p1_pct = 0.03` 到底意味着
降 3% 进日报、还是会推飞书"——面板上只显示数字时，这句话没人能自己推出来。
两处口径要注意：

- 它讲的是**当前实际生效**的层，所以只要自家锚点存在就带 `my_product_id`，
  **不跟着上面那排「公司默认 / 产品覆盖」芯片走**（芯片选的是"你要改哪一层"）。
- 个人层在 `diff` / `tiering` 组里不参与计算，这时它会**单列一行警示**
  （"你设的 3% 不参与判断，个人层只影响打扰节奏"），而不是把那句提示埋在正文里。
  存了不生效的值必须自己说出来，否则用户会以为系统按他的标准在判。

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
| `notify` | 同一条信号多久内不重复推（冷却窗口） | company / user / product | **company + product** | 这条配置唯一的消费点是 `collector.py:202` 的冷却判断，它发生在**采集过程中**——一轮采集只跑一遍、没有"这个人"，所以天然只有两层。注意：它**不是**推送开关，别把它当"这个人要不要收"用（那是下面的收件人路由） |

**"一人一个规则"现在到底能到什么程度**（这句话别跳过）：
- 各自的**品不同** → `product` 层覆盖，现在就能用，这是 10 人场景的真实差异来源
- **谁的告警推给谁** → **已接通**。链路是 `竞品 → CompetitorBinding(status=active) 里的人
  → 各人 UserIdentity 的飞书 open_id → 本人`。之前 `router._targets()` 只读 `ChannelBinding`
  这张全项目没人写的表，于是永远落回 `.env` 的一个 `feishu_default_chat_id`——
  **所有人的所有告警全推到一个群**，10 人共用时等于噪音互相淹没。
  现在解析不到收件人时不会静默丢弃：退回默认渠道 + 打 WARNING 点名是哪个竞品。
- 各自的**打扰容忍度不同**（有人只要 P0、有人不想半夜被叫）→ **还没接线**。
  `notify` 的 `user` 层存得下，`explain` 也会如实告诉你"这条没参与计算"，
  但当前没有消费点：`notify` 组只服务采集期的冷却窗口，那里没有自然人。
  要做的话，正确位置是推送前的**按人闸门**（`push_alert` 里按 user_id 取一次 notify 阈值），
  不是采集期。
- 各自的**判断逻辑不同**（不是参数不同，是规则不同）→ **做不到**。要做得把规则插件化 + 版本化，
  代价比现在这版高一个量级。别把 `product` 层能解决的事当成"需要插件化"

**面板试算与生产采集同口径**（company + product）。试算页会回显本次实际踩了哪几层
（返回值里的 `scope.layers`）——如果你在 API 里显式传了 `user_id`，它会显示「含个人层覆盖」
并用警示色标出来，因为那个数字和生产口径不同，不能拿来预估真实触发频率。

个人层存了 `diff` 覆盖但没参与计算时，`/api/config/thresholds/explain` 会单列
`personal_overrides_not_applied` 并明说"想让这个数字生效，请改产品层或公司层"——
存了不生效的值必须自己说出来，否则用户会以为系统按他的标准在判。

