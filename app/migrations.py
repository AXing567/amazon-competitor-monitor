"""幂等迁移：老库加列、补索引。

## 为什么不用 Alembic

MVP 的迁移只有「加列 / 加表 / 加索引」三种动作，且只跑 SQLite。
引 Alembic 的代价（迁移目录、版本表、离线 SQL、团队约定）远大于收益。
**但前提是迁移必须真幂等 + 真原子**——这两条由本文件的实现方式保证，不靠纪律。

## 为什么用 PRAGMA 检查而不是 try/except

`try: ALTER ... except OperationalError: pass` 是最常见的写法，也是最坏的写法：
它把「列已存在」和「磁盘满 / 表被锁 / 语法错误 / 数据库只读」归成同一类，
于是真正的迁移失败被静默吞掉，等你在面板上看到 KeyError 才发现列没加上。
**吞异常比不迁移更糟**——不迁移至少启动就报错。

所以：先 `PRAGMA table_info` 查，确定不存在才动手；动手失败就让它抛。

## 为什么索引也要先查（缺陷 D1 的修复）

上一版只给**列**做了 PRAGMA 保护，索引是 `CREATE INDEX IF NOT EXISTS ...` 无条件执行的。
`IF NOT EXISTS` 只保证「索引已存在」不报错，**不保证表/列存在**：

- `actions` 表还没建 → `no such table: main.actions`
- `actions` 在但缺 `executed_at` → `no such column: executed_at`

于是「空库跑一次迁移」这个最该安全的动作变成了启动即崩。
修法：建索引前用同一套 `_columns()` 检查表与列，缺一个就跳过，
并且**把跳过原因记进 `skipped`**（静默跳过等于没跳过——下次崩了没人知道为什么）。

## 为什么必须手写事务（缺陷 D2 的修复）

pysqlite 的默认隔离级别只在 **DML** 前隐式 `BEGIN`，**DDL 走 autocommit**。
所以 `with engine.begin()` 包不住 `ALTER TABLE`：中途抛异常时，
已经加上的列留在库里，而 `applied` 随异常丢失 —— 库变成半新半旧。
更糟的是它会**自我锁定**：下次再跑，列被 PRAGMA 跳过、索引继续崩，
于是每次都崩在同一处，永远修不好。

SQLite 本身支持事务性 DDL，问题只在 pysqlite 不替 DDL 开事务。
所以这里绕开 ORM 的事务管理，直接拿 DBAPI 连接：
`isolation_level=None`（关掉 pysqlite 的隐式事务）→ `BEGIN IMMEDIATE` → 跑全部 DDL
→ `COMMIT`；任何异常 `ROLLBACK` 并原样抛出。
`applied` 只在 COMMIT 成功之后才对外返回——**失败时调用方不该拿到半份清单**，
那比拿不到更误导（它会以为前 3 列已经加好了）。

## 为什么新列不写 REFERENCES 子句（刻意为之，不要"修"掉）

SQLite 的 `ALTER TABLE ADD COLUMN` 对带外键约束的列有诸多限制，
且本迁移必须能在「列已存在」时安全跳过。所以新列只写裸类型，
外键约束放在**应用层**校验（写之前先 `session.get()` 确认对象存在）。

代价是数据库层不拦脏数据，收益是迁移在任何 SQLite 版本上都不会炸。
这个取舍写在这里，避免后续被人当成疏漏补上 REFERENCES —— 补上之后
第一次在有数据的库上跑迁移就会失败。

## 执行顺序

`db.init_db()` 是「先 create_all 再 migrate」：
migrate 的 PRAGMA 检查在新表不存在时返回空集，正好被跳过。
反过来（先 migrate 后 create_all）在下一轮给新表加字段时会变成 bug。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

#: 只允许 ADD COLUMN。禁止 DROP / ALTER TYPE / UPDATE 历史数据。
#: (表名, 列名, 列定义)
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    # actions：本轮从「建了没用」变成「真正在用」，5 列全可空、纯追加。
    # 刻意不写 REFERENCES —— 见模块文档「为什么新列不写 REFERENCES」。
    ("actions", "user_id", "INTEGER"),            # 谁做的
    ("actions", "competitor_id", "INTEGER"),      # 针对哪个竞品
    ("actions", "signal_id", "INTEGER"),          # 依据哪条信号
    ("actions", "reason", "TEXT"),                # 人话依据
    ("actions", "source", "VARCHAR(16)"),         # panel / feishu_card / agent / system

    # snapshots / my_products：补齐分层判断缺的两个维度（见 tiering.py 的 TODO）。
    # 都是 JSON 列，因为存的是**原始列表**而不是算好的结果：
    # 权重算法将来会调，存结果就没法用新算法回溯历史。
    # SQLite 里 JSON 只是带 JSON 校验钩子的 TEXT，ADD COLUMN 对它没有额外限制。
    ("snapshots", "category_path", "JSON"),       # 竞品类目路径
    ("snapshots", "keywords", "JSON"),            # 竞品流量词
    ("my_products", "category_path", "JSON"),     # 自家类目路径（节点维度只认它）
]

#: 索引：(索引名, 表名, 依赖列, DDL)。
#:
#: **依赖列必须显式写出来**，不能靠 DDL 里拼字符串反解。理由：
#: 缺列时要能在执行前判断，而不是等 SQLite 报 `no such column`。
#: DDL 仍然带 `IF NOT EXISTS`：索引已存在时不该重跑一次建索引（大表上要扫描）。
IDEMPOTENT_INDEXES: list[tuple[str, str, tuple[str, ...], str]] = [
    (
        "ix_action_user_time",
        "actions",
        ("user_id", "executed_at"),
        "CREATE INDEX IF NOT EXISTS ix_action_user_time ON actions(user_id, executed_at)",
    ),
]

#: 类型别名：PRAGMA 里读到的 `INT` 与声明里的 `INTEGER` 是同一个意思，
#: 不做归一的话每次启动都会误报一条"类型不一致"的警告，警告一多就没人看了。
_TYPE_ALIASES = {
    "INT": "INTEGER",
    "BIGINT": "INTEGER",
    "BOOL": "BOOLEAN",
    "DATETIME": "TIMESTAMP",
    "STRING": "VARCHAR",
}


def _normalize_type(raw: Optional[str]) -> str:
    """把 PRAGMA 里的类型串归一成可比较的形式（大小写、空格、别名、长度参数）。"""
    text = (raw or "").strip().upper().replace(" ", "")
    if "(" in text:
        base, _, rest = text.partition("(")
        return f"{_TYPE_ALIASES.get(base, base)}({rest}"
    return _TYPE_ALIASES.get(text, text)


def _columns(cur, table: str) -> dict[str, str]:
    """表存在则返回 {列名: 声明类型}；表不存在返回空 dict。

    用底层的 `PRAGMA table_info`：它不需要任何 ORM 元数据，
    对「表是别人（老版本代码）建的」这种情况同样有效。
    """
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    # table_info: (cid, name, type, notnull, dflt_value, pk)
    return {row[1]: (row[2] or "") for row in rows}


def _check_existing_type(table: str, column: str, declared: str, actual: str) -> None:
    """列已存在但类型与声明不符时**只告警，不自动改**。

    为什么不自动改：SQLite 改列类型要「建新表 → 拷数据 → 删旧表 → 改名」，
    在迁移里做这件事，一次断电就可能丢数据。而动态类型让类型不符的列
    通常还能跑（QA 实测：`user_id` 建成 TEXT 后读写都正常），
    所以"发现 → 告警 → 人工确认"是风险最低的处理方式。
    看到这条日志不要忽略：代码里声明的是 `Integer`，迟早有一天会在远处炸。
    """
    want = _normalize_type(declared)
    have = _normalize_type(actual)
    if want and have and want != have:
        logger.warning(
            "迁移：%s.%s 现有类型 %r 与声明 %r 不一致。本轮**不自动修改**（SQLite 改类型需重建表，"
            "风险高于收益）。若该列上出现类型相关的异常，请人工确认后再迁移。",
            table,
            column,
            actual,
            declared,
        )


def migrate_report(engine: Engine) -> dict[str, list[str]]:
    """跑一次迁移，返回 `{"applied": [...], "skipped": [...]}`。

    两个清单都是**给人看的**：
    - `applied` 为空 = 库已是最新（`scripts/init_db.py` 打印它来证明幂等）；
    - `skipped` 非空 = 有东西被主动跳过了，要能一眼看出为什么。
    """
    applied: list[str] = []
    skipped: list[str] = []
    seen_missing_tables: set[str] = set()

    raw = engine.raw_connection()
    # SQLAlchemy 2.0 用 driver_connection；1.4 用 .connection。两者取其一。
    dbapi = getattr(raw, "driver_connection", None) or getattr(raw, "connection", raw)
    previous_isolation = getattr(dbapi, "isolation_level", None)

    try:
        # 关掉 pysqlite 的隐式事务，改由下面显式控制。
        # 不这么做的话 DDL 会走 autocommit，BEGIN/ROLLBACK 对它无效。
        dbapi.isolation_level = None
        cur = dbapi.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for table, column, ddl in ADDITIVE_COLUMNS:
                columns = _columns(cur, table)
                if not columns:
                    # 表还不存在：create_all 会整表建好（含新列），这里什么都不用做。
                    # 但要说一声——否则「迁移跑了却什么都没加」看起来像 bug。
                    if table not in seen_missing_tables:
                        seen_missing_tables.add(table)
                        skipped.append(f"{table}  [skipped] 表不存在，列与索引都交给 create_all")
                    continue
                if column in columns:
                    _check_existing_type(table, column, ddl, columns[column])
                    continue
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                applied.append(f"{table}.{column}  [added]")

            for index_name, table, needs, ddl in IDEMPOTENT_INDEXES:
                columns = _columns(cur, table)
                if not columns:
                    skipped.append(f"{index_name}  [skipped] 表 {table} 不存在")
                    continue
                missing = [c for c in needs if c not in columns]
                if missing:
                    # 这是缺陷 D1 的根因：缺列时 CREATE INDEX 会抛 no such column。
                    skipped.append(
                        f"{index_name}  [skipped] 表 {table} 缺列：{'、'.join(missing)}"
                    )
                    continue
                cur.execute(ddl)

            dbapi.commit()
        except Exception:
            # 回滚整批 DDL：要么全成、要么全不成。
            # 半迁移状态比"没迁移"危险得多，而且它会自我锁定（列被跳过、索引继续崩）。
            dbapi.rollback()
            raise
    finally:
        try:
            dbapi.isolation_level = previous_isolation
        except Exception:  # noqa: BLE001 —— 恢复隔离级别失败不该掩盖真正的异常
            logger.debug("恢复 isolation_level 失败", exc_info=True)
        raw.close()

    if applied:
        logger.info("迁移已应用：%s", applied)
    if skipped:
        logger.info("迁移已跳过：%s", skipped)
    return {"applied": applied, "skipped": skipped}


def migrate(engine: Engine) -> list[str]:
    """幂等迁移。返回本次**真正应用**的动作列表；第二次调用返回 `[]`。

    返回值不是日志装饰：`scripts/init_db.py` 与测试都靠它判断
    「这次到底动了没有」。空列表就是「库已是最新」的证据。

    需要「跳过了什么」时用 `migrate_report()`——那是同一个函数的完整报告。
    """
    return migrate_report(engine)["applied"]
