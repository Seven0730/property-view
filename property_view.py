#!/usr/bin/env python3
"""批量分析 CSV 或分析单条预约。Python 3.9+，无需第三方依赖。"""
import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STALE_REGIONS = {"华东大区", "华北大区", "西南大区"}
STALE_MINUTES = 10
LOW_STOCK_UNITS = 3
RESULT_FIELDS = ["needs_review", "reason", "age_minutes"]


def parse_time(value):
    value = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("时间必须包含时区，例如 +08:00 或 Z")
    return value


def analyze_record(row):
    """批量与单条共用同一规则，不读取 is_successful。"""
    if not isinstance(row, dict):
        raise ValueError("单条数据必须是 JSON 对象")
    region = str(row["region_name"]).strip()
    if not region:
        raise ValueError("region_name 不可为空")
    units = float(row["available_units"])
    if not math.isfinite(units) or units < 0 or not units.is_integer():
        raise ValueError("available_units 必须为非负整数")
    age = (parse_time(row["viewing_time"]) - parse_time(row["status_synced_at"])).total_seconds() / 60
    if age < 0:
        raise ValueError("status_synced_at 不可晚于 viewing_time")
    reason = ""
    if region in STALE_REGIONS and age > STALE_MINUTES:
        reason = f"状态距看房超过 {STALE_MINUTES} 分钟"
    elif region == "华南大区" and units <= LOW_STOCK_UNITS:
        reason = f"华南剩余库存不超过 {LOW_STOCK_UNITS} 套"
    elif region not in STALE_REGIONS and region != "华南大区":
        reason = "未知区域，需人工核查"
    return {"needs_review": bool(reason), "reason": reason or "未命中提醒规则", "age_minutes": age}


def outcome(row):
    value = str(row.get("is_successful", "")).strip()
    if value == "":
        return None
    if value not in ("0", "1", "0.0", "1.0"):
        raise ValueError("is_successful 只能是 0、1 或空")
    return int(float(value))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def group_stats(rows):
    labels = [outcome(row) for row in rows]
    failed, successful = labels.count(0), labels.count(1)
    return {"total": len(rows), "known": failed + successful,
            "failed": failed, "successful": successful, "unknown": labels.count(None),
            "failure_rate": ratio(failed, failed + successful)}


def percentage(value):
    return "不适用（分母为 0）" if value is None else f"{value:.2%}"


def summary_text(metrics):
    groups = metrics["groups"]
    lines = ["# 批量分析结果", "",
             f"原始 {metrics['raw_rows']} 条，去掉 {metrics['duplicates_removed']} 条完全重复记录，保留 {groups['all']['total']} 次预约。",
             f"提醒率：{groups['reminded']['total']} / {groups['all']['total']} = {percentage(metrics['reminder_rate'])}。", "",
             "| 范围 | 预约数 | 已回传 | 未回传 | 跑空次数 | 跑空率 |",
             "|---|---:|---:|---:|---:|---:|"]
    for name, key in (("原整体", "all"), ("提醒组", "reminded"), ("无提醒组", "unreminded")):
        group = groups[key]
        lines.append(f"| {name} | {group['total']} | {group['known']} | {group['unknown']} | {group['failed']} | {percentage(group['failure_rate'])} |")
    lines += ["", f"已知跑空覆盖率：{groups['reminded']['failed']} / {groups['all']['failed']} = {percentage(metrics['failure_coverage'])}。",
              "", "口径：提醒率 = 提醒预约数 / 去重后全部预约数；跑空率 = 跑空次数 / 已回传结果次数。未知结果不当作成功。",
              "原整体与无提醒组是不同人群，以上仅说明历史风险筛选效果，不代表实际避免了跑空。",
              "规则：华东/华北/西南时延 >10 分钟，华南库存 ≤3 套；未知区域提醒人工核查。",
              "时延以 viewing_time 与 status_synced_at 的带时区时间差计算。历史到场快照不等于出发前可获取的信息。"]
    return "\n".join(lines) + "\n"


def batch(input_path, output_dir):
    input_path, output_dir = Path(input_path), Path(output_dir)
    targets = [output_dir / name for name in ("results.csv", "metrics.json", "analysis.md")]
    if input_path.resolve() in [target.resolve() for target in targets] or input_path.resolve() == output_dir.resolve():
        raise ValueError("输出不可覆盖原 CSV")
    with input_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        required = {"viewing_id", "viewing_time", "status_synced_at", "region_name", "available_units"}
        if required - set(fields):
            raise ValueError(f"CSV 缺少字段：{', '.join(sorted(required - set(fields)))}")
        if len(fields) != len(set(fields)) or set(fields) & set(RESULT_FIELDS):
            raise ValueError("CSV 表头重复或包含保留的结果字段")
        raw = list(reader)
    if not raw:
        raise ValueError("CSV 没有数据")
    seen, results, duplicates = {}, [], 0
    for line, row in enumerate(raw, 2):
        if None in row or None in row.values():
            raise ValueError(f"第 {line} 行列数与表头不一致")
        identifier = row["viewing_id"].strip()
        if not identifier:
            raise ValueError(f"第 {line} 行 viewing_id 为空")
        if identifier in seen:
            if row != seen[identifier]:
                raise ValueError(f"预约 {identifier} 重复且内容冲突，请先核对")
            duplicates += 1
            continue
        seen[identifier] = row
        try:
            outcome(row)
            results.append({**row, **analyze_record(row)})
        except (ValueError, TypeError) as exc:
            raise ValueError(f"第 {line} 行：{exc}") from exc
    reminded = [row for row in results if row["needs_review"]]
    unreminded = [row for row in results if not row["needs_review"]]
    groups = {"all": group_stats(results), "reminded": group_stats(reminded),
              "unreminded": group_stats(unreminded)}
    metrics = {"raw_rows": len(raw), "duplicates_removed": duplicates, "groups": groups,
               "reminder_rate": ratio(len(reminded), len(results)),
               "failure_coverage": ratio(groups["reminded"]["failed"], groups["all"]["failed"])}
    output_dir.mkdir(parents=True, exist_ok=True)
    with targets[0].open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields + RESULT_FIELDS)
        writer.writeheader()
        writer.writerows({**row, "needs_review": int(row["needs_review"])} for row in results)
    targets[1].write_text(json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    text = summary_text(metrics)
    targets[2].write_text(text, encoding="utf-8")
    print(text)
    print(f"逐条分析结果：{targets[0]}")
    return metrics


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    bulk = commands.add_parser("batch", help="批量分析 CSV，输出逐条结果、跑空率和提醒率")
    bulk.add_argument("--input", default=str(ROOT / "property_viewings.csv"))
    bulk.add_argument("--output", default=str(ROOT / "reports"))
    single = commands.add_parser("single", help="分析单条 JSON 数据，输出是否提醒及原因")
    single.add_argument("--data", required=True, help="含 region_name、available_units、viewing_time、status_synced_at 的 JSON 对象")
    args = parser.parse_args()
    try:
        if args.command == "batch":
            batch(args.input, args.output)
        else:
            print(json.dumps(analyze_record(json.loads(args.data)), ensure_ascii=False, indent=2))
    except KeyError as exc:
        parser.error(f"缺少字段：{exc.args[0]}")
    except (ValueError, TypeError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
