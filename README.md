# 竞品监控服务（competitor-monitor）

亚马逊竞品监控系统。把《竞品监控业务全景手册》里的 8 步框架，落成一套能天天跑的系统。

**一句话定位**：采集在后台静默跑，判断由代码算，结论从飞书推给你，你只负责决定「跟不跟」。

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

```
飞书 Bot ──┐
网页看板 ──┼─→ FastAPI ─→ Agent(DeepSeek) ─→ 12 个工具 ─→ 领域层
外部 API ──┘                     │                        │
                                 │                        ├─ tiering.py   四同法则分层
   定时任务(APScheduler) ─────────┘                        ├─ diff.py      信号判定
   ├─ 采集 60min/15min                                    ├─ decision.py  决策与算法
   ├─ P0 预警（采集后立刻）                                └─ baseline.py  锚点体检
   ├─ 日报 09:00
   ├─ 池子维护 每周一
   └─ 快照清理 每日 03:30
                                 ↓
                    SQLite / PostgreSQL
                                 ↑
                    卖家精灵转接桥（sellersprite-api-bridge）
```

**关键设计决策**：

1. **采集与推送解耦**。采集可以一小时一次甚至 15 分钟一次，但推送必须克制——中间的闸门是 `diff.should_push()`。跑多密都不会吵到你。
2. **判断在代码里，不在模型里**。模型只负责听懂人话、调工具、说人话。盈亏平衡点让模型算，它会算错，而且错得很自信。
3. **字段级降级**。数据源拿不到的字段一律留 `None`，快照上标 `data_quality`。缺数据时系统会说「判断不了」，而不是编一个数。
4. **采集失败绝不产生信号**。宁可不报，也不能拿空值比出一个假信号——误报比漏报更快摧毁信任。

---

## 快速开始

### 0. 前置：先启动卖家精灵转接桥

本系统不直接调卖家精灵，而是通过本地 bridge：

```bash
cd ../sellersprite-api-bridge
# 按那个项目的 README 启动，默认监听 127.0.0.1:8000
```

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

- 看板：<http://127.0.0.1:8100>
- 接口文档：<http://127.0.0.1:8100/docs>
- 健康检查：<http://127.0.0.1:8100/health>

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
│   ├── models.py               9 张表
│   ├── adapters/               ★ 多平台在这里扩展
│   │   ├── unified.py          UnifiedSnapshot 统一模型
│   │   ├── base.py             DataAdapter 抽象基类
│   │   └── sellersprite.py     卖家精灵桥适配器
│   ├── domain/                 ★ 判断逻辑，纯函数，好测
│   │   ├── baseline.py         步骤 0 锚点体检
│   │   ├── tiering.py          步骤 1 四同法则 + 加权词重合
│   │   ├── diff.py             步骤 2 差异判定 + P0/P1/P2 分级
│   │   └── decision.py         步骤 3 跟价/清仓/进攻窗口算法
│   ├── services/               业务编排（碰 DB、发网络）
│   │   ├── pool.py             竞品池管理
│   │   ├── collector.py        采集 → 快照 → 信号
│   │   ├── briefer.py          日报与预警组装
│   │   └── scheduler.py        APScheduler 定时任务
│   ├── agent/
│   │   ├── tools.py            12 个工具（定义 + 实现）
│   │   └── llm.py              DeepSeek Function Calling
│   ├── channels/               ★ 多渠道在这里扩展
│   │   ├── base.py             Channel 抽象 + NullChannel 降级
│   │   ├── feishu.py           飞书客户端 + 事件解析 + 解密
│   │   ├── render.py           业务数据 → 卡片 JSON
│   │   └── router.py           渠道路由
│   └── api/
│       ├── server.py           FastAPI 入口 + 飞书 webhook
│       └── dashboard.py        只读看板（单文件 HTML）
├── scripts/
│   ├── init_db.py              建库建表
│   └── set_baseline.py         命令行填锚点
└── tests/
    └── test_domain.py          纯函数测试（不需要 pytest）
```

### 两个扩展点

- **加平台**：写一个 `DataAdapter` 子类，把数据映射进 `UnifiedSnapshot`。`tiering` / `diff` / `decision` 一行都不用改。
- **加渠道**：写一个 `Channel` 子类，实现 `send_text` / `send_card` / `health`，然后在 `router._channel()` 里注册。

---

## Agent 工具清单

模型能调 12 个工具，全部返回结构化 JSON：

| 工具 | 用途 |
|---|---|
| `get_daily_brief` | 过去 N 小时的 P0/P1 变化，每条带动作 |
| `list_competitors` | 池子列表，可按层过滤 |
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
采集间隔从 1h → 15min，DiffEngine 阈值收紧 20%（`RULES["phase_scale"]["blast"] = 0.8`）。
旺季错过的代价远高于误报的代价，所以宁可能多报几条。

---

## 调试

```bash
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

# 只跑 API，不跑定时任务（调试时用）
python run.py --no-scheduler
```

数据都在 `data/competitor_monitor.db`，用任何 SQLite 客户端都能看。

---

## 分阶段路线

| 阶段 | 范围 | 验收 |
|---|---|---|
| **Phase 1（当前）** | 美国站单平台 · 单租户 · 飞书 · 步骤 0/1/2 + P0 预警 | 加竞品 → 次日收日报 → 对方改价收预警 |
| Phase 2 | 补步骤 3/4/5：决策落库、对标学习、动作验证闭环 | 每个动作都有验证结论 |
| Phase 3 | 网页端增删改、多站点、多租户 | 能接管全部配置 |
| Phase 4 | 钉钉 / 企业微信 / 邮件多渠道 | 加渠道不改业务代码 |

---

## 相关文档

- `../competitor-monitor-tool/docs/竞品监控业务全景手册.md` —— 业务逻辑的完整来源（1372 行）
- `../competitor-monitor-tool/docs/系统架构设计.md` —— 架构决策与取舍
- `../competitor-monitor-tool/competitor_scenario_cheatsheet.html` —— 21 个高频场景速查表
