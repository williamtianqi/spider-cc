#!/usr/bin/env python3
"""
多进程 + 异步混合 crawler。
每个 worker 进程运行独立的 asyncio event loop，处理一组站点。
N 个进程 × 单进程 150-200 p/s = N × 175 p/s

目标:
  8 workers × 175 p/s = 1400 p/s (约 10x)
  16 workers × 175 p/s = 2800 p/s (约 18x)
"""
import argparse
import json
import multiprocessing
import sys
import time
from pathlib import Path


def worker_main(worker_id, seeds_chunk, args_dict):
    """单个 worker 进程: 运行 async engine 处理分配到的站点子集"""
    import asyncio
    sys.path.insert(0, str(Path(__file__).parent))
    from async_pipeline_crawler import AsyncCrawlEngine, SiteCrawler

    args = type("Args", (), args_dict)()
    args.stats_jsonl = f"{args.stats_jsonl}.worker{worker_id}"
    args.partial_jsonl = f"{args.partial_jsonl}.worker{worker_id}"
    args.latest_json = f"{args.latest_json}.worker{worker_id}"

    engine = AsyncCrawlEngine(args)

    for seed_url, scope, output_name in seeds_chunk:
        output_dir = Path(args.output_root) / output_name
        if (output_dir / "summary.json").exists():
            try:
                s = json.loads((output_dir / "summary.json").read_text())
                if s.get("site_crawl_complete") and s.get("stopped_reason") == "frontier_exhausted":
                    continue
            except Exception:
                pass
        site = SiteCrawler(
            seed_url, scope, str(output_dir),
            args.pages_per_site, args.max_discovered, args.max_depth
        )
        engine.sites.append(site)

    if not engine.sites:
        return {"worker_id": worker_id, "fetched": 0, "unique": 0, "sites": 0}

    asyncio.run(engine.run())
    return {
        "worker_id": worker_id,
        "fetched": engine.total_fetched,
        "unique": engine.total_unique,
        "sites": len(engine.completed_sites),
    }


def load_all_seeds(seeds_tsv, max_sites):
    seeds = []
    with open(seeds_tsv, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            seeds.append((parts[0], parts[1], parts[2]))
            if len(seeds) >= max_sites:
                break
    return seeds


def chunk_seeds(seeds, n_workers):
    """Round-robin 分配站点到 workers，确保域名分布均匀"""
    chunks = [[] for _ in range(n_workers)]
    for i, seed in enumerate(seeds):
        chunks[i % n_workers].append(seed)
    return chunks


def main():
    parser = argparse.ArgumentParser(description="Multi-process async crawler for 10x throughput")
    parser.add_argument("--seeds-tsv", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-sites", type=int, default=20000)
    parser.add_argument("--pages-per-site", type=int, default=100000)
    parser.add_argument("--max-discovered", type=int, default=2000000)
    parser.add_argument("--max-depth", type=int, default=50)
    parser.add_argument("--max-concurrent-per-worker", type=int, default=1024)
    parser.add_argument("--max-per-host", type=int, default=8)
    parser.add_argument("--active-sites-per-worker", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=6)
    parser.add_argument("--max-page-bytes", type=int, default=2000000)
    parser.add_argument("--stats-jsonl", default="cc_multiproc_stats.jsonl")
    parser.add_argument("--partial-jsonl", default="cc_multiproc_partial.jsonl")
    parser.add_argument("--latest-json", default="cc_multiproc_latest.json")
    args = parser.parse_args()

    seeds = load_all_seeds(args.seeds_tsv, args.max_sites)
    print(f"Loaded {len(seeds)} seeds, distributing to {args.workers} workers", flush=True)

    chunks = chunk_seeds(seeds, args.workers)
    for i, chunk in enumerate(chunks):
        print(f"  Worker {i}: {len(chunk)} sites", flush=True)

    Path(args.output_root).mkdir(parents=True, exist_ok=True)

    args_dict = {
        "max_concurrent": args.max_concurrent_per_worker,
        "max_per_host": args.max_per_host,
        "timeout": args.timeout,
        "max_page_bytes": args.max_page_bytes,
        "active_sites": args.active_sites_per_worker,
        "output_root": args.output_root,
        "pages_per_site": args.pages_per_site,
        "max_discovered": args.max_discovered,
        "max_depth": args.max_depth,
        "stats_jsonl": args.stats_jsonl,
        "partial_jsonl": args.partial_jsonl,
        "latest_json": args.latest_json,
    }

    started = time.time()
    with multiprocessing.Pool(processes=args.workers) as pool:
        results = []
        for i, chunk in enumerate(chunks):
            r = pool.apply_async(worker_main, (i, chunk, args_dict))
            results.append(r)

        # 监控进度
        while True:
            time.sleep(10)
            all_done = all(r.ready() for r in results)

            # 读取各 worker 的 latest json
            total_fetched = 0
            total_unique = 0
            for i in range(args.workers):
                latest = Path(f"{args.latest_json}.worker{i}")
                if latest.exists():
                    try:
                        d = json.loads(latest.read_text())
                        total_fetched += d.get("total_fetched", 0)
                        total_unique += d.get("total_unique_text", 0)
                    except Exception:
                        pass

            elapsed = time.time() - started
            rate = total_fetched / max(0.001, elapsed)
            stat = {
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed_seconds": round(elapsed, 1),
                "total_fetched": total_fetched,
                "total_unique_text": total_unique,
                "pages_per_second": round(rate, 2),
                "workers": args.workers,
            }
            Path(args.latest_json).write_text(json.dumps(stat, indent=2) + "\n")
            with open(args.stats_jsonl, "a") as f:
                f.write(json.dumps(stat) + "\n")
            print(f"[{stat['timestamp']}] rate={rate:.0f}p/s fetched={total_fetched} unique={total_unique} workers={args.workers}", flush=True)

            if all_done:
                break

    # 汇总
    final_results = [r.get() for r in results]
    total_fetched = sum(r["fetched"] for r in final_results)
    total_unique = sum(r["unique"] for r in final_results)
    total_sites = sum(r["sites"] for r in final_results)
    elapsed = time.time() - started

    # 合并 partial JSONL
    with open(args.partial_jsonl, "w") as out:
        for i in range(args.workers):
            worker_partial = Path(f"{args.partial_jsonl}.worker{i}")
            if worker_partial.exists():
                out.write(worker_partial.read_text())

    summary = {
        "elapsed_seconds": round(elapsed, 2),
        "total_fetched": total_fetched,
        "total_unique_text": total_unique,
        "total_sites_completed": total_sites,
        "pages_per_second": round(total_fetched / max(0.001, elapsed), 2),
        "workers": args.workers,
        "worker_results": final_results,
    }
    print(f"\nDone. {total_fetched} fetched, {total_unique} unique, {total_sites} sites in {elapsed:.0f}s ({total_fetched/elapsed:.0f} p/s)", flush=True)
    Path(args.latest_json).write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    main()
