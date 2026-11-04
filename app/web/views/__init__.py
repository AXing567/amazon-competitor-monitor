"""五个视图的登记处。

## 为什么每个视图一个文件，却只用一次拼装

拆文件是为了**改一个视图时不用翻一个 500 行的 HTML 字符串**；
但产物必须是**单文件 HTML**（断网也要能打开，不能等第二个资源）。
所以这里做的事只有两件：按顺序拼 section、按顺序拼 JS。

## 顺序就是标签顺序

`VIEWS` 的元组顺序 = 顶部标签顺序 = 页面里 section 的顺序。
三者如果各写一遍，总会有一天不一致（标签点进去是个空 section）。
所以标签也是从 `VIEWS` 生成的，`layout.topbar()` 只负责样式与身份切换。

## 为什么加一个 import 时要检查 `section()` / `js()` 是否存在

这是面板唯一的"编译期检查"：漏写一个函数，只会在打开页面时
`getattr` 抛出 `AttributeError`——那会是整页 500，而这一页的价值恰恰在
"别的东西都挂了时它还能打开"。所以这里在 import 期就把它变成一个明确的报错。
"""
from __future__ import annotations

from typing import NamedTuple

from app.web.views import actions, board, config, health, pool


class View(NamedTuple):
    """一个视图：键（用于 `#view-<key>` 与 `location.hash`）、标签文字、模块。"""

    key: str
    label: str
    module: object


#: 顺序 = 顶部标签顺序 = 页面 section 顺序。
VIEWS: tuple[View, ...] = (
    View("board", "数据看板", board),
    View("config", "阈值配置", config),
    View("pool", "竞品池", pool),
    View("actions", "动作记录", actions),
    View("health", "数据健康", health),
)


def _check() -> None:
    """import 期自检：每个视图模块都得同时有 `section()` 与 `js()`。"""
    for v in VIEWS:
        for fn in ("section", "js"):
            if not callable(getattr(v.module, fn, None)):
                raise RuntimeError(
                    f"面板视图 {v.key!r} 缺少 {fn}()——"
                    f"见 app/web/views/__init__.py 的分层说明"
                )


_check()


def html_sections() -> str:
    """五个 `<section class="view">` 拼起来。"""
    return "\n".join(v.module.section() for v in VIEWS)


def js_bundle() -> str:
    """五个视图的 JS 拼起来（在 `layout.JS_CORE` 之后执行，所以能直接用 `register`）。"""
    return "\n".join(v.module.js() for v in VIEWS)


def tabs() -> list[tuple[str, str]]:
    """`[(key, label), ...]`，给 `layout.topbar()` 生成标签用。"""
    return [(v.key, v.label) for v in VIEWS]
