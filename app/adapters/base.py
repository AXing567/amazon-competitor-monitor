"""数据源适配器抽象。

新增平台（Walmart / Temu）时只需实现这个接口，上层业务代码不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from app.adapters.unified import UnifiedSnapshot


class DataAdapter(ABC):
    name: str = "base"

    @abstractmethod
    async def fetch_snapshot(self, asin: str, marketplace: str = "US") -> UnifiedSnapshot:
        """采集单个 ASIN 的全维度快照。失败时返回 data_quality='failed' 的对象，不抛异常。"""
        raise NotImplementedError

    @abstractmethod
    async def fetch_keywords(self, asin: str, marketplace: str = "US") -> list[dict]:
        """只取关键词列表（省成本，用于算重合度）。"""
        raise NotImplementedError

    async def health(self) -> dict:
        return {"adapter": self.name, "status": "unknown"}
