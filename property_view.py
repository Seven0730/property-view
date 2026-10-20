#!/usr/bin/env python3
"""Auditable review queues and explicitly conditional historical simulations.

Python 3.9+, standard library only. No network calls or automatic cancellations.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))


def timestamp(value, field):
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field}: 无效时间 {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field}: 必须包含时区，例如 +08:00 或 Z")
    return parsed


def nonnegative_int(value, field):
    try:
        number = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{field}: 必须为非负整数") from exc
    if not math.isfinite(number) or number < 0 or not number.is_integer():
        raise ValueError(f"{field}: 必须为非负整数")
    return int(number)


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("CSV 缺少表头或有重复字段名")
        rows = list(reader)
        if any(None in row or None in row.values() for row in rows):
            raise ValueError("CSV 行的列数与表头不一致")
        return reader.fieldnames, rows


def load_records(path, mode="history"):
    fields, raw = read_csv(path)
    required = {
        "viewing_id", "viewing_time", "status_synced_at",
        "region_name", "available_units", "recent_inquiries",
    }
    required.add("is_successful" if mode == "history" else "decision_time")
    if mode != "history":
        required.add("planned_departure_time")
    if required - set(fields):
        raise ValueError(f"CSV 缺少字段: {', '.join(sorted(required - set(fields)))}")
    if not raw:
        raise ValueError("输入没有预约记录")
    seen, records, duplicates = {}, [], 0
    for line, row in enumerate(raw, 2):
        identifier = row["viewing_id"].strip()
        if not identifier:
            raise ValueError(f"第 {line} 行: viewing_id 为空")
        if identifier in seen:
            if row != seen[identifier]:
                raise ValueError(f"第 {line} 行: viewing_id={identifier} 内容冲突，需人工核对")
            duplicates += 1
            continue
        seen[identifier] = row
        viewing = timestamp(row["viewing_time"], "viewing_time")
        synced = timestamp(row["status_synced_at"], "status_synced_at")
        decision = viewing if mode == "history" else timestamp(row["decision_time"], "decision_time")
        departure = None if mode == "history" else timestamp(row["planned_departure_time"], "planned_departure_time")
        if mode != "history" and not decision < departure < viewing:
            raise ValueError(f"预约 {identifier}: 必须满足 decision_time < planned_departure_time < viewing_time")
        if synced > decision:
            raise ValueError(f"预约 {identifier}: 同步时间晚于决策时间，存在未来信息")
        units = nonnegative_int(row["available_units"], "available_units")
        inquiries = nonnegative_int(row["recent_inquiries"], "recent_inquiries")
        result = None
        if mode == "history" and row["is_successful"].strip():
            text = row["is_successful"].strip()
            if text not in ("0", "1", "0.0", "1.0"):
                raise ValueError(f"预约 {identifier}: is_successful 只能为 0、1 或空")
            result = int(float(text))
        region = row["region_name"].strip()
        if not region:
            raise ValueError(f"预约 {identifier}: region_name 为空")
        records.append({
            "viewing_id": identifier,
            "viewing_time": viewing,
            "decision_time": decision,
            "planned_departure_time": departure,
            "status_synced_at": synced,
            "region_name": region,
            "property_category": row.get("property_category", "").strip(),
            "available_units": units,
            "recent_inquiries": inquiries,
            "age_minutes": (decision - synced).total_seconds() / 60,
            "is_successful": result,
        })
    records.sort(key=lambda row: (row["viewing_time"], row["viewing_id"]))
    return records, {"raw_rows": len(raw), "duplicate_rows_removed": duplicates,
                     "unique_viewings": len(records)}


def load_policy(path):
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"version", "stale_regions", "stale_after_minutes", "low_stock_region",
                "low_stock_max_units", "evaluation_start", "assumptions", "simulation_targets"}
    if required - policy.keys():
        raise ValueError(f"策略缺少字段: {sorted(required - policy.keys())}")
    regions = policy["stale_regions"]
    if not isinstance(regions, list) or not regions or any(not isinstance(x, str) or not x for x in regions):
        raise ValueError("stale_regions 必须是非空区域列表")
    if not isinstance(policy["low_stock_region"], str) or not policy["low_stock_region"]:
        raise ValueError("low_stock_region 必须为区域名")
    if policy["low_stock_region"] in regions or len(regions) != len(set(regions)):
        raise ValueError("区域策略不可重复或重叠")
    age = policy["stale_after_minutes"]
    if not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0:
        raise ValueError("stale_after_minutes 必须为有限非负数")
    policy["low_stock_max_units"] = nonnegative_int(policy["low_stock_max_units"], "low_stock_max_units")
    date.fromisoformat(policy["evaluation_start"])
    assumptions = policy["assumptions"]
    for key in ("minutes_per_empty_trip", "minutes_per_review"):
        number = assumptions[key]
        if not isinstance(number, (int, float)) or not math.isfinite(number) or number <= 0:
            raise ValueError(f"{key} 必须为正数")
    for key in ("preventable_fraction", "false_unavailable_fraction"):
        if not 0 <= assumptions[key] <= 1:
            raise ValueError(f"{key} 必须在 0 到 1 之间")
    targets = policy["simulation_targets"]
    for key in ("min_empty_trip_reduction", "max_successful_visit_disruption"):
        if not 0 <= targets[key] <= 1:
            raise ValueError(f"{key} 必须在 0 到 1 之间")
    if not isinstance(targets["min_net_minutes_saved"], (int, float)) or not math.isfinite(targets["min_net_minutes_saved"]):
        raise ValueError("min_net_minutes_saved 必须为有限数值")
    return policy


def risk_reasons(record, policy):
    """Never reads outcome, viewing ID, inquiry volume, or evaluation period."""
    region = record["region_name"]
    if region in policy["stale_regions"]:
        return ["STALE_STATUS"] if record["age_minutes"] > policy["stale_after_minutes"] else []
    if region == policy["low_stock_region"]:
        return ["LOW_STOCK"] if record["available_units"] <= policy["low_stock_max_units"] else []
    # New regions are not silently certified as low risk.
    return ["UNSUPPORTED_REGION"]


def selected(record, policy, strategy):
    if strategy == "none":
        return False
    if strategy == "all":
        return True
    if strategy == "stale_everywhere":
        return record["age_minutes"] > policy["stale_after_minutes"]
    if strategy == "low_stock_everywhere":
        return record["available_units"] <= policy["low_stock_max_units"]
    if strategy == "regional":
        return bool(risk_reasons(record, policy))
    raise ValueError(f"未知策略 {strategy}")


def fraction(numerator, denominator):
    return numerator / denominator if denominator else None


def evaluate(records, policy, strategy="regional", preventable=None, false_unavailable=None):
    """Counterfactual simulation; unknown labels incur cost but receive no benefit."""
    a = policy["assumptions"]
    sensitivity = a["preventable_fraction"] if preventable is None else preventable
    false_rate = a["false_unavailable_fraction"] if false_unavailable is None else false_unavailable
    if not 0 <= sensitivity <= 1 or not 0 <= false_rate <= 1:
        raise ValueError("模拟概率必须在 0 到 1 之间")
    counts = Counter(r["is_successful"] for r in records)
    queued = [r for r in records if selected(r, policy, strategy)]
    hits = Counter(r["is_successful"] for r in queued)
    failures, successes = counts[0], counts[1]
    avoided = hits[0] * sensitivity
    disrupted = hits[1] * false_rate
    cost = len(queued) * a["minutes_per_review"]
    saving = avoided * a["minutes_per_empty_trip"] - cost
    result = {
        "strategy": strategy, "total": len(records),
        "known": failures + successes, "successful": successes,
        "failed": failures, "unknown": counts[None],
        "baseline_failure_rate_known": fraction(failures, failures + successes),
        "review_count": len(queued), "review_share": fraction(len(queued), len(records)),
        "targeted_failures": hits[0], "targeted_successes": hits[1], "targeted_unknown": hits[None],
        "precision_known": fraction(hits[0], hits[0] + hits[1]),
        "recall_known": fraction(hits[0], failures),
        "assumed_preventable_fraction": sensitivity,
        "assumed_false_unavailable_fraction": false_rate,
        "expected_avoided_failures": avoided,
        "expected_remaining_failures": failures - avoided,
        "expected_failure_reduction": fraction(avoided, failures),
        # Fixed denominator: never manufacture a lower rate by dropping reviews.
        "expected_failure_rate_original_known_denominator": fraction(failures - avoided, failures + successes),
        "expected_disrupted_successes": disrupted,
        "expected_success_disruption_rate": fraction(disrupted, successes),
        "review_cost_minutes": cost, "expected_net_minutes_saved": saving,
        "required_preventable_fraction_for_break_even": fraction(cost, hits[0] * a["minutes_per_empty_trip"]),
        "unknown_benefit_assumed": 0,
        "worst_unknown_failure_reduction": fraction(avoided, failures + counts[None]),
    }
    targets = policy["simulation_targets"]
    result["simulation_target_checks"] = {
        "failure_reduction": result["expected_failure_reduction"] is not None
            and result["expected_failure_reduction"] >= targets["min_empty_trip_reduction"],
        "successful_visit_disruption": result["expected_success_disruption_rate"] is not None
            and result["expected_success_disruption_rate"] <= targets["max_successful_visit_disruption"],
        "net_minutes_saved": saving > targets["min_net_minutes_saved"],
    }
    return result


QUEUE_FIELDS = [
    "viewing_id", "policy_version", "mode", "viewing_time", "decision_time",
    "planned_departure_time", "status_synced_at", "region_name", "property_category", "available_units",
    "recent_inquiries", "age_minutes", "needs_review", "reason",
    "workflow_status", "next_action",
]


def queue_rows(records, policy, mode):
    for record in records:
        reasons = risk_reasons(record, policy)
        row = {key: value.isoformat() if isinstance(value, datetime) else value
               for key, value in record.items() if key in QUEUE_FIELDS}
        row.update({
            "policy_version": policy["version"], "mode": mode,
            "needs_review": int(bool(reasons)), "reason": ";".join(reasons),
            "workflow_status": "pending_review" if reasons else "standard_process",
            "next_action": "确认具体房源；未确认前人工处理" if reasons else "沿用常规流程；不代表已确认可看",
        })
        yield row


def write_csv(path, fields, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def pct(value):
    return "不适用" if value is None else f"{value:.1%}"


def markdown_report(records, quality, evaluations, policy, source_hash, sensitivities):
    full, earlier, later = (evaluations[x]["regional"] for x in ("all", "earlier", "later"))
    a = policy["assumptions"]
    lines = [
        "# 带看风险复核与历史模拟",
        "",
        "**这是历史诊断与条件化模拟，不是线上已减少的跑空。** 规则是在已讨论过全量数据后冻结的；按日期拆分仅检查稳定性，不是未见测试集验证。",
        "历史时延由到场快照计算。线上必须使用出发前当时可获得的快照，不能回填到场时的信息。",
        "",
        "## 数据质量与基线",
        "",
        f"- 原始 {quality['raw_rows']} 行，移除 {quality['duplicate_rows_removed']} 条完全重复记录，保留 {len(records)} 次预约。相同 ID 内容冲突会停止运行。",
        f"- 已回传 {full['known']} 次：成功 {full['successful']}，跑空 {full['failed']}；未回传 {full['unknown']}，不会填为成功。",
        f"- 已回传跑空率 {pct(full['baseline_failure_rate_known'])}；已知跑空时间损失 {full['failed'] * a['minutes_per_empty_trip'] / 60:.1f} 小时。",
        f"- 若未回传全部成功或全部失败，全量跑空率范围 {pct(full['failed']/full['total'])}—{pct((full['failed']+full['unknown'])/full['total'])}。",
        "- 所有时间按时区解析后相减；CSV 的 +08:00 与 Z 不按字符串直接相减。",
        f"- CSV SHA-256：`{source_hash}`",
        "",
        "## 分区域依据",
        "",
        "| 大区 | 较新状态：跑空/已回传 | 旧状态：跑空/已回传 |",
        "|---|---:|---:|",
    ]
    for region in sorted({r["region_name"] for r in records}):
        cells = []
        for stale in (False, True):
            group = [r for r in records if r["region_name"] == region and
                     (r["age_minutes"] > policy["stale_after_minutes"]) == stale]
            count = Counter(r["is_successful"] for r in group)
            cells.append(f"{count[0]}/{count[0]+count[1]}（{pct(fraction(count[0],count[0]+count[1]))}）")
        lines.append(f"| {region} | {' | '.join(cells)} |")
    age_values = sorted({r["age_minutes"] for r in records})
    below = [x for x in age_values if x <= policy["stale_after_minutes"]]
    above = [x for x in age_values if x > policy["stale_after_minutes"]]
    if below and above:
        lines.extend(["", f"分组边界设为 {policy['stale_after_minutes']} 分钟；边界两侧最近观测为 {max(below):g} 和 {min(above):g} 分钟。不能由此证明精确最佳阈值。"])
    lines.extend(["", "| 华南库存 | 跑空/已回传 |", "|---|---:|"])
    for low in (True, False):
        group = [r for r in records if r["region_name"] == policy["low_stock_region"] and
                 (r["available_units"] <= policy["low_stock_max_units"]) == low]
        count = Counter(r["is_successful"] for r in group)
        lines.append(f"| {'≤' if low else '>'}{policy['low_stock_max_units']} 套 | {count[0]}/{count[0]+count[1]}（{pct(fraction(count[0],count[0]+count[1]))}） |")
    zero = Counter(r["is_successful"] for r in records if r["available_units"] == 0)
    lines.extend([
        "", f"库存为 0 的记录有 {sum(zero.values())} 次，其中 {zero[1]} 次正常带看；保留原数据，不将其自动判成不可看。",
        "",
        "## 冻结规则与时间切分",
        "",
        f"- `{policy['version']}`：{ '、'.join(policy['stale_regions']) }状态超过 {policy['stale_after_minutes']} 分钟，或{policy['low_stock_region']}库存 ≤{policy['low_stock_max_units']} 套，进入复核。",
        "- 评分只读取区域、决策时刻距最后同步的分钟数、库存；不读取结果、ID、日期或咨询量。结果仅用于评估。",
        "- 未支持的新区域转人工复核；不得自动认为低风险。",
        f"- 较早日期：当地日期 < {policy['evaluation_start']}；较晚日期：≥ {policy['evaluation_start']}。没有在较晚日期上调参。",
        "",
        "## 默认模拟：减少跑空与正常带看保护",
        "",
        f"假设：每次复核预算 {a['minutes_per_review']:g} 分钟；命中跑空中 {pct(a['preventable_fraction'])} 能在出发前确认并避免；命中正常带看中 {pct(a['false_unavailable_fraction'])} 被误判不可看。",
        "**20 分钟来自题目的提前标记成本；将其作为每次复核总成本只是预算假设，需用实际人工耗时替换。**",
        "未回传样本仍消耗复核成本，但不计任何避免跑空收益。误判带来的成交、体验损失未折算为分钟，另列正常带看受损指标。",
        "",
        "| 范围 | 已知跑空 | 复核次数 | 命中跑空 | 预计剩余跑空 | 预计减少 | 净节省小时 | 正常带看预计受损 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for label, result in (("全量", full), ("较早日期", earlier), ("较晚日期", later)):
        lines.append(f"| {label} | {result['failed']} | {result['review_count']} | {result['targeted_failures']} | {result['expected_remaining_failures']:.1f} | {pct(result['expected_failure_reduction'])} | {result['expected_net_minutes_saved']/60:.1f} | {result['expected_disrupted_successes']:.2f}（{pct(result['expected_success_disruption_rate'])}） |")
    lines.extend([
        "",
        "以上小数为期望值，不是实测整数事件。跑空率始终以原已回传预约数为分母；不通过删掉待复核或取消记录制造改善。",
        f"全量命中正常带看 {full['targeted_successes']} 次、未知结果 {full['targeted_unknown']} 次，说明**不能将风险规则直接变成取消规则**。",
        f"全量在未回传全部实际跑空、且一例未知也未避免的情景下，预计减少比例为 {pct(full['worst_unknown_failure_reduction'])}。",
        "",
        "## 与简单策略比较（较晚日期，同一成本与确认假设）",
        "",
        "| 策略 | 复核次数 | 命中跑空/全部跑空 | 正常带看进入复核 | 预计减少 | 净节省小时 |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    names = {"none": "不干预", "all": "全量复核", "stale_everywhere": "所有区域旧状态复核",
             "low_stock_everywhere": "所有区域低库存复核", "regional": "分区域规则"}
    for strategy, result in evaluations["later"].items():
        lines.append(f"| {names[strategy]} | {result['review_count']} | {result['targeted_failures']}/{result['failed']} | {result['targeted_successes']} | {pct(result['expected_failure_reduction'])} | {result['expected_net_minutes_saved']/60:.1f} |")
    lines.extend([
        "",
        "## 敏感性：复核有多有效，才值得做",
        "",
        "| 命中跑空中实际可避免比例 | 较晚日期预计减少 | 较晚日期净节省小时 |",
        "|---|---:|---:|",
    ])
    for result in sensitivities:
        lines.append(f"| {pct(result['assumed_preventable_fraction'])} | {pct(result['expected_failure_reduction'])} | {result['expected_net_minutes_saved']/60:.1f} |")
    lines.extend([
        "",
        f"较晚日期时间成本盈亏平衡要求：至少避免命中跑空的 {pct(later['required_preventable_fraction_for_break_even'])}。",
        "公式：净节省分钟 = 命中已知跑空 × 可避免比例 × 每次跑空损失 − 全部复核次数 × 每次复核耗时。",
        "",
        "## 模拟目标检查",
        "",
        f"配置目标：跑空减少至少 {pct(policy['simulation_targets']['min_empty_trip_reduction'])}；正常带看受损不超过 {pct(policy['simulation_targets']['max_successful_visit_disruption'])}；净节省超过 {policy['simulation_targets']['min_net_minutes_saved']:g} 分钟。",
        f"较晚日期默认假设检查：`{json.dumps(later['simulation_target_checks'], ensure_ascii=False)}`。",
        "**通过模拟目标不等于上线验收通过。** 真正验收需要出发前快照、实际复核结果和对照组。",
        "",
        "## 交付文件与实际使用",
        "",
        "- `historical_review_queue.csv`：仅历史命中的复核候选，不可作为当前预约清单执行。",
        "- `metrics.json`：数据质量、策略配置、分时间段对照和敏感性结果。",
        "- `triage`：接收含 decision_time、planned_departure_time 的真实出发前快照，生成逐单动作，严格拒绝未来信息。",
        "- `resolve`：记录确认人、具体房源和确认时点；确认不可看才建议更换房源或改约。未知结果保持人工处理，不默认放行。计划出发时间之后的确认标记为过晚。",
        "",
        "当前无法确认的根因包括同步链路故障、源头状态未上报、具体房源与同类库存映射错误、预约占用缺失。应补充 property_id、出发时间、状态变更日志、不可看原因与回传时间，再开展对照试点。",
    ])
    return "\n".join(lines) + "\n"


def analyze(args):
    policy = load_policy(args.policy)
    records, quality = load_records(args.input)
    split = date.fromisoformat(policy["evaluation_start"])
    partitions = {
        "all": records,
        "earlier": [r for r in records if r["viewing_time"].astimezone(CHINA).date() < split],
        "later": [r for r in records if r["viewing_time"].astimezone(CHINA).date() >= split],
    }
    if not partitions["earlier"] or not partitions["later"]:
        raise ValueError("时间切分两侧都必须有记录，请调整 evaluation_start")
    strategies = ("none", "all", "stale_everywhere", "low_stock_everywhere", "regional")
    evaluations = {
        period: {strategy: evaluate(rows, policy, strategy) for strategy in strategies}
        for period, rows in partitions.items()
    }
    sensitivities = [evaluate(partitions["later"], policy, preventable=x)
                     for x in (0, .4, .6, .8, 1)]
    source_hash = hashlib.sha256(Path(args.input).read_bytes()).hexdigest()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"evidence_type": "retrospective_conditional_simulation",
               "unseen_test_set": False, "source_sha256": source_hash,
               "policy": policy, "quality": quality, "evaluations": evaluations,
               "later_sensitivity": sensitivities}
    (out / "metrics.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    (out / "analysis.md").write_text(markdown_report(records, quality, evaluations, policy, source_hash, sensitivities), encoding="utf-8")
    write_csv(out / "historical_review_queue.csv", QUEUE_FIELDS,
              (row for row in queue_rows(records, policy, "retrospective") if row["needs_review"]))
    later = evaluations["later"]["regional"]
    print(f"历史模拟已生成：{out / 'analysis.md'}")
    print(f"较晚日期：跑空 {later['failed']} → 预计 {later['expected_remaining_failures']:.1f}，"
          f"预计减少 {pct(later['expected_failure_reduction'])}，净节省 {later['expected_net_minutes_saved']/60:.1f} 小时。")
    print("以上依赖配置中的复核有效率/成本假设，尚无线上干预证据。")


def triage(args):
    policy = load_policy(args.policy)
    records, quality = load_records(args.input, mode="predeparture")
    rows = list(queue_rows(records, policy, "predeparture"))
    write_csv(args.output, QUEUE_FIELDS, rows)
    print(f"已处理 {quality['unique_viewings']} 次预约，需复核 {sum(r['needs_review'] for r in rows)} 次：{args.output}")


RESOLUTION_FIELDS = ["confirmation", "confirmed_at", "confirmed_by", "property_id", "confirmation_note"]


def resolve(args):
    fields, rows = read_csv(args.queue)
    if set(QUEUE_FIELDS) - set(fields):
        raise ValueError("队列字段不完整，请使用 triage 生成")
    if any(r["mode"] != "predeparture" for r in rows):
        raise ValueError("历史模拟队列不可执行，请使用真实出发前快照")
    if len({r["viewing_id"] for r in rows}) != len(rows):
        raise ValueError("队列存在重复 ID")
    confirm_fields, confirmations = read_csv(args.confirmations)
    if {"viewing_id", "confirmation", "confirmed_at", "confirmed_by", "property_id"} - set(confirm_fields):
        raise ValueError("确认表须包含 viewing_id、confirmation、confirmed_at、confirmed_by、property_id")
    by_id, ids = {}, {r["viewing_id"] for r in rows}
    for confirmation in confirmations:
        key = confirmation["viewing_id"]
        if key not in ids or key in by_id:
            raise ValueError(f"确认表包含队列外或重复 ID: {key}")
        if confirmation["confirmation"] not in ("available", "unavailable", "unknown"):
            raise ValueError("confirmation 必须是 available、unavailable 或 unknown")
        if not confirmation["confirmed_by"].strip():
            raise ValueError(f"预约 {key}: 必须记录确认人")
        if confirmation["confirmation"] != "unknown" and not confirmation["property_id"].strip():
            raise ValueError(f"预约 {key}: 确认可看/不可看必须关联具体房源 ID")
        timestamp(confirmation["confirmed_at"], "confirmed_at")
        by_id[key] = confirmation
    output = []
    for row in rows:
        result = dict(row)
        result.update({key: "" for key in RESOLUTION_FIELDS})
        confirmation = by_id.get(row["viewing_id"])
        if confirmation:
            at = timestamp(confirmation["confirmed_at"], "confirmed_at")
            decision = timestamp(row["decision_time"], "decision_time")
            departure = timestamp(row["planned_departure_time"], "planned_departure_time")
            if at < decision:
                raise ValueError(f"预约 {row['viewing_id']}: 确认时间早于本次复核决策")
            result.update({key: confirmation.get(key, "") for key in RESOLUTION_FIELDS})
            if at >= departure:
                result.update(workflow_status="late_confirmation", next_action="确认过晚，人工跟进；不得计为提前避免跑空")
            elif confirmation["confirmation"] == "available":
                result.update(workflow_status="confirmed_available", next_action="已确认具体房源在记录时点可看；出发有延迟须重新确认")
            elif confirmation["confirmation"] == "unavailable":
                result.update(workflow_status="confirmed_unavailable", next_action="建议联系租客并更换房源或改约；工具未自动取消")
            else:
                result.update(workflow_status="pending_review", next_action="状态未确认，转人工处理")
        output.append(result)
    write_csv(args.output, QUEUE_FIELDS + RESOLUTION_FIELDS, output)
    print(f"已生成 {len(output)} 条处理记录：{args.output}（未发送消息或自动取消预约）")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    analysis = commands.add_parser("analyze", help="历史诊断、固定规则回放和条件化效果模拟")
    analysis.add_argument("--input", default=str(ROOT / "property_viewings.csv"))
    analysis.add_argument("--policy", default=str(ROOT / "risk_policy.json"))
    analysis.add_argument("--output", default=str(ROOT / "reports"))
    analysis.set_defaults(run=analyze)
    upcoming = commands.add_parser("triage", help="处理真实出发前快照，生成复核队列")
    upcoming.add_argument("--input", required=True)
    upcoming.add_argument("--policy", default=str(ROOT / "risk_policy.json"))
    upcoming.add_argument("--output", required=True)
    upcoming.set_defaults(run=triage)
    resolution = commands.add_parser("resolve", help="记录人工确认并生成下一步动作")
    resolution.add_argument("--queue", required=True)
    resolution.add_argument("--confirmations", required=True)
    resolution.add_argument("--output", required=True)
    resolution.set_defaults(run=resolve)
    args = parser.parse_args(argv)
    # Never overwrite source inputs through output arguments.
    sources = [Path(getattr(args, k)).resolve() for k in ("input", "policy", "queue", "confirmations") if hasattr(args, k)]
    output = Path(args.output).resolve()
    targets = ([output / name for name in ("analysis.md", "metrics.json", "historical_review_queue.csv")]
               if args.command == "analyze" else [output])
    if any(path in sources for path in targets) or output in sources:
        parser.error("输出不可覆盖输入数据或策略文件")
    try:
        args.run(args)
    except (ValueError, KeyError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
