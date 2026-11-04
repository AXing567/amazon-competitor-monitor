"""adapters 包：取数层（L1）。

两类适配器，**产出不同的类型**，不要混用：

- 竞品：`app.adapters.sellersprite.SellerSpriteAdapter` → `UnifiedSnapshot`
- 自家：`app.adapters.spapi.SpApiAdapter` → `SelfSnapshot`

两者平级、互不继承（`ARCHITECTURE.md` 六/2 的红线）。把自家数据塞进
`UnifiedSnapshot` 会让一半字段永远为空，而"空"在这里的含义是"该维度不适用"——
下游会把"不适用"读成"没数据"，然后据此下判断。

下面这层 re-export 只是为了少写几个长 import 路径，不改变任何语义。
"""
from app.adapters.self_snapshot import SelfSnapshot
from app.adapters.spapi import SpApiAdapter, SpApiAuthError, SpApiError, SpApiNotConfigured
from app.adapters.unified import TieringResult, UnifiedSnapshot

__all__ = [
    "SelfSnapshot",
    "SpApiAdapter",
    "SpApiAuthError",
    "SpApiError",
    "SpApiNotConfigured",
    "TieringResult",
    "UnifiedSnapshot",
]
