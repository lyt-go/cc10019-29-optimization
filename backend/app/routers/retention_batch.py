"""留存批次路由：批量登记/提交、续做、重试、补报、确认与明细查询。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import ActionResult
from app.services.retention_batch import (
    confirm_retention_batch,
    get_retention_batch,
    list_retention_batches,
    list_retention_queue,
    register_retention_batch,
    report_cohort_supplement,
    resume_retention_batch,
    retry_retention_batch,
    submit_retention_batch,
)

router = APIRouter(prefix="/api/retention-batch", tags=["retention_batch"])


def _bad_request(error: ValueError) -> HTTPException:
    message = str(error)
    if "not found" in message or "不存在" in message:
        return HTTPException(status_code=404, detail=message)
    return HTTPException(status_code=400, detail=message)


@router.get("")
def index() -> dict[str, Any]:
    items = list_retention_batches()
    return {"items": items, "total": len(items)}


@router.post("")
def create(payload: dict[str, Any]) -> ActionResult:
    """只登记批次明细，不计算；登记后按需续做。"""
    config_ids = payload.get("config_ids") or []
    try:
        batch = register_retention_batch(
            [str(item) for item in config_ids],
            remark=str(payload.get("remark", "")),
        )
    except ValueError as e:
        raise _bad_request(e)
    return ActionResult(ok=True, message=f"已登记留存批次 {batch['batch_id']}", entry=batch)


@router.post("/submit")
def submit(payload: dict[str, Any]) -> ActionResult:
    """批量提交并计算；``compute_limit`` 可只算前几组，其余留作续做。"""
    config_ids = payload.get("config_ids") or []
    limit = payload.get("compute_limit")
    try:
        batch = submit_retention_batch(
            [str(item) for item in config_ids],
            compute_limit=int(limit) if limit is not None else None,
            remark=str(payload.get("remark", "")),
        )
    except ValueError as e:
        raise _bad_request(e)
    return ActionResult(
        ok=True,
        message=(
            f"已提交批次 {batch['batch_id']}：完成 {batch['group_count'] - batch['pending_count']} 组，"
            f"待算 {batch['pending_count']} 组"
        ),
        entry=batch,
    )


@router.get("/queue")
def queue(batch_id: str | None = None, config_id: str | None = None) -> dict[str, Any]:
    """计算队列：逐条查看各组已用的入组分母、回访区间与结果版本。"""
    items = list_retention_queue(batch_id=batch_id, config_id=config_id)
    return {"items": items, "total": len(items)}


@router.get("/{batch_id}")
def detail(batch_id: str) -> dict[str, Any]:
    batch = get_retention_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Not found")
    return batch


@router.post("/{batch_id}/resume")
def resume(batch_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    """续做：只补算待算组，已算组保留不重算。"""
    limit = (payload or {}).get("limit")
    try:
        batch = resume_retention_batch(batch_id, limit=int(limit) if limit is not None else None)
    except ValueError as e:
        raise _bad_request(e)
    return ActionResult(
        ok=True,
        message=f"续做完成：待算 {batch['pending_count']} 组",
        entry=batch,
    )


@router.post("/{batch_id}/retry")
def retry(batch_id: str, payload: dict[str, Any] | None = None) -> ActionResult:
    """批量重试：返回 retry_summary，说明哪些计算受补报更正影响。"""
    limit = (payload or {}).get("limit")
    try:
        batch = retry_retention_batch(batch_id, limit=int(limit) if limit is not None else None)
    except ValueError as e:
        raise _bad_request(e)
    summary = batch.get("retry_summary") or {}
    return ActionResult(
        ok=True,
        message=str(summary.get("message", "重试完成")),
        entry=batch,
    )


@router.post("/{batch_id}/supplement")
def supplement(batch_id: str, payload: dict[str, Any]) -> ActionResult:
    """补报入组人数：只更正已算完的组，未算完的组放进 skipped。"""
    try:
        outcome = report_cohort_supplement(
            batch_id,
            config_id=payload.get("config_id"),
            cohort_denominator=payload.get("cohort_denominator"),
            delta=payload.get("delta"),
            remark=str(payload.get("remark", "")),
            corrections=payload.get("corrections"),
        )
    except ValueError as e:
        raise _bad_request(e)
    return ActionResult(ok=True, message=outcome["message"], entry=outcome)


@router.post("/{batch_id}/confirm")
def confirm(batch_id: str) -> ActionResult:
    try:
        batch = confirm_retention_batch(batch_id)
    except ValueError as e:
        raise _bad_request(e)
    return ActionResult(ok=True, message=f"批次 {batch_id} 已确认", entry=batch)
