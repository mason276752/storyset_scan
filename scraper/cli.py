"""命令列介面：crawl / retry / remeasure / errors / search / stats / tags / export / viewer。"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path

from .config import SiteConfig
from .crawl import Crawler
from .db import Database
from .fetch import Fetcher, TimeBudgetExceeded


def _setup_logging(verbose: bool, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:  # CI 上把完整 log 留成檔案，方便當成 artifact 下載
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def cmd_crawl(args: argparse.Namespace) -> int:
    cfg = SiteConfig.load(args.config)

    # 命令列可覆寫速度設定，方便臨時調整
    if args.delay is not None:
        cfg.politeness.delay = args.delay
    if args.jitter is not None:
        cfg.politeness.jitter = args.jitter
    if args.no_robots:
        cfg.politeness.respect_robots = False

    logging.info(
        "站台 %s｜間隔 %.1fs（抖動 %.1fs）｜robots %s｜尺寸量測 %s",
        cfg.name,
        cfg.politeness.delay,
        cfg.politeness.jitter,
        "遵守" if cfg.politeness.respect_robots else "忽略",
        cfg.measure_size,
    )

    db = Database(args.db)
    crawler = Crawler(
        cfg,
        db,
        fetcher=Fetcher(cfg.politeness),
        limit=args.limit or 0,
        force=args.force,
        dry_run=args.dry_run,
        max_runtime=args.max_runtime or 0,
        restart=args.restart,
    )
    crawler.run()
    stats = db.stats()
    db.close()
    logging.info("資料庫現況：%s", stats)
    return 0 if crawler.errors == 0 else 1


def cmd_retry(args: argparse.Namespace) -> int:
    cfg = SiteConfig.load(args.config)
    db = Database(args.db)

    statuses = ["error", "empty"] if args.status == "all" else [args.status]
    urls: list[str] = []
    for status in statuses:
        found = [row["url"] for row in db.pages_by_status(status, cfg.name)]
        if found:
            logging.info("%s 分頁：%d 個", status, len(found))
        urls.extend(found)

    if not urls:
        logging.info("沒有需要重試的分頁")
        db.close()
        return 0

    logging.info("重試 %d 個分頁", len(urls))
    # 這些分頁已有狀態記錄，要 force 才不會被 is_done 擋掉
    crawler = Crawler(cfg, db, limit=args.limit or 0,
                      max_runtime=args.max_runtime or 0, force=True)
    crawler.crawl_urls(urls)
    db.close()
    return 0 if crawler.errors == 0 else 1


def cmd_remeasure(args: argparse.Namespace) -> int:
    """補量尺寸：量測當下被 429 或網路問題擋掉的圖，之後不會有任何流程回頭處理，
    所以獨立成一個指令。"""
    cfg = SiteConfig.load(args.config)
    db = Database(args.db)
    rows = db.images_without_size(cfg.name, args.limit)
    if not rows:
        logging.info("沒有缺尺寸的圖片")
        db.close()
        return 0

    logging.info("補量 %d 張圖的尺寸", len(rows))
    fetcher = Fetcher(cfg.politeness)
    if args.max_runtime:  # 沒有上限的話，一連串 429 冷卻會讓這一步拖很久
        fetcher.deadline = time.monotonic() + args.max_runtime
    fixed = failed = 0
    # 依來源頁分組，Referer 才能帶對
    by_page: dict[str, list[dict]] = {}
    for row in rows:
        by_page.setdefault(row["page_url"], []).append(row)

    for page_url, items in by_page.items():
        try:
            sizes = fetcher.image_sizes([i["image_url"] for i in items], referer=page_url)
        except TimeBudgetExceeded as exc:
            logging.info("%s，已補的先保留，其餘下次再補", exc)
            break
        for item in items:
            size = sizes.get(item["image_url"])
            if size:
                db.update_size(item["id"], size[0], size[1])
                fixed += 1
            else:
                failed += 1
        db.commit()

    logging.info("補量完成：成功 %d 張，仍然失敗 %d 張", fixed, failed)
    db.close()
    return 0


def cmd_errors(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.pages_by_status(args.status, args.site)
    for row in rows:
        print(f"{row['updated_at']}  {row['url']}")
        if row["error"]:
            print(f"    原因：{row['error']}")
    label = "失敗" if args.status == "error" else "無圖片"
    print(f"\n共 {len(rows)} 個{label}分頁")
    db.close()
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.search(
        tags=args.tag or (),
        match_any=args.any,
        query=args.q,
        raw_query=args.raw,
        site=args.site,
        since=args.since,
        until=args.until,
        limit=args.limit,
        offset=args.offset,
    )
    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        for row in rows:
            size = f"{row['width']}x{row['height']}" if row["width"] else "尺寸未知"
            names = row["names"] or ["(無名稱)"]
            print(f"[{row['id']}] {names[0]}  {size}  {row['published_at'] or ''}")
            for alias in names[1:]:
                print(f"    別名：{alias}")
            print(f"    {row['image_url']}")
            if len(row["page_urls"]) > 1:
                print(f"    來源：{len(row['page_urls'])} 個分頁")
            if row["tags"]:
                print(f"    標籤：{'、'.join(row['tags'])}")
        print(f"\n共 {len(rows)} 筆")
    db.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    db = Database(args.db)
    stats = db.stats()
    stats["missing_size"] = len(db.images_without_size())
    top = db.top_tags(10)
    db.close()

    if args.format == "json":
        json.dump(stats, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    if args.format == "markdown":
        _print_markdown_stats(stats, top, args.baseline)
        return 0

    print(f"圖片：{stats['images']}")
    print(f"來源頁筆數：{stats['sources']}（其中 {stats['multi_source']} 張圖出現在多個分頁）")
    print(f"標籤：{stats['tags']}")
    print(f"有尺寸：{stats['with_size']}")
    print(f"已完成分頁：{stats['pages_done']}，無圖片分頁：{stats['pages_empty']}，"
          f"失敗分頁：{stats['pages_error']}")
    print(f"全文索引：{'啟用' if stats['fts'] else '未啟用（搜尋改用 LIKE）'}")
    return 0


def _print_markdown_stats(
    stats: dict, top: list[tuple[str, int]], baseline_path: str | None
) -> None:
    """輸出 GitHub Job Summary 用的 Markdown。"""
    before = {}
    if baseline_path and Path(baseline_path).exists():
        try:
            before = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            before = {}

    def delta(key: str) -> str:
        if not before or key not in before:
            return ""
        diff = stats[key] - before[key]
        return f" (+{diff})" if diff > 0 else ""

    print("## 爬取結果\n")
    print("| 項目 | 數量 |")
    print("| --- | ---: |")
    print(f"| 圖片 | **{stats['images']}**{delta('images')} |")
    print(f"| 來源頁筆數 | {stats['sources']}{delta('sources')} |")
    print(f"| 出現在多個分頁的圖 | {stats['multi_source']} |")
    print(f"| 標籤 | {stats['tags']}{delta('tags')} |")
    print(f"| 已完成分頁 | {stats['pages_done']}{delta('pages_done')} |")
    print()

    problems = [
        ("失敗分頁（會自動重試）", stats["pages_error"]),
        ("沒有圖片的分頁", stats["pages_empty"]),
        ("缺尺寸的圖片", stats["missing_size"]),
    ]
    bad = [(label, n) for label, n in problems if n]
    if bad:
        print("### 待處理\n")
        for label, n in bad:
            print(f"- {label}：**{n}**")
    else:
        print("### 待處理\n\n無 — 沒有失敗分頁、沒有缺尺寸的圖片。")
    print()

    if top:
        print("<details><summary>最常見的標籤</summary>\n")
        print("| 標籤 | 圖片數 |")
        print("| --- | ---: |")
        for name, n in top:
            print(f"| {name} | {n} |")
        print("\n</details>")


def cmd_tags(args: argparse.Namespace) -> int:
    db = Database(args.db)
    for name, count in db.top_tags(args.limit):
        print(f"{count:6d}  {name}")
    db.close()
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    db = Database(args.db)
    rows = db.search(limit=args.limit or 10**9)
    out = open(args.out, "w", encoding="utf-8", newline="") if args.out else sys.stdout
    try:
        if args.format == "json":
            json.dump(rows, out, ensure_ascii=False, indent=2)
            out.write("\n")
        else:
            # 多值欄位用 | 串接。names / descriptions / page_urls 依來源頁排序，
            # 三者索引一一對應（第 n 個名稱配第 n 個說明與第 n 個來源頁），
            # 所以這裡不能各自去重。
            writer = csv.writer(out)
            writer.writerow(
                ["id", "site", "image_url", "width", "height", "published_at",
                 "name", "description", "page_url",
                 "names", "descriptions", "page_urls", "source_count", "tags"]
            )
            for r in rows:
                srcs = r["sources"]
                writer.writerow([
                    r["id"], r["site"], r["image_url"],
                    r["width"], r["height"], r["published_at"],
                    srcs[0]["name"] if srcs else None,
                    srcs[0]["description"] if srcs else None,
                    srcs[0]["page_url"] if srcs else None,
                    "|".join(s["name"] or "" for s in srcs),
                    "|".join(s["description"] or "" for s in srcs),
                    "|".join(s["page_url"] for s in srcs),
                    len(srcs),
                    "|".join(r["tags"]),
                ])
    finally:
        if args.out:
            out.close()
    db.close()
    return 0


def cmd_viewer(args: argparse.Namespace) -> int:
    """產生一頁式的圖片檢視器，資料讀同目錄的 images.json。"""
    template = Path(__file__).parent / "viewer.html"
    html = template.read_text(encoding="utf-8")

    if args.embed:  # 把資料直接嵌進去，單一檔案就能用 file:// 開啟
        db = Database(args.db)
        rows = db.search(limit=10**9)
        db.close()
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        html = html.replace("/*__EMBEDDED_DATA__*/", f"window.__IMAGES__ = {payload};")
        logging.info("已內嵌 %d 筆資料", len(rows))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    logging.info("已產生 %s", out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scraper", description="Storyset JSON API 圖片 metadata 爬蟲")
    p.add_argument("-v", "--verbose", action="store_true", help="輸出除錯訊息")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("crawl", help="依設定檔爬取")
    c.add_argument("-c", "--config", required=True, help="站台設定檔（YAML）")
    c.add_argument("-d", "--db", default="data/images.db", help="SQLite 檔案路徑")
    c.add_argument("--limit", type=int, default=0, help="最多寫入幾筆後停止（0=不限）")
    c.add_argument("--delay", type=float, default=None, help="覆寫請求間隔秒數")
    c.add_argument("--jitter", type=float, default=None, help="覆寫隨機抖動秒數")
    c.add_argument("--max-runtime", type=float, default=None,
                   help="跑滿幾秒就收工（CI 有時間上限時用，進度會保存）")
    c.add_argument("--restart", action="store_true",
                   help="忽略上次的翻頁進度，從 start_urls 重新開始")
    c.add_argument("--force", action="store_true", help="已抓過的頁面也重抓")
    c.add_argument("--dry-run", action="store_true", help="只印出結果，不寫入資料庫")
    c.add_argument("--no-robots", action="store_true", help="不檢查 robots.txt")
    c.add_argument("--log-file", help="同時把 log 寫到檔案")
    c.set_defaults(func=cmd_crawl)

    r = sub.add_parser("retry", help="重跑先前失敗（error）或沒抓到圖（empty）的分頁")
    r.add_argument("-c", "--config", required=True)
    r.add_argument("-d", "--db", default="data/images.db")
    r.add_argument("--status", choices=["error", "empty", "all"], default="error",
                   help="要重試哪種狀態：error（預設）／empty（曾經抽不到圖）／all")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--max-runtime", type=float, default=None)
    r.add_argument("--log-file")
    r.set_defaults(func=cmd_retry)

    rm = sub.add_parser("remeasure", help="補量尺寸沒抓到的圖片（例如量測時被 429 擋掉）")
    rm.add_argument("-c", "--config", required=True)
    rm.add_argument("-d", "--db", default="data/images.db")
    rm.add_argument("--limit", type=int, default=0, help="這次最多補幾張")
    rm.add_argument("--max-runtime", type=float, default=None,
                    help="跑滿幾秒就收工（已補的會保留）")
    rm.add_argument("--log-file")
    rm.set_defaults(func=cmd_remeasure)

    er = sub.add_parser("errors", help="列出失敗／無圖片的分頁")
    er.add_argument("-d", "--db", default="data/images.db")
    er.add_argument("--status", choices=["error", "empty"], default="error")
    er.add_argument("--site")
    er.set_defaults(func=cmd_errors)

    s = sub.add_parser("search", help="查詢資料庫")
    s.add_argument("-d", "--db", default="data/images.db")
    s.add_argument("-t", "--tag", action="append", help="標籤（可重複；預設全部符合）")
    s.add_argument("--any", action="store_true", help="標籤改成符合任一個")
    s.add_argument("-q", help="名稱／說明全文搜尋（短於 3 字自動改用 LIKE）")
    s.add_argument("--raw", action="store_true", help="-q 直接當成 FTS5 語法（AND/OR/NEAR/*）")
    s.add_argument("--site", help="限定站台")
    s.add_argument("--since", help="公開日期下界（YYYY-MM-DD）")
    s.add_argument("--until", help="公開日期上界（YYYY-MM-DD）")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true", help="輸出 JSON")
    s.set_defaults(func=cmd_search)

    st = sub.add_parser("stats", help="顯示資料庫統計")
    st.add_argument("-d", "--db", default="data/images.db")
    st.add_argument("--format", choices=["text", "json", "markdown"], default="text",
                    help="markdown 可直接寫進 GitHub 的 $GITHUB_STEP_SUMMARY")
    st.add_argument("--baseline", help="爬取前的 stats --format json 檔，用來算出本次增量")
    st.set_defaults(func=cmd_stats)

    t = sub.add_parser("tags", help="列出最常見的標籤")
    t.add_argument("-d", "--db", default="data/images.db")
    t.add_argument("--limit", type=int, default=30)
    t.set_defaults(func=cmd_tags)

    e = sub.add_parser("export", help="匯出 JSON / CSV")
    e.add_argument("-d", "--db", default="data/images.db")
    e.add_argument("--format", choices=["json", "csv"], default="json")
    e.add_argument("--out", help="輸出輸出檔案（預設印到 stdout）")
    e.add_argument("--limit", type=int, default=0)
    e.set_defaults(func=cmd_export)

    v = sub.add_parser("viewer", help="產生看圖用的 HTML（讀同目錄的 images.json）")
    v.add_argument("-d", "--db", default="data/images.db")
    v.add_argument("--out", default="data/index.html")
    v.add_argument("--embed", action="store_true",
                   help="把資料內嵌進 HTML，單一檔案即可用 file:// 開啟")
    v.set_defaults(func=cmd_viewer)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose, getattr(args, "log_file", None))
    return args.func(args)