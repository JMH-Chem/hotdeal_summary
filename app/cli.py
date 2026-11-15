"""명령줄: `python -m app.cli collect|check-prices|compare <검색어>|research <상품명>`"""

from __future__ import annotations

import argparse
import asyncio

from .db import session
from .parsing import format_won
from .services.collector import collect_all
from .services.compare import compare
from .services.research import create_run, execute_run, run_view
from .services.tracker import check_all


async def _research(args: argparse.Namespace) -> None:
    with session() as conn:
        res = create_run(conn, args.query, category=args.category, include_overseas=not args.no_overseas,
                         allow_used=args.allow_used, force=args.force)
        if res.error:
            print(f"실행 안 함: {res.error}")
            return
        if res.reused:
            print(f"오늘 이미 조사한 결과를 보여줍니다 (--force로 다시 조사). run #{res.run_id}")
        else:
            print(f"AI 조사 시작 (run #{res.run_id}) — 1~5분 걸리고 Anthropic API 비용이 듭니다...")
            await execute_run(res.run_id)
        view = run_view(conn, res.run_id)
        run, ranking = view["run"], view["ranking"]
        print(f"상태: {run['status']}  비용 약 ${run['cost_usd'] or 0:.3f}  검색 {run['web_searches']}회  "
              f"페이지 {run['web_fetches']}개")
        if run["error"]:
            print(f"오류: {run['error']}")
        if ranking is None:
            return
        if ranking.best:
            b = ranking.best
            print(f"\n✅ {run['run_date']} 확인된 최저가: {format_won(b['landed_price'])}  {b['seller']}  "
                  f"[{b['verification']}]  {b['url']}")
        else:
            print("\n확인된 최저가 없음 (아래 미확인 후보를 직접 확인하세요)")
        for title, rows in (("확인됨", ranking.confirmed), ("⚠️ 더 싸지만 미확인", ranking.cheaper_unconfirmed)):
            if rows:
                print(f"\n[{title}]")
            for c in rows[:12]:
                flags = f" ({', '.join(c['flags'])})" if c["flags"] else ""
                print(f"{format_won(c['landed_price']):>12}  {c['verification']:<10} {(c['seller'] or '')[:16]:16} "
                      f"{(c['title'] or '')[:50]}{flags}")
        if run["summary"]:
            print(f"\n요약: {run['summary']}")


async def _main() -> None:
    parser = argparse.ArgumentParser(prog="hotdeal")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect", help="커뮤니티 핫딜 수집")
    p_collect.add_argument("--only", nargs="*", default=[], help="특정 소스 key만")
    sub.add_parser("check-prices", help="추적 상품 가격 확인")
    p_cmp = sub.add_parser("compare", help="가격 비교")
    p_cmp.add_argument("query")
    p_res = sub.add_parser("research", help="AI로 오늘의 최저가 조사 (Anthropic API 비용 발생)")
    p_res.add_argument("query")
    p_res.add_argument("--category", default="general", help="관부가세 품목: general|electronics|clothing|cosmetics|food")
    p_res.add_argument("--no-overseas", action="store_true", help="해외직구 제외")
    p_res.add_argument("--allow-used", action="store_true", help="중고·리퍼 포함")
    p_res.add_argument("--force", action="store_true", help="오늘 이미 조사했어도 다시 조사")
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
    elif args.cmd == "research":
        await _research(args)


if __name__ == "__main__":
    asyncio.run(_main())
