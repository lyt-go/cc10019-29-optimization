"""留存计算引擎：计算用户留存率，支持按维度分段。"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.schemas import RetentionResult


def _generate_retention_events(event_id: str, base_date: str, days: int) -> list[dict[str, Any]]:
    """生成留存分析所需的模拟事件数据。"""
    events = []
    import datetime
    base = datetime.datetime.strptime(base_date, "%Y-%m-%d")
    base_ts = int(base.timestamp())

    for day in range(days):
        # 每天的用户数递减模拟留存
        num_users = max(1000 - day * 80, 200)
        for user_idx in range(1, num_users + 1):
            user_id = f"U{user_idx:05d}"
            # 用户在不同维度上的分布
            attr_001 = ["app", "web", "miniapp", "h5"][user_idx % 4]
            attr_005 = ["普通", "银卡", "金卡", "钻石"][user_idx % 4]
            events.append({
                "user_id": user_id,
                "event_id": event_id,
                "timestamp": base_ts + day * 86400 + (user_idx % 3600),
                "ATTR-001": attr_001,
                "ATTR-005": attr_005,
                "date": (base + datetime.timedelta(days=day)).strftime("%Y-%m-%d"),
            })
    return events


def calculate_retention(config_id: str) -> RetentionResult:
    """执行留存计算：走留存批次明细，结果登记后可直接被 LTV 校准复用。"""
    # 延迟导入：retention_batch 依赖本模块的事件生成与日期工具。
    from app.services.retention_batch import run_single_config_batch

    return run_single_config_batch(config_id)


def _add_days(date_str: str, days: int) -> str:
    """日期加减。"""
    import datetime
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    return (d + datetime.timedelta(days=days)).strftime("%Y-%m-%d")


def get_retention_result(config_id: str) -> dict[str, Any] | None:
    """获取留存计算结果。"""
    artifact = store.get_artifact("retention_calc", config_id)
    if artifact:
        return artifact
    try:
        result = calculate_retention(config_id)
        return result.model_dump()
    except ValueError:
        return None
