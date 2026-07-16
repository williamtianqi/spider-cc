#!/usr/bin/env python3
"""
基于采样数据反推 Common Crawl 全量可抓英文网页规模。

方法：
1. CC-MAIN-2025-08 共有 ~90000 个 WET 文件
2. 每个 WET 文件约 29000-30000 条 WARC 记录
3. 其中约 60% 产生有效候选站点 (candidate_sites/warc_records)
4. 去重后 unique 站点率 = unique_sites / candidate_sites
5. 每个成功站点的平均 live 页面数基于采样推算
6. 乘积 = 全 CC 英文可抓页面估计

输入：
  - cc_site_discovery_summary (JSON): 站点发现统计
  - run stats (JSONL): 实时爬取统计
  - site summaries: 每站完成情况

输出：
  - 规模推算报告 JSON
"""
import argparse
import json
import statistics
import time
from pathlib import Path


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Estimate total crawlable English pages from Common Crawl sampling")
    parser.add_argument("--discovery-summary", default="cc_site_discovery_summary_100x_en.json")
    parser.add_argument("--run-root", default="data/runs/cc_live_sites_100x_en")
    parser.add_argument("--run-stats", default="cc_live_run_stats_100x_en.jsonl")
    parser.add_argument("--output", default="cc_scale_estimate.json")
    parser.add_argument("--cc-total-wet-files", type=int, default=90000, help="Total WET files in CC-MAIN crawl")
    parser.add_argument("--cc-avg-records-per-wet", type=int, default=30000, help="Avg WARC records per WET file")
    args = parser.parse_args()

    discovery = load_json(args.discovery_summary)
    run_root = Path(args.run_root)

    # --- 站点发现采样统计 ---
    wet_paths_sampled = discovery.get("wet_paths_considered", 0) if discovery else 0
    unique_sites_found = discovery.get("unique_sites", 0) if discovery else 0
    counters = discovery.get("counters", {}) if discovery else {}
    warc_records_scanned = counters.get("warc_records", 0)
    candidate_sites = counters.get("candidate_sites", 0)
    ok_wet_files = counters.get("ok_wet_files", 0)
    duplicate_hosts = counters.get("duplicate_hosts", 0)

    candidate_rate = candidate_sites / warc_records_scanned if warc_records_scanned else 0
    unique_rate = unique_sites_found / candidate_sites if candidate_sites else 0
    sites_per_wet = unique_sites_found / ok_wet_files if ok_wet_files else 0

    # --- 全 CC 英文站点数估计 ---
    total_warc_records_est = args.cc_total_wet_files * args.cc_avg_records_per_wet
    total_candidate_sites_est = int(total_warc_records_est * candidate_rate)
    # 去重率随规模增加会提高，用保守估计：采样 2 WET 去重率 * 修正因子
    dedup_ratio_sampled = duplicate_hosts / candidate_sites if candidate_sites else 0
    # 全量去重率更高，用 log 近似：每多处理 10x WET，去重率升约 15%
    import math
    scale_factor = math.log10(max(1, args.cc_total_wet_files / max(1, ok_wet_files)))
    dedup_correction = min(0.95, dedup_ratio_sampled + 0.12 * scale_factor)
    total_unique_sites_est = int(total_candidate_sites_est * (1 - dedup_correction))

    # --- 每站页面数统计 ---
    site_pages = []
    site_pages_nonzero = []
    site_complete = 0
    site_total = 0
    running_pages = 0
    running_frontier = 0

    for summary_path in sorted(run_root.glob("*/summary.json")):
        try:
            s = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        site_total += 1
        pages = s.get("unique_text_pages", 0)
        site_pages.append(pages)
        if pages > 0:
            site_pages_nonzero.append(pages)
        if s.get("site_crawl_complete"):
            site_complete += 1

    for progress_path in sorted(run_root.glob("*/progress.json")):
        if (progress_path.parent / "summary.json").exists():
            continue
        try:
            p = json.loads(progress_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        site_total += 1
        pages = p.get("unique_extracted_text_records", 0)
        frontier = p.get("remaining_frontier", 0)
        site_pages.append(pages)
        if pages > 0:
            site_pages_nonzero.append(pages)
        running_pages += pages
        running_frontier += frontier

    # 每站页面数分布
    success_rate = len(site_pages_nonzero) / site_total if site_total else 0
    avg_pages_per_site = statistics.mean(site_pages) if site_pages else 0
    avg_pages_nonzero = statistics.mean(site_pages_nonzero) if site_pages_nonzero else 0
    median_pages_nonzero = statistics.median(site_pages_nonzero) if site_pages_nonzero else 0

    # 加上 running sites 的 frontier 作为「潜在可抓」
    potential_pages_from_frontier = running_frontier

    # --- 全量页面数估计 ---
    # 保守估计：用 avg_pages_per_site（含 0 页站点）
    pages_conservative = total_unique_sites_est * avg_pages_per_site
    # 中等估计：只算成功站点比例 * avg nonzero
    pages_moderate = total_unique_sites_est * success_rate * avg_pages_nonzero
    # 乐观估计：考虑大站 frontier 没抓完，加 frontier/site 估计
    avg_frontier_per_running = running_frontier / max(1, sum(1 for p in run_root.glob("*/progress.json") if not (p.parent / "summary.json").exists()))
    pages_optimistic = total_unique_sites_est * success_rate * (avg_pages_nonzero + avg_frontier_per_running * 0.5)

    # --- 吞吐估计 ---
    stats_lines = []
    stats_path = Path(args.run_stats)
    if stats_path.exists():
        for line in stats_path.open("r", encoding="utf-8"):
            try:
                stats_lines.append(json.loads(line))
            except Exception:
                pass
    current_rate = stats_lines[-1].get("records_per_second_avg", 0) if stats_lines else 0
    peak_rate = max((s.get("delta_records", 0) / max(1, s.get("interval_seconds", 60)) for s in stats_lines), default=0)

    # 以当前吞吐预估完成时间
    total_pages_target = pages_moderate
    time_at_current_rate_hours = total_pages_target / max(1, current_rate * 3600)
    time_at_10x_hours = total_pages_target / max(1, current_rate * 10 * 3600)
    time_at_100x_hours = total_pages_target / max(1, current_rate * 100 * 3600)

    estimate = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "common_crawl": {
            "crawl_id": discovery.get("crawl_id", "") if discovery else "",
            "total_wet_files_est": args.cc_total_wet_files,
            "avg_records_per_wet": args.cc_avg_records_per_wet,
            "total_warc_records_est": total_warc_records_est,
        },
        "discovery_sampling": {
            "wet_files_sampled": ok_wet_files,
            "warc_records_scanned": warc_records_scanned,
            "candidate_sites_found": candidate_sites,
            "unique_sites_found": unique_sites_found,
            "candidate_rate": round(candidate_rate, 4),
            "unique_rate_in_sample": round(unique_rate, 4),
            "dedup_ratio_sampled": round(dedup_ratio_sampled, 4),
            "dedup_correction_full_scale": round(dedup_correction, 4),
        },
        "site_scale_estimate": {
            "total_candidate_sites_est": total_candidate_sites_est,
            "total_unique_english_sites_est": total_unique_sites_est,
            "note": "去重率随规模增大,此为 log-corrected 保守估计",
        },
        "page_stats_from_sample": {
            "sites_sampled": site_total,
            "sites_complete": site_complete,
            "sites_with_pages": len(site_pages_nonzero),
            "success_rate": round(success_rate, 4),
            "avg_pages_per_site_all": round(avg_pages_per_site, 2),
            "avg_pages_per_successful_site": round(avg_pages_nonzero, 2),
            "median_pages_per_successful_site": round(median_pages_nonzero, 2),
            "running_sites_frontier_total": running_frontier,
            "percentiles_nonzero": {
                "p25": sorted(site_pages_nonzero)[int(len(site_pages_nonzero) * 0.25)] if site_pages_nonzero else 0,
                "p50": sorted(site_pages_nonzero)[int(len(site_pages_nonzero) * 0.5)] if site_pages_nonzero else 0,
                "p75": sorted(site_pages_nonzero)[int(len(site_pages_nonzero) * 0.75)] if site_pages_nonzero else 0,
                "p90": sorted(site_pages_nonzero)[int(len(site_pages_nonzero) * 0.9)] if site_pages_nonzero else 0,
                "p99": sorted(site_pages_nonzero)[min(len(site_pages_nonzero) - 1, int(len(site_pages_nonzero) * 0.99))] if site_pages_nonzero else 0,
                "max": max(site_pages_nonzero) if site_pages_nonzero else 0,
            },
        },
        "total_page_estimate": {
            "conservative_billion": round(pages_conservative / 1e9, 3),
            "moderate_billion": round(pages_moderate / 1e9, 3),
            "optimistic_billion": round(pages_optimistic / 1e9, 3),
            "conservative_raw": int(pages_conservative),
            "moderate_raw": int(pages_moderate),
            "optimistic_raw": int(pages_optimistic),
            "note": "conservative=所有站点含0页; moderate=成功率*非零均值; optimistic=加未抓frontier估计",
        },
        "throughput": {
            "current_pages_per_second": round(current_rate, 2),
            "peak_pages_per_second": round(peak_rate, 2),
            "est_hours_at_current_rate": round(time_at_current_rate_hours, 1),
            "est_hours_at_10x": round(time_at_10x_hours, 1),
            "est_hours_at_100x": round(time_at_100x_hours, 1),
            "est_days_at_current": round(time_at_current_rate_hours / 24, 1),
            "est_days_at_10x": round(time_at_10x_hours / 24, 1),
            "est_days_at_100x": round(time_at_100x_hours / 24, 1),
        },
        "scaling_notes": {
            "10x_approach": "单机: 增加并行站点到 512+, 用 aiohttp 替换 urllib, 减少 per-site overhead",
            "100x_approach": "多机分布式: 8-16 台机器各跑 512 并行站点, 共享 seed 队列, 独立输出 JSONL",
            "bottleneck_current": "128 并行站点, urllib 同步 IO, per-host 信号量限制, regex 抽取 CPU-bound on large pages",
        },
    }

    Path(args.output).write_text(json.dumps(estimate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(estimate, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
