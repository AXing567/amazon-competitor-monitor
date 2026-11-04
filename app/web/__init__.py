"""L5B · 管理面板（交互层之二，另一个是 L5A 飞书推送）。

    panel.py    拼装：`render_panel()` → 单文件 HTML
    layout.py   外壳：CSS、HTML 骨架、核心 JS（工具函数 / 路由 / 故障隔离）
    views/      五个视图：看板 / 阈值 / 竞品池 / 动作记录 / 数据健康

## 这一层允许做什么

- 把能力层已经算好的结论**摆出来**（列表、明细、数字卡）
- 把配置**收进去**（阈值表单、竞品状态、动作手工记录）
- 展示错误，并把 `hint` 一起显示出来

## 这一层禁止做什么

- 写任何判定逻辑（加权、阈值比较、"这个算不算异常"）——那属于 L2
- 直连数据库、`import app.services`——所有数据走 `/api/panel/*` 或 `/api/cap/*`
- 引用任何外部资源（CDN / 字体 / `<script src>`）——断网必须完整渲染

`render_panel()` 为什么要 `lru_cache`、面板为什么不鉴权，见 `panel.py` 的模块文档。
"""
from __future__ import annotations

from app.web.panel import render_panel

__all__ = ["render_panel"]
