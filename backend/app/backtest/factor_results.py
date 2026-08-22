"""因子回测结果持久化 — data_dir/backtest_results/factor/<run_id>.json。

单因子与批量评估结果统一落盘 (kind=single/batch):
  - 原子写 (临时文件 + os.replace), 进程中断不留半截 JSON
  - 保留最近 MAX_KEEP 条, 超出自动删除最旧 (按文件 mtime)
  - list 只回轻量摘要, 完整数据按 run_id 单独加载 (含净值级数)
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path

MAX_KEEP = 50


def _dir(data_dir: Path) -> Path:
    return Path(data_dir) / "backtest_results" / "factor"


def _json_safe(obj):
    """nan/inf → None, 保证 JSON.parse 不崩 (同 api/backtest._json_safe 语义)。"""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def save_result(data_dir: Path, run_id: str, payload: dict) -> dict:
    """保存一次结果, 返回 {run_id, created_at, kind, path}。"""
    directory = _dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now().isoformat(timespec="seconds")
    record = {
        "run_id": run_id,
        "kind": payload.get("kind", "single"),
        "created_at": created_at,
        "data": _json_safe(payload.get("data", {})),
    }
    path = directory / f"{run_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(record, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    os.replace(tmp, path)

    # 裁剪: 保留最近 MAX_KEEP 条
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in files[MAX_KEEP:]:
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass

    return {"run_id": run_id, "created_at": created_at, "kind": record["kind"], "path": str(path)}


def _summarize(record: dict) -> dict:
    data = record.get("data", {}) or {}
    config = data.get("config", {}) or {}
    if record.get("kind") == "batch":
        return {
            "run_id": record.get("run_id"),
            "kind": "batch",
            "created_at": record.get("created_at"),
            "factor_count": len(data.get("factors", [])),
            "skipped": data.get("skipped", []),
            "config": config,
        }
    return {
        "run_id": record.get("run_id"),
        "kind": "single",
        "created_at": record.get("created_at"),
        "factor_name": config.get("factor_name"),
        "config": config,
        "ic_mean": data.get("ic_mean"),
        "ir": data.get("ir"),
        "ic_win_rate": data.get("ic_win_rate"),
        "ls_total_return": (data.get("long_short_stats") or {}).get("total_return"),
        "ls_annual_return": (data.get("long_short_stats") or {}).get("annual_return"),
        "ls_sharpe": (data.get("long_short_stats") or {}).get("sharpe"),
        "n_symbols": data.get("n_symbols"),
        "n_dates": data.get("n_dates"),
        "elapsed_ms": data.get("elapsed_ms"),
    }


def list_results(data_dir: Path, limit: int = 20) -> list[dict]:
    """最近 N 次结果摘要 (新到旧)。"""
    directory = _dir(data_dir)
    if not directory.exists():
        return []
    files = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for path in files[: max(1, limit)]:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(_summarize(record))
    return out


def load_result(data_dir: Path, run_id: str) -> dict | None:
    """加载完整结果记录 (含净值级数); 不存在返回 None。"""
    path = _dir(data_dir) / f"{run_id}.json"
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    record["data"]["run_id"] = run_id
    return record