"""调 sellersprite-api-bridge 并**把原始返回落盘**。

## 为什么要有这个脚本，而不是直接 curl

三件事：

1. **省额度**。这个账号的额度是有限的（试用户），同一个 ASIN 调两次就是两次消耗。
   落盘之后可以反复读、反复试映射，不用再打网络。
2. **留证据**。真实数据跑出来的结论要能回溯「当时接口返回的原文长什么样」。
   只存映射后的 `UnifiedSnapshot` 是存不回去的——半年后字段对不上，就再也说不清
   是解析写错了还是源头变了。
3. **看清降级**。bridge 在试用账号下会把 `traffic-keywords` 降级成 ABA 出词反查，
   返回体里用 `source` / `notice` 标明。这些信息**只在原始 JSON 里**，映射后就丢了。

## 用法

    python scripts/bridge_probe.py filter --keyword "wooden toy car" --size 10
    python scripts/bridge_probe.py summary --asin B079B73T68
    python scripts/bridge_probe.py get --path /api/v1/keywords/research --param keyword=car

原始返回写到 `data/pilot_raw/<tag>.json`，同名会覆盖（但会先打一行提醒）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "pilot_raw"
DEFAULT_BASE = "http://127.0.0.1:8000"


def _base() -> str:
    """读项目配置里的 bridge 地址，读不到就用默认值。

    为什么不直接 hardcode：bridge 可能被挪到别的端口/机器上，
    而 `app/config.py` 已经有 `sellersprite_bridge_url` 这一个真相源。
    """
    try:
        sys.path.insert(0, str(ROOT))
        from app.config import settings

        return str(settings.sellersprite_bridge_url).rstrip("/")
    except Exception:  # noqa: BLE001
        return DEFAULT_BASE


def _opener() -> urllib.request.OpenerDirector:
    """造一个**不用任何代理**的 opener。

    这是踩出来的：同一条 `filter --node-id-path ...` 请求，`curl` 拿到 20 条真实
    商品，而本脚本（`urllib`）拿到 HTTP 502 + `upstream connect failed ...`。
    请求被环境里的 `HTTP_PROXY` 截走了，根本没到 bridge。
    之后同一条命令又能通——**环境问题会时好时坏，所以不能靠"等一下再试"**。
    空 `ProxyHandler({})` = 「所有协议都不走代理」，写死在本进程里，
    不依赖调用者记得先 `unset HTTP_PROXY`。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path: str, tag: str, *, base: str | None = None, timeout: int = 180, **params) -> tuple[int, str]:
    """GET 一个 bridge 接口，原始 body 写盘，返回 `(状态码, body)`。"""
    base = (base or _base()).rstrip("/")
    # 丢掉 None，否则会拼出 `?asin=None` 这种把字符串 "None" 当值发出去
    params = {k: v for k, v in params.items() if v is not None}
    q = urllib.parse.urlencode(params)
    url = f"{base}{path}" + (f"?{q}" if q else "")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target = OUT_DIR / f"{tag}.json"

    started = time.time()
    try:
        with _opener().open(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            code = int(resp.status)
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也把 body 留下来——降级/熔断的说明都在里面
        body = exc.read().decode("utf-8", "replace")
        code = int(exc.code)
    except Exception as exc:  # noqa: BLE001
        body = json.dumps({"__error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
        code = -1

    elapsed = time.time() - started
    if target.exists():
        print(f"  ! 覆盖已有文件 {target.name}")
    target.write_text(body, encoding="utf-8")
    print(f"[{code}] {elapsed:5.1f}s  {path}  {params}  ->  {target.name}  ({len(body)} B)")
    return code, body


def main() -> None:
    ap = argparse.ArgumentParser(description="调 bridge 并落盘原始返回")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_filter = sub.add_parser("filter", help="产品库选品")
    p_filter.add_argument("--keyword", default=None)
    p_filter.add_argument("--node-id-path", default=None, dest="node_id_path")
    p_filter.add_argument("--size", type=int, default=20)
    p_filter.add_argument("--page", type=int, default=1)
    p_filter.add_argument("--order-field", default="units", dest="order_field")
    p_filter.add_argument("--tag", default=None)

    p_sum = sub.add_parser("summary", help="单 ASIN 全维度聚合")
    p_sum.add_argument("--asin", required=True)
    p_sum.add_argument("--tag", default=None)

    p_mkt = sub.add_parser("market", help="细分市场大盘")
    p_mkt.add_argument("--node-id", default=None, dest="node_id")
    p_mkt.add_argument("--size", type=int, default=20)
    p_mkt.add_argument("--tag", default=None)

    p_get = sub.add_parser("get", help="任意接口（--param k=v 可重复）")
    p_get.add_argument("--path", required=True)
    p_get.add_argument("--param", action="append", default=[])
    p_get.add_argument("--tag", required=True)

    ap.add_argument("--base", default=None)
    args = ap.parse_args()

    if args.cmd == "filter":
        tag = args.tag or f"filter_{(args.keyword or args.node_id_path or 'x')}".replace(" ", "_")
        code, body = call("/api/v1/product/filter", tag, base=args.base,
                          keyword=args.keyword, node_id_path=args.node_id_path,
                          size=args.size, page=args.page, order_field=args.order_field)
    elif args.cmd == "summary":
        tag = args.tag or f"summary_{args.asin}"
        code, body = call("/api/v1/competitor/monitor-summary", tag, base=args.base, asin=args.asin)
    elif args.cmd == "market":
        tag = args.tag or f"market_{args.node_id or 'all'}"
        code, body = call("/api/v1/market/research", tag, base=args.base,
                          node_id=args.node_id, size=args.size)
    else:
        kv = {}
        for item in args.param:
            if "=" not in item:
                raise SystemExit(f"--param 需要 k=v 形式，收到 {item!r}")
            k, v = item.split("=", 1)
            kv[k] = v
        code, body = call(args.path, args.tag, base=args.base, **kv)

    # 只回一段摘要，正文在文件里——终端刷满 JSON 对判断没帮助
    try:
        d = json.loads(body)
        print("  keys:", list(d.keys())[:12])
        for k in ("notice", "source", "warning", "detail", "error"):
            if d.get(k):
                print(f"  {k}: {d[k]}")
        items = d.get("items") or d.get("keywords") or d.get("keywords_list") or []
        if isinstance(items, list) and items:
            print(f"  items: {len(items)} 条")
    except Exception:  # noqa: BLE001
        print("  (body 不是 JSON，已原样落盘)")


if __name__ == "__main__":
    main()
