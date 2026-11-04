"""L5B · 面板拼装。

    render_panel() -> 一个完整 HTML 字符串（CSS / JS / 数据全部内联）

## 为什么 `lru_cache`

拼一次大约 40KB 字符串，成本不高但没必每请求重拼。更重要的理由：
`lru_cache` 让「面板长什么样」在进程启动后就是恒定的，
所以线上表现与本地看到一个样——不会出现"某次请求拼错了顺序"这种偶发。
真要改内容重启服务即可（面板是代码，不是配置）。

## 为什么面板不做鉴权

单租户自用（本轮裁定 #6）。面板能读到的东西，本来就是这台机器上 `data/*.db` 里的东西，
给同一个内网的人加一道"请输入密码"只是心理安慰。**但公网部署前必须加口令层**
（README 顶部有提示）——这一点在文档里写明，而不是靠"应该没人会这么干"。

## 面板不写分析逻辑

这一屏所有数字都来自 `/api/panel/*` 或 `/api/cap/*` 的返回值。
前端只做格式化（`money` / `pct` / `ago`）与展示，不做任何加权、比较、判定。
理由：判据一旦在前端复制一份，就会和 L2 的那份在下次改阈值时悄悄分叉，
而分叉的结果是"面板说有 5 条异常、飞书只推了 3 条"——这种账最后会算到模型头上。
"""
from __future__ import annotations

from functools import lru_cache

from app.config import settings
from app.web import layout
from app.web.views import html_sections, js_bundle, tabs


@lru_cache(maxsize=1)
def render_panel() -> str:
    """把外壳 + 五个视图拼成一个完整页面。"""
    return "".join(
        (
            layout.head("竞品监控 · 管理面板"),
            layout.topbar(settings.tenant_id, tabs()),
            html_sections(),
            layout.tail(js_bundle()),
        )
    )
