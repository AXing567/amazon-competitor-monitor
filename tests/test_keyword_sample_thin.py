"""词库抽样太薄时，「重合 0」不许当结论。

## 这组用例守的是一个「看起来是证据的数字」

上游 `/competitor/traffic-keywords` 每个 ASIN 只回 20 个词，而真实 listing 的词库
可以到上千个。实测：

    B0B4K1XH8Y（Kitsure，月销 14749）  总词数 2016，拿到 20 个 → 1% 抽样
    B0BYVBMPTD（锚点）                总词数   92，拿到 20 个 → 22% 抽样

在 1% 的样本上算「锚点那两三个主力词一个都没出现」，是概率的必然，
不是竞争关系的结论。而 32 个同节点、月销上千的竞品里有 28 个会算出 0——
直接拿去判档，等于用抽样把真实对手逐个降级，且**没有任何报错**。

所以：命中 > 0 是正面证据（薄样本里也能作证）；命中 == 0 在薄样本里返回 None。
这条不对称是刻意的，理由和「宁可 unknown 也不许把不知道写成不合格」是同一条。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.unified import UnifiedSnapshot       # noqa: E402
from app.domain.tiering import (                       # noqa: E402
    THIN_SAMPLE_RATIO,
    calc_keyword_overlap,
    judge_tier,
)

MY_CORE = {"dish drying rack": 0.8, "black dish rack": 0.2}


def _kw(*words: str) -> list[dict]:
    return [{"keyword": w, "traffic_share": 0.1} for w in words]


class TestCalcOverlapThinSample:
    def test_zero_overlap_from_thin_sample_is_none(self):
        """20 个词 vs 2016 个词：没撞上主力词说明不了任何事。"""
        assert calc_keyword_overlap(MY_CORE, _kw("yoga mat", "phone case"), comp_total=2016) is None

    def test_zero_overlap_from_thick_sample_stays_zero(self):
        """样本够厚（≥50 条）时，缺席就是缺席，0 是有效结论。"""
        got = calc_keyword_overlap(MY_CORE, _kw(*[f"unrelated {i}" for i in range(60)]))
        assert got == 0.0

    def test_positive_overlap_survives_thin_sample(self):
        """命中是正面证据：薄样本里也能确认「他在抢这个词」。"""
        got = calc_keyword_overlap(
            MY_CORE, _kw("dish drying rack", "yoga mat"), comp_total=2016
        )
        assert got == 0.8

    def test_without_total_still_guards_by_word_count(self):
        """总量缺失时不能整个失效——否则又回到「用一页样本否定上千词的词库」。"""
        assert calc_keyword_overlap(MY_CORE, _kw("yoga mat")) is None
        thick = _kw(*[f"unrelated {i}" for i in range(60)])
        assert calc_keyword_overlap(MY_CORE, thick) == 0.0

    def test_ratio_constant_is_sane(self):
        assert THIN_SAMPLE_RATIO >= 2


class TestJudgeTierReportsThinSampleHonestly:
    def _judge(self, comp_keywords, total):
        return judge_tier(
            my_price=22.99,
            my_core_keywords=dict(MY_CORE),
            my_category_path=["Home & Kitchen"],
            my_daily_units=15,
            comp=UnifiedSnapshot(
                asin="B0THIN0001",
                price=22.99,
                keywords=comp_keywords,
                traffic_keywords_total=total,
                category_path=["Home & Kitchen"],
            ),
        )

    def test_thin_sample_is_marked_and_not_silently_b(self):
        res = self._judge(_kw("yoga mat", "phone case"), total=2016)
        assert res.keyword_overlap is None
        assert res.tier_reasons["keyword_sample_thin"] is True
        assert res.provisional is True

    def test_reason_says_sampling_not_missing_data(self):
        """「缺流量词」会把人送去查采集，而真正要查的是上游返回条数。"""
        res = self._judge(_kw("yoga mat"), total=2016)
        reason = res.reject_reason or res.provisional_reason or ""
        assert "抽样过薄" in reason
        assert "缺少流量词" not in reason

    def test_enough_sample_is_not_flagged_thin(self):
        thick = _kw(*[f"unrelated {i}" for i in range(60)])
        res = self._judge(thick, total=60)
        assert res.tier_reasons["keyword_sample_thin"] is False
        assert res.keyword_overlap == 0.0

    def test_no_keywords_at_all_is_not_flagged_thin(self):
        """采不到词和采样太薄是两件事，不能混成一个标记。"""
        res = self._judge([], total=None)
        assert res.tier_reasons["keyword_sample_thin"] is False
