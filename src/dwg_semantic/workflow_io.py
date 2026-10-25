"""Shared workflow IO and immutable source freshness; no modeling entry points."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class WorkflowError(ValueError):
    pass


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def digest_file(path: Path | str) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _source_freshness(document: dict) -> list[str]:
    intake = document.get("intake", {})
    issues = []
    entries = [{"path": intake.get("source_path"), "sha256": intake.get("source_sha256"), "status": "loaded"}, *intake.get("dependencies", [])]
    for entry in entries:
        path, expected = entry.get("path"), entry.get("sha256")
        if not path or not expected:
            if entry.get("status") not in {"overlay_skipped", "not_used"}:
                issues.append(f"來源或參照缺少可驗證的路徑／雜湊：{path}")
            continue
        candidate = Path(path)
        if not candidate.is_file():
            issues.append(f"來源或參照已不存在：{path}")
        elif digest_file(candidate) != expected:
            issues.append(f"來源或參照已改變，請重新解析：{path}")
    return issues

