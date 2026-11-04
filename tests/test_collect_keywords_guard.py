"""采集脚本的库守卫：写错库比报错更糟。

## 为什么值得单独一个文件

`scripts/collect_keywords.py` 读的是「库里的 ASIN 列表」。如果它连到了主库
（`data/competitor_monitor.db`，真实数据），行为不是崩，而是：

1. 从**主库**读出 ASIN 列表；
2. 照常给这些 ASIN 采词、写进 `data/pilot_raw/keyword_cache/`；
3. 退出码 0，一句报错都没有。

然后 pilot 一读缓存，发现 ASIN 对不上，于是「这些竞品没词」——又是一个
「没采到」被读成「没有」的假话。所以脚本在 import 期就拒绝主库，
这里用子进程验证它真的拒绝，而不是只相信注释。

## 为什么用子进程

守卫在模块顶层执行（`_preset_database_url` 必须早于 `app.config` 的实例化），
用 import 测会把 `os.environ["DATABASE_URL"]` 留给后面的用例，污染整轮。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "collect_keywords.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


class TestRefusesMainDatabase:
    def test_explicit_main_db_is_refused(self):
        proc = _run("--db", "data/competitor_monitor.db", "--help")
        assert proc.returncode != 0, "指向主库时必须拒绝，不能默默采一遍"
        assert "拒绝：" in (proc.stdout + proc.stderr)

    def test_relative_main_db_path_is_refused_too(self):
        """`./data/competitor_monitor.db` 这种写法也要拦住，不能只比字符串。"""
        proc = _run("--db", "./data/competitor_monitor.db", "--help")
        assert proc.returncode != 0
        assert "拒绝：" in (proc.stdout + proc.stderr)

    def test_pilot_db_is_not_refused(self):
        """指向 pilot 库时守卫放行。

        `--help` 文本里本来就有「指向主库会被拒绝」这句说明，所以断言用的是
        `"拒绝："` 这个带冒号的前缀——它只出现在守卫真正触发时。

        必须用 `--help`：`--limit 0` 在脚本里是**不限**，会真的开始联网采集，
        测试就会卡在自己开的网络请求上（这条踩过）。`--help` 只走到 argparse，
        守卫（在 import 期）已经跑完了，该验证的两件事都验证到了。
        """
        proc = _run("--db", "data/pilot.db", "--help")
        combined = proc.stdout + proc.stderr
        assert "拒绝：" not in combined
        assert proc.returncode == 0, combined[:400]
