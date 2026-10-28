"""启动入口。

    python run.py
    python run.py --reload     # 开发时热重载
    python run.py --port 8200
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="启动竞品监控服务")
    p.add_argument("--host", default=settings.app_host)
    p.add_argument("--port", type=int, default=settings.app_port)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--no-scheduler", action="store_true", help="只跑 API，不跑定时任务")
    args = p.parse_args()

    if args.no_scheduler:
        settings.scheduler_enabled = False

    uvicorn.run(
        "app.api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
