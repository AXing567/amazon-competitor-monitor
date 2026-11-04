"""pytest 全局夹具：给**每个测试模块**一份独立的空库。

## 为什么需要这个文件

在这之前，5 个测试模块各自在 **import 期**写 `os.environ["DATABASE_URL"]`：

- pytest 会先把**所有**测试模块 import 完（collection），再跑第一个用例；
- 而 `app.config.settings` 在 `app/config.py` 被 import 的那一刻就实例化了，
  之后再改环境变量对它**完全无效**；
- `app.db` 的 engine 又绑定在 `settings.database_url` 上。

三者叠起来的结果是「谁先被 import 谁赢」：字母序第一个模块
（`test_demo_isolation.py`）把全局库钉在自己的临时目录上，其余四个模块
全部挤到同一个库上互相污染，而 `test_t05_panel.py` 里那句
「临时库没有生效」的模块级断言会在 **collection 阶段**直接中断整轮测试。
表现就是 `pytest tests/` 根本跑不起来，只能一个文件一个文件跑。

## 现在怎么做的

- 本文件用 module 作用域的 autouse 夹具，在**每个模块的第一个用例之前**
  把 `settings.database_url` 指到该模块专属的临时目录，然后
  `app.db.reset_engine()` 重建 engine、`init_db()` 建表。
- 于是模块之间彻底隔离，且**与 import 顺序无关**——这正是原来做不到的事。
- 模块若自己管理库（目前只有 `test_t05_panel.py`，它还要在 import 期起子进程
  造演示数据），可以在模块里设 `__own_db__ = True` 跳过本夹具，自己负责。

## 为什么要留 `_sync_engine()` 那道保险

夹具靠 `reset_engine()` 显式重建；但**直接跑脚本**（`python tests/xxx.py`，
不经过 pytest）时不会有人调它。所以 `app.db.get_session()` / `init_db()` 里也留了
`_sync_engine()`——只要连接串和 engine 当前绑的不一致就自动重建。
两道保险各管一种入口，缺一不可。
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _isolated_db_per_module(request, tmp_path_factory):
    """本模块专用的空库。模块级 `__own_db__ = True` 时让位。"""
    if getattr(request.module, "__own_db__", False):
        yield None
        return

    import app.db as app_db
    from app.config import settings

    module_name = getattr(request.module, "__name__", "unknown").rsplit(".", 1)[-1]

    # 模块若自己声明了 `_DB_FILE`，就用**它指的那个文件**，不要另起一个。
    # 为什么：好几个模块里还有绕过 ORM 的裸 `sqlite3.connect(_DB_FILE)`（用来
    # 直接验列、验表是否存在、验写入条数）。如果夹具把 ORM 指到另一个文件，
    # 裸连接那条路就会看到「no such table」——表是建了，只是建在别的文件里。
    # 两边必须指向同一个文件，这件事只有模块自己知道。
    declared = getattr(request.module, "_DB_FILE", None)
    if declared is not None:
        db_path = Path(declared)
        db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        db_path = tmp_path_factory.mktemp(f"db_{module_name}") / "test.db"

    # 清掉上一轮（上一次 pytest 进程）留下的文件，否则"空库"用例会看到旧数据。
    try:
        if db_path.exists():
            db_path.unlink()
    except OSError:      # pragma: no cover —— 文件被别的进程占着就退化成复用
        pass

    settings.database_url = f"sqlite:///{db_path.as_posix()}"
    app_db.reset_engine()
    app_db.init_db()

    try:
        yield db_path
    finally:
        # 只 dispose，不删目录：用例失败时还能进去看现场。
        import app.db as _db

        try:
            _db.engine.dispose()
        except Exception:      # pragma: no cover —— 清理失败不该掩盖用例本身的失败
            pass
