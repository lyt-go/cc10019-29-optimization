"""留存批次服务：合并读取留存事件、登记批次明细，并支持续做与结果复用。

设计要点：

* 同一批次里相同事件输入只生成一次事件数据（``_load_events``），同一分段维度
  也只聚合一次（``_aggregate``），避免逐条配置重复扫描。
* 批次按“组”登记明细：每个配置是一组，组上记录本次使用的入组分母、
  回访区间和结果版本；批次被确认后才允许跨批次复用。
* 明细带 ``status``（待算/待重算/已算），中断后可用 ``resume_retention_batch``
  只补算未完成组，已算组原样保留。
* 补报只允许更正已算完的组，更正会作废旧结果版本，但不会重扫事件数据——
  重试时仅用新的入组分母重算留存率。
* LTV 校准只接受已确认批次的明细（``get_confirmed_retention_curve``），
  未确认的入组数据不会和已有曲线混用。
"""
from __future__ import annotations

import copy
import datetime
from typing import Any

from app.schemas import RetentionResult
from app.services.retention_calc import _add_days, _generate_retention_events
from app.store import store

PENDING = "待算"
RECALC = "待重算"
DONE = "已算"

BATCH_RUNNING = "计算中"
BATCH_TO_CONFIRM = "待确认"
BATCH_CONFIRMED = "已确认"

_BATCHES = "retention_batch"
_QUEUE = "retention_queue"
_OVERRIDES = "retention_cohort_override"
_ARTIFACT = "retention_batch"


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _batch_rows() -> list[dict[str, Any]]:
    return store.rows(_BATCHES)


def _queue_rows() -> list[dict[str, Any]]:
    return store.rows(_QUEUE)


def _find_batch(batch_id: str) -> dict[str, Any] | None:
    return store.find_by_field(_BATCHES, "batch_id", batch_id)


def _require_batch(batch_id: str) -> dict[str, Any]:
    batch = _find_batch(batch_id)
    if batch is None:
        raise ValueError(f"Retention batch not found: {batch_id}")
    return batch


def _require_config(config_id: str) -> dict[str, Any]:
    config = store.find_by_field("retention_calc", "config_id", config_id)
    if config is None:
        raise ValueError(f"Retention config not found: {config_id}")
    return config


def _cohort_key(config: dict[str, Any]) -> str:
    """入组与回访条件的指纹；条件不变时结果仍可复用。"""
    return "|".join(
        [
            str(config.get("init_event", "")),
            str(config.get("return_event", "")),
            str(config.get("base_date", "")),
            str(config.get("retention_window", 0)),
            str(config.get("calc_period", "day")),
            str(config.get("segment_dim", "")),
        ]
    )


def _event_fingerprint(config: dict[str, Any]) -> str:
    """事件输入指纹：生成模拟事件只依赖初始事件、基准日期和窗口。"""
    return "|".join(
        [
            str(config.get("init_event", "")),
            str(config.get("base_date", "")),
            str(config.get("retention_window", 0)),
        ]
    )


def _return_window(config: dict[str, Any]) -> dict[str, Any]:
    start = str(config["base_date"])
    days = int(config.get("retention_window", 30))
    return {"start": start, "end": _add_days(start, days - 1), "days": days}


def _public_batch(batch: dict[str, Any]) -> dict[str, Any]:
    """返回调用方可见的批次结构（深拷贝，防止外部改到内存仓库）。"""
    return copy.deepcopy(batch)


# ---------------------------------------------------------------------------
# 事件读取与聚合：一批内共享
# ---------------------------------------------------------------------------

def _load_events(config: dict[str, Any], stats: dict[str, int]) -> list[dict[str, Any]]:
    """按事件输入指纹缓存事件数据，同一时段只读取一次。"""
    fingerprint = _event_fingerprint(config)
    cache_key = f"retention_events:{fingerprint}"
    cached = store.get_compute_cache(cache_key)
    if cached is not None:
        stats["input_cache_hits"] += 1
        return cached

    events = _generate_retention_events(
        str(config["init_event"]),
        str(config["base_date"]),
        int(config.get("retention_window", 30)),
    )
    store.set_compute_cache(cache_key, events)
    stats["input_scans"] += 1
    return events


def _aggregate(config: dict[str, Any], stats: dict[str, int]) -> dict[str, Any]:
    """按（事件输入, 分段维度）聚合一次，供同批次所有组复用。"""
    segment_dim = str(config.get("segment_dim", "") or "")
    cache_key = f"retention_agg:{_event_fingerprint(config)}|{segment_dim}"
    cached = store.get_compute_cache(cache_key)
    if cached is not None:
        stats["aggregate_cache_hits"] += 1
        return cached

    events = _load_events(config, stats)
    date_users: dict[str, set[str]] = {}
    dim_date_users: dict[str, dict[str, set[str]]] = {}
    for event in events:
        day = event["date"]
        user_id = event["user_id"]
        date_users.setdefault(day, set()).add(user_id)
        if segment_dim:
            dim_value = str(event.get(segment_dim, "unknown"))
            dim_date_users.setdefault(dim_value, {}).setdefault(day, set()).add(user_id)

    aggregate = {"date_users": date_users, "dim_date_users": dim_date_users}
    store.set_compute_cache(cache_key, aggregate)
    stats["aggregations_built"] += 1
    return aggregate


def _curve_from_aggregate(
    aggregate: dict[str, Any],
    config: dict[str, Any],
    denominator: int | None = None,
) -> dict[str, Any]:
    """把聚合结果换算成留存曲线；分母可被补报更正。"""
    base_date = str(config["base_date"])
    window = int(config.get("retention_window", 30))
    segment_dim = str(config.get("segment_dim", "") or "")

    date_users: dict[str, set[str]] = aggregate["date_users"]
    init_users = date_users.get(base_date, set())
    cohort_denominator = len(init_users)
    effective_denominator = cohort_denominator if denominator is None else denominator

    retained_counts: list[int] = []
    rates: list[float] = []
    for day in range(window):
        target = date_users.get(_add_days(base_date, day), set())
        retained = len(init_users & target)
        retained_counts.append(retained)
        rates.append(round(retained / effective_denominator, 4) if effective_denominator else 0.0)

    cohort_sizes: dict[str, int] = {base_date: effective_denominator}
    retention_rates: dict[str, list[float]] = {base_date: rates}
    segment_denominators: dict[str, int] = {}
    segment_retained_counts: dict[str, list[int]] = {}

    if segment_dim:
        for dim_value, dim_dates in aggregate["dim_date_users"].items():
            key = f"{segment_dim}:{dim_value}"
            dim_init = dim_dates.get(base_date, set())
            dim_denominator = len(dim_init)
            segment_denominators[key] = dim_denominator
            cohort_sizes[key] = dim_denominator
            dim_counts: list[int] = []
            dim_rates: list[float] = []
            for day in range(window):
                target = dim_dates.get(_add_days(base_date, day), set())
                count = len(dim_init & target)
                dim_counts.append(count)
                dim_rates.append(round(count / dim_denominator, 4) if dim_denominator else 0.0)
            segment_retained_counts[key] = dim_counts
            retention_rates[key] = dim_rates

    return {
        "cohort_denominator": effective_denominator,
        "raw_cohort_denominator": cohort_denominator,
        "cohort_sizes": cohort_sizes,
        "retention_rates": retention_rates,
        "retained_counts": retained_counts,
        "segment_denominators": segment_denominators,
        "segment_retained_counts": segment_retained_counts,
        "calibration_curve": rates,
    }


# ---------------------------------------------------------------------------
# 队列与版本
# ---------------------------------------------------------------------------

def _next_version(config_id: str) -> int:
    versions = [
        int(row.get("result_version", 0))
        for row in _queue_rows()
        if row.get("config_id") == config_id
    ]
    return max(versions, default=0) + 1


def _find_reusable(config: dict[str, Any]) -> dict[str, Any] | None:
    """查找仍然有效且已确认的同条件结果。"""
    key = _cohort_key(config)
    candidates = [
        row
        for row in _queue_rows()
        if row.get("config_id") == config["config_id"]
        and row.get("cohort_key") == key
        and row.get("valid")
        and row.get("confirmed")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda row: (int(row.get("result_version", 0)), int(row.get("id", 0))))


def _append_queue(
    batch_id: str,
    config: dict[str, Any],
    detail: dict[str, Any],
    *,
    confirmed: bool,
    reused_from: dict[str, Any] | None,
) -> None:
    """逐条登记一组本次使用的分母、回访区间与结果版本。"""
    store.insert(
        _QUEUE,
        {
            "batch_id": batch_id,
            "config_id": config["config_id"],
            "cohort_key": detail["cohort_key"],
            "cohort_denominator": detail["cohort_denominator"],
            "return_window": copy.deepcopy(detail["return_window"]),
            "result_version": detail["result_version"],
            "retention_rates": copy.deepcopy(detail["retention_rates"]),
            "retained_counts": list(detail["retained_counts"]),
            "segment_denominators": copy.deepcopy(detail["segment_denominators"]),
            "segment_retained_counts": copy.deepcopy(detail["segment_retained_counts"]),
            "cohort_sizes": copy.deepcopy(detail["cohort_sizes"]),
            "calibration_curve": list(detail["calibration_curve"]),
            "valid": True,
            "confirmed": confirmed,
            "reused": reused_from is not None,
            "source_batch_id": (reused_from or {}).get("batch_id", batch_id),
            "created_at": _now(),
        },
    )


def _invalidate_config_queue(config_id: str) -> None:
    """条件被更正后，旧版本不可再被其他批次复用。"""
    for row in _queue_rows():
        if row.get("config_id") == config_id:
            row["valid"] = False


def _save_override(batch_id: str, detail: dict[str, Any]) -> None:
    """补报的分母是确认后的修正值，后续重算也必须沿用。"""
    if not detail.get("correction"):
        return
    store.set_compute_cache(
        f"{_OVERRIDES}:{detail['config_id']}",
        {
            "cohort_denominator": int(detail["cohort_denominator"]),
            "previous_denominator": detail["correction"].get("previous_denominator"),
            "remark": detail["correction"].get("remark", ""),
            "reported_at": detail["correction"].get("reported_at"),
            "batch_id": batch_id,
            "result_version": detail["result_version"],
        },
    )


def _get_override(config_id: str) -> dict[str, Any] | None:
    return store.get_compute_cache(f"{_OVERRIDES}:{config_id}")


def _set_batch_queue_confirmed(batch_id: str, confirmed: bool) -> None:
    for row in _queue_rows():
        if row.get("batch_id") == batch_id:
            row["confirmed"] = confirmed


# ---------------------------------------------------------------------------
# 批次明细
# ---------------------------------------------------------------------------

def _new_group(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "group_key": config["config_id"],
        "config_id": config["config_id"],
        "cohort_key": _cohort_key(config),
        "cohort_denominator": 0,
        "segment_denominators": {},
        "return_window": _return_window(config),
        "result_version": 0,
        "status": PENDING,
        "confirmed": False,
        "reused": False,
        "reused_from": None,
        "retained_counts": [],
        "segment_retained_counts": {},
        "retention_rates": {},
        "cohort_sizes": {},
        "calibration_curve": [],
        "affected_by_correction": False,
        "correction": None,
        "computed_at": None,
    }


def _apply_curve(detail: dict[str, Any], curve: dict[str, Any]) -> None:
    for field in (
        "cohort_denominator",
        "cohort_sizes",
        "retention_rates",
        "retained_counts",
        "segment_denominators",
        "segment_retained_counts",
        "calibration_curve",
    ):
        detail[field] = curve[field]


def _compute_group(
    batch_id: str,
    detail: dict[str, Any],
    stats: dict[str, int],
    *,
    force: bool = False,
) -> None:
    config = _require_config(detail["config_id"])

    if not force:
        reusable = _find_reusable(config)
        if reusable is not None:
            # 复用已确认结果：不再读取事件、也不再生成聚合。
            for field in (
                "cohort_denominator",
                "cohort_sizes",
                "retention_rates",
                "retained_counts",
                "segment_denominators",
                "segment_retained_counts",
                "calibration_curve",
                "result_version",
            ):
                detail[field] = copy.deepcopy(reusable[field])
            detail["status"] = DONE
            detail["confirmed"] = False
            detail["reused"] = True
            detail["reused_from"] = {
                "batch_id": reusable.get("batch_id"),
                "result_version": reusable.get("result_version"),
            }
            detail["computed_at"] = _now()
            stats["reused"] += 1
            _append_queue(
                batch_id,
                config,
                detail,
                confirmed=False,
                reused_from=detail["reused_from"],
            )
            return

    aggregate = _aggregate(config, stats)
    curve = _curve_from_aggregate(aggregate, config)
    detail["result_version"] = _next_version(config["config_id"])
    _apply_curve(detail, curve)
    # 补报更正的分母是权威值，重新计算时沿用，不能被重扫出来的原始分母覆盖。
    override = _get_override(detail["config_id"])
    if override is not None:
        denominator = int(override["cohort_denominator"])
        base_date = str(config["base_date"])
        detail["cohort_denominator"] = denominator
        detail["cohort_sizes"][base_date] = denominator
        detail["retention_rates"][base_date] = [
            round(min(count, denominator) / denominator, 4) if denominator else 0.0
            for count in detail["retained_counts"]
        ]
        detail["calibration_curve"] = detail["retention_rates"][base_date]
    detail["status"] = DONE
    detail["confirmed"] = False
    detail["reused"] = False
    detail["reused_from"] = None
    detail["computed_at"] = _now()
    stats["computed"] += 1
    _append_queue(batch_id, config, detail, confirmed=False, reused_from=None)


def _recompute_corrected(
    batch_id: str,
    detail: dict[str, Any],
    stats: dict[str, int],
) -> None:
    """补报后重算：只换分母，沿用已算出的保留人数。"""
    config = _require_config(detail["config_id"])
    denominator = int(detail["cohort_denominator"])

    if not detail.get("retained_counts"):
        # 没有留存人数明细时退回完整计算，保证结果可用。
        _compute_group(batch_id, detail, stats, force=True)
        denominator = int(detail["cohort_denominator"])
    else:
        base_date = str(config["base_date"])
        # 补报可能把分母调小；保留人数不会超过入组人数，按新分母截断后再算比率。
        rates = [
            round(min(count, denominator) / denominator, 4) if denominator else 0.0
            for count in detail["retained_counts"]
        ]
        detail["retention_rates"][base_date] = rates
        detail["cohort_sizes"][base_date] = denominator
        detail["calibration_curve"] = rates

    detail["result_version"] = _next_version(config["config_id"])
    detail["status"] = DONE
    detail["confirmed"] = False
    detail["reused"] = False
    detail["reused_from"] = None
    detail["computed_at"] = _now()
    correction = detail.get("correction") or {}
    correction["resolved_version"] = detail["result_version"]
    correction["resolved_at"] = _now()
    detail["correction"] = correction
    _save_override(batch_id, detail)
    stats["recomputed"] += 1
    _append_queue(batch_id, config, detail, confirmed=False, reused_from=None)


def _refresh_batch_status(batch: dict[str, Any]) -> None:
    groups = batch["groups"]
    statuses = [group["status"] for group in groups]
    if batch.get("confirmed") and all(status == DONE for status in statuses):
        batch["status"] = BATCH_CONFIRMED
    elif any(status in (PENDING, RECALC) for status in statuses):
        batch["status"] = BATCH_RUNNING
    else:
        batch["status"] = BATCH_TO_CONFIRM
    batch["pending_count"] = sum(1 for status in statuses if status in (PENDING, RECALC))
    batch["group_count"] = len(groups)
    batch["reused_count"] = sum(1 for group in groups if group.get("reused"))
    batch["updated_at"] = _now()


def _save_batch(batch: dict[str, Any]) -> None:
    _refresh_batch_status(batch)
    store.set_artifact(_ARTIFACT, batch["batch_id"], _public_batch(batch))


def _execute(
    batch_id: str,
    *,
    limit: int | None = None,
    include_recalc: bool = True,
    only_status: str | None = None,
) -> dict[str, Any]:
    batch = _require_batch(batch_id)
    stats = {"input_scans": 0, "input_cache_hits": 0, "aggregate_cache_hits": 0,
             "aggregations_built": 0, "computed": 0, "recomputed": 0, "reused": 0}

    if only_status is not None:
        targets = [group for group in batch["groups"] if group["status"] == only_status]
    else:
        allowed = [PENDING, RECALC] if include_recalc else [PENDING]
        targets = [group for group in batch["groups"] if group["status"] in allowed]
    if limit is not None:
        targets = targets[: max(int(limit), 0)]

    for group in targets:
        if group["status"] == RECALC:
            _recompute_corrected(batch_id, group, stats)
        else:
            _compute_group(batch_id, group, stats)

    batch["input_scans"] = int(batch.get("input_scans", 0)) + stats["input_scans"]
    batch["input_cache_hits"] = int(batch.get("input_cache_hits", 0)) + stats["input_cache_hits"]
    batch["last_run"] = {"at": _now(), **stats, "executed_groups": [g["group_key"] for g in targets]}
    _save_batch(batch)
    return _public_batch(batch)


# ---------------------------------------------------------------------------
# 对外：登记、提交、续做、重试、补报、确认
# ---------------------------------------------------------------------------

def register_retention_batch(config_ids: list[str], remark: str = "") -> dict[str, Any]:
    """只登记批次明细（全部待算），不执行计算，便于分批续做。"""
    if not config_ids:
        raise ValueError("config_ids 不能为空")
    deduped: list[str] = []
    for config_id in config_ids:
        _require_config(str(config_id))
        if config_id not in deduped:
            deduped.append(str(config_id))

    existing_ids = [int(row.get("id", 0)) for row in _batch_rows()]
    next_id = max(existing_ids, default=0) + 1
    batch = {
        "id": next_id,
        "batch_id": f"RB-{next_id:04d}",
        "status": BATCH_RUNNING,
        "confirmed": False,
        "config_ids": deduped,
        "groups": [_new_group(_require_config(config_id)) for config_id in deduped],
        "revision": 0,
        "pending_count": len(deduped),
        "group_count": len(deduped),
        "reused_count": 0,
        "input_scans": 0,
        "input_cache_hits": 0,
        "remark": remark,
        "retry_summary": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    store.insert(_BATCHES, batch)
    _save_batch(batch)
    return _public_batch(batch)


def submit_retention_batch(
    config_ids: list[str],
    compute_limit: int | None = None,
    remark: str = "",
) -> dict[str, Any]:
    """批量提交：登记后立即计算；``compute_limit`` 可只算前几组，其余留待续做。"""
    batch = register_retention_batch(config_ids, remark=remark)
    return _execute(batch["batch_id"], limit=compute_limit)


def resume_retention_batch(batch_id: str, limit: int | None = None) -> dict[str, Any]:
    """续做：只补算“待算”组，已算组原样保留、不重复读取输入。"""
    return _execute(batch_id, limit=limit, include_recalc=False)


def retry_retention_batch(batch_id: str, limit: int | None = None) -> dict[str, Any]:
    """批量重试：补算待算组，并重算被补报更正过的组。

    返回值里的 ``retry_summary`` 明确列出哪些计算受补报影响。
    """
    batch = _require_batch(batch_id)
    before = {
        group["group_key"]: {
            "status": group["status"],
            "result_version": group["result_version"],
            "affected": bool(group.get("affected_by_correction")),
        }
        for group in batch["groups"]
    }

    executed = _execute(batch_id, limit=limit)
    batch = _require_batch(batch_id)

    recomputed: list[dict[str, Any]] = []
    computed: list[dict[str, Any]] = []
    reused: list[str] = []
    kept: list[str] = []
    affected: list[dict[str, Any]] = []

    for group in batch["groups"]:
        previous = before.get(group["group_key"], {})
        entry = {
            "group_key": group["group_key"],
            "config_id": group["config_id"],
            "previous_version": previous.get("result_version", 0),
            "result_version": group["result_version"],
            "cohort_denominator": group["cohort_denominator"],
            "return_window": group["return_window"],
        }
        if group["status"] != DONE:
            continue
        if previous.get("status") == RECALC and group.get("correction"):
            recomputed.append(entry)
            affected.append(
                {
                    **entry,
                    "reason": "补报更正了入组分母，重算留存率",
                    "previous_denominator": group["correction"].get("previous_denominator"),
                    "cohort_denominator": group["correction"].get("corrected_denominator"),
                }
            )
        elif previous.get("status") == PENDING:
            computed.append(entry)
        elif group.get("reused"):
            reused.append(group["config_id"])
        else:
            kept.append(group["config_id"])

    stale_valuations = _stale_valuations({entry["config_id"] for entry in affected}, batch)
    summary = {
        "batch_id": batch_id,
        "retried_at": _now(),
        "affected_by_correction": affected,
        "recomputed": recomputed,
        "computed": computed,
        "reused": reused,
        "kept": kept,
        "stale_valuations": stale_valuations,
        "message": (
            f"补报影响 {len(affected)} 组计算："
            + ("、".join(item["config_id"] for item in affected) if affected else "无")
            + f"；重算 {len(recomputed)} 组，补算 {len(computed)} 组，"
            + f"复用 {len(reused)} 组，保留 {len(kept)} 组。"
        ),
    }
    batch["retry_summary"] = summary
    batch["revision"] = int(batch.get("revision", 0)) + 1
    _save_batch(batch)
    executed["retry_summary"] = copy.deepcopy(summary)
    executed["status"] = batch["status"]
    executed["pending_count"] = batch["pending_count"]
    return executed


def _apply_supplement(
    batch: dict[str, Any],
    group: dict[str, Any],
    *,
    cohort_denominator: int | None,
    delta: int | None,
    remark: str,
) -> dict[str, Any]:
    if group["status"] != DONE:
        raise ValueError(
            f"补报只允许更正已算完的分组：{group['config_id']} 当前为 {group['status']}"
        )
    previous = int(group["cohort_denominator"])
    new_denominator = int(cohort_denominator) if cohort_denominator is not None else previous + int(delta or 0)
    if new_denominator <= 0:
        raise ValueError("入组分母必须大于 0")
    if new_denominator == previous:
        raise ValueError(f"补报入组人数与当前一致：{previous}")

    group["cohort_denominator"] = new_denominator
    group["status"] = RECALC
    group["confirmed"] = False
    group["affected_by_correction"] = True
    group["correction"] = {
        "previous_denominator": previous,
        "corrected_denominator": new_denominator,
        "remark": remark,
        "reported_at": _now(),
        "resolved_version": None,
        "resolved_at": None,
    }
    _save_override(batch["batch_id"], group)

    # 旧版本立即失效：其他批次不能再用更正前的曲线。
    _invalidate_config_queue(group["config_id"])

    batch["confirmed"] = False
    batch["revision"] = int(batch.get("revision", 0)) + 1
    _set_batch_queue_confirmed(batch["batch_id"], False)
    _refresh_batch_status(batch)

    return {
        "group_key": group["group_key"],
        "config_id": group["config_id"],
        "previous_denominator": previous,
        "cohort_denominator": new_denominator,
        "result_version": group["result_version"],
        "status": group["status"],
        "message": (
            f"已更正 {group['config_id']} 入组分母 {previous} -> {new_denominator}，"
            "留存率待重试后按新分母重算。"
        ),
    }


def report_cohort_supplement(
    batch_id: str,
    *,
    config_id: str | None = None,
    cohort_denominator: int | None = None,
    delta: int | None = None,
    remark: str = "",
    corrections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """补报入组人数。

    * 单组补报：目标必须是已算完的组，否则报错。
    * 批量补报 ``corrections``：只更正其中已算完的组，未算完的组放进 ``skipped``，
      不会被“顺带”算掉。
    """
    batch = _require_batch(batch_id)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if corrections:
        for item in corrections:
            target_id = str(item.get("config_id", ""))
            group = next((g for g in batch["groups"] if g["config_id"] == target_id), None)
            if group is None:
                skipped.append({"config_id": target_id, "reason": "批次中不存在该组"})
                continue
            if group["status"] != DONE:
                skipped.append(
                    {
                        "config_id": target_id,
                        "reason": f"分组尚未算完（{group['status']}），补报只更正已算完的组",
                    }
                )
                continue
            applied.append(
                _apply_supplement(
                    batch,
                    group,
                    cohort_denominator=item.get("cohort_denominator"),
                    delta=item.get("delta"),
                    remark=str(item.get("remark", remark)),
                )
            )
    else:
        if not config_id:
            raise ValueError("补报需要 config_id 或 corrections")
        group = next((g for g in batch["groups"] if g["config_id"] == config_id), None)
        if group is None:
            raise ValueError(f"批次 {batch_id} 中不存在分组：{config_id}")
        applied.append(
            _apply_supplement(
                batch,
                group,
                cohort_denominator=cohort_denominator,
                delta=delta,
                remark=remark,
            )
        )

    _save_batch(batch)
    return {
        "batch": _public_batch(batch),
        "applied": applied,
        "skipped": skipped,
        "message": f"更正 {len(applied)} 组，跳过 {len(skipped)} 组。",
    }


def confirm_retention_batch(batch_id: str) -> dict[str, Any]:
    """确认批次；只有全部组算完且没有待重算时才能确认。"""
    batch = _require_batch(batch_id)
    unfinished = [group["config_id"] for group in batch["groups"] if group["status"] != DONE]
    if unfinished:
        raise ValueError("批次仍有未完成分组，不能确认：" + "、".join(unfinished))

    for group in batch["groups"]:
        group["confirmed"] = True
    batch["confirmed"] = True
    batch["status"] = BATCH_CONFIRMED
    batch["revision"] = int(batch.get("revision", 0)) + 1
    batch["confirmed_at"] = _now()
    _set_batch_queue_confirmed(batch_id, True)
    _save_batch(batch)
    return _public_batch(batch)


# ---------------------------------------------------------------------------
# 对外：查询与已确认曲线
# ---------------------------------------------------------------------------

def list_retention_batches() -> list[dict[str, Any]]:
    return [
        {
            "batch_id": batch["batch_id"],
            "status": batch["status"],
            "confirmed": bool(batch.get("confirmed")),
            "config_ids": list(batch.get("config_ids", [])),
            "group_count": batch.get("group_count", len(batch.get("groups", []))),
            "pending_count": batch.get("pending_count", 0),
            "reused_count": batch.get("reused_count", 0),
            "created_at": batch.get("created_at"),
            "updated_at": batch.get("updated_at"),
        }
        for batch in _batch_rows()
    ]


def get_retention_batch(batch_id: str) -> dict[str, Any] | None:
    batch = _find_batch(batch_id)
    return _public_batch(batch) if batch is not None else None


def list_retention_queue(
    batch_id: str | None = None,
    config_id: str | None = None,
) -> list[dict[str, Any]]:
    """计算队列明细：每组实际使用的入组分母、回访区间和结果版本。"""
    items = []
    for row in _queue_rows():
        if batch_id is not None and row.get("batch_id") != batch_id:
            continue
        if config_id is not None and row.get("config_id") != config_id:
            continue
        items.append(
            {
                "id": row.get("id"),
                "batch_id": row.get("batch_id"),
                "config_id": row.get("config_id"),
                "cohort_key": row.get("cohort_key"),
                "cohort_denominator": row.get("cohort_denominator"),
                "return_window": copy.deepcopy(row.get("return_window")),
                "result_version": row.get("result_version"),
                "valid": bool(row.get("valid")),
                "confirmed": bool(row.get("confirmed")),
                "reused": bool(row.get("reused")),
                "source_batch_id": row.get("source_batch_id"),
                "calibration_curve": list(row.get("calibration_curve", [])),
                "created_at": row.get("created_at"),
            }
        )
    return items


def _latest_batch_for_config(config_id: str) -> dict[str, Any] | None:
    candidates = [
        batch
        for batch in _batch_rows()
        if config_id in batch.get("config_ids", [])
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda batch: int(batch.get("id", 0)))


def get_confirmed_retention_curve(
    config_id: str,
    batch_id: str | None = None,
) -> dict[str, Any]:
    """从已确认的留存批次明细取得校准曲线。

    最新批次尚未确认时直接拒绝，避免把未确认的入组数据与旧曲线混用。
    """
    if batch_id is not None:
        batch = _require_batch(batch_id)
        if config_id not in batch.get("config_ids", []):
            raise ValueError(f"批次 {batch_id} 不包含配置 {config_id}")
    else:
        batch = _latest_batch_for_config(config_id)
        if batch is None:
            raise ValueError(f"未找到包含配置 {config_id} 的留存批次")

    if not batch.get("confirmed"):
        raise ValueError(
            f"留存批次 {batch['batch_id']} 尚未确认，未确认的入组数据不能用于预测校准"
        )

    group = next((g for g in batch["groups"] if g["config_id"] == config_id), None)
    if group is None:
        raise ValueError(f"批次 {batch['batch_id']} 中不存在分组 {config_id}")
    if group["status"] != DONE or not group.get("confirmed"):
        raise ValueError(f"分组 {config_id} 尚未确认完成，不能用于预测校准")

    base_date = str(group["return_window"]["start"])
    rates = list(group["calibration_curve"])
    counts = list(group["retained_counts"])
    curve = [
        {
            "offset_day": index,
            "date": _add_days(base_date, index),
            "retention_rate": rate,
            "retained_users": counts[index] if index < len(counts) else None,
        }
        for index, rate in enumerate(rates)
    ]
    average = round(sum(rates) / len(rates), 4) if rates else 0.0

    return {
        "batch_id": batch["batch_id"],
        "config_id": config_id,
        "result_version": group["result_version"],
        "cohort_denominator": group["cohort_denominator"],
        "return_window": copy.deepcopy(group["return_window"]),
        "calibration_curve": curve,
        "retention_rates": copy.deepcopy(group["retention_rates"]),
        "avg_retention_rate": average,
        "reused": bool(group.get("reused")),
        "reused_from": copy.deepcopy(group.get("reused_from")),
        "groups_used": [
            {
                "group_key": group["group_key"],
                "config_id": group["config_id"],
                "batch_id": batch["batch_id"],
                "result_version": group["result_version"],
                "cohort_denominator": group["cohort_denominator"],
                "return_window": copy.deepcopy(group["return_window"]),
                "reused": bool(group.get("reused")),
                "reused_from": copy.deepcopy(group.get("reused_from")),
            }
        ],
    }


def _stale_valuations(config_ids: set[str], batch: dict[str, Any]) -> list[dict[str, Any]]:
    """列出受本次更正影响、仍引用旧版本的 LTV 校准结果。"""
    if not config_ids:
        return []
    current_versions = {
        group["config_id"]: int(group["result_version"])
        for group in batch["groups"]
        if group["config_id"] in config_ids
    }
    stale: list[dict[str, Any]] = []
    seen: set[str] = set()
    for artifact in store.list_artifacts("lifetime_value_calibration"):
        model_id = str(artifact.get("model_id", ""))
        if model_id in seen:
            continue
        for used in artifact.get("groups_used", []):
            config_id = str(used.get("config_id", ""))
            if config_id not in current_versions:
                continue
            if int(used.get("result_version", 0)) < current_versions[config_id]:
                stale.append(
                    {
                        "model_id": model_id,
                        "retention_config_id": artifact.get("retention_config_id"),
                        "batch_id": artifact.get("batch_id"),
                        "used_result_version": used.get("result_version"),
                        "current_result_version": current_versions[config_id],
                        "reason": "估值引用了补报前的留存结果版本",
                    }
                )
                seen.add(model_id)
                break
    return stale


# ---------------------------------------------------------------------------
# 单配置直算（兼容原有 calculate_retention）
# ---------------------------------------------------------------------------

def run_single_config_batch(config_id: str) -> RetentionResult:
    """单配置直接计算：登记一个已确认的单组批次，供后续复用和 LTV 校准。"""
    config = _require_config(config_id)
    batch = register_retention_batch([config_id], remark="单配置直算")
    _execute(batch["batch_id"])
    confirmed = confirm_retention_batch(batch["batch_id"])
    group = confirmed["groups"][0]

    result = RetentionResult(
        config_id=config_id,
        init_event=str(config["init_event"]),
        return_event=str(config["return_event"]),
        base_date=str(config["base_date"]),
        cohort_sizes=copy.deepcopy(group["cohort_sizes"]),
        retention_rates=copy.deepcopy(group["retention_rates"]),
        result_table=str(config.get("result_table", "")),
    )
    store.set_artifact("retention_calc", config_id, result.model_dump())
    store.update(
        "retention_calc",
        config["id"],
        {"status": "已完成", "pending": False},
    )
    return result
