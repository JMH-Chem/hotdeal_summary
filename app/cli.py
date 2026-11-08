"""명령줄: `python -m app.cli collect|check-prices|compare <검색어>`"""

from __future__ import annotations

import argparse
import asyncio

from .db import session
from .parsing import format_won
from .services.collector import collect_all
from .services.compare import compare
from .services.tracker import check_all


async def _main() -> None:
    parser = argparse.ArgumentParser(prog="hotdeal")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect", help="커뮤니티 핫딜 수집")
    p_collect.add_argument("--only", nargs="*", default=[], help="특정 소스 key만")
    sub.add_parser("check-prices", help="추적 상품 가격 확인")
    p_cmp = sub.add_parser("compare", help="가격 비교")
    p_cmp.add_argument("query")
    args = parser.parse_args()

    if args.cmd == "collect":
        with session() as conn:
            for run in await collect_all(conn, tuple(args.only)):
                status = "OK " if run.ok else "ERR"
                print(f"{status} {run.source:18} fetched={run.fetched:3} new={run.inserted:3} "
                      f"coupons={run.coupons:2} {run.message}")
    elif args.cmd == "check-prices":
        with session() as conn:
            print(f"checked {await check_all(conn)} items")
    elif args.cmd == "compare":
        result = await compare(args.query, use_cache=False)
        for p in result.providers:
            print(f"[{p.provider_name}] ok={p.ok} offers={len(p.offers)} {p.message}")
        for o in result.offers[:15]:
            print(f"{format_won(o.extra.get('landed_price', o.total)):>12}  {o.provider_name:8} {o.mall or '':20} {o.title[:60]}")


if __name__ == "__main__":
    asyncio.run(_main())
