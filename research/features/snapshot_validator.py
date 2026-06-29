from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from research.features.feature_provider import DECISION_SCHEMA_VERSION, SCHEMA_VERSION
from research.features.evidence_contract import EVIDENCE_CONTRACT_VERSION, validate_evidence_contract
from research.features.readiness import DECISION_REQUIRED_FIELDS, TRADE_REQUIRED_FIELDS


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_jsonl(path: Path) -> tuple[list[dict], list[dict]]:
    items = []
    issues = []
    if not path.exists():
        return items, [{"file": str(path), "issue": "missing_file"}]
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                issues.append(
                    {
                        "file": str(path),
                        "line": line_no,
                        "issue": "invalid_json",
                        "detail": str(exc),
                    }
                )
                continue
            if not isinstance(item, dict):
                issues.append({"file": str(path), "line": line_no, "issue": "non_object_json"})
                continue
            items.append(item)
    return items, issues


def _schema_issues(items: list[dict], *, schema: str, required: list[str], kind: str) -> list[dict]:
    issues = []
    for idx, item in enumerate(items, start=1):
        sample_id = str(item.get("sample_id") or f"{kind}:{idx}")
        if item.get("schema_version") != schema:
            issues.append(
                {
                    "kind": kind,
                    "sample_id": sample_id,
                    "field": "schema_version",
                    "issue": "schema_mismatch",
                    "expected": schema,
                    "actual": item.get("schema_version"),
                }
            )
        for field in required:
            if field not in item:
                issues.append(
                    {
                        "kind": kind,
                        "sample_id": sample_id,
                        "field": field,
                        "issue": "missing_field",
                    }
                )
        issues.extend(validate_evidence_contract(item, kind=kind))
    return issues


class LearningDatasetValidator:
    """Validate an exported learning dataset snapshot without reading runtime DB state."""

    def validate(self, dataset_ref: str | Path) -> dict[str, Any]:
        root = Path(dataset_ref)
        manifest_path = root / "manifest.json"
        issues: list[dict] = []
        if not manifest_path.exists():
            return {
                "valid": False,
                "dataset_ref": str(root),
                "issues": [{"file": str(manifest_path), "issue": "missing_manifest"}],
            }

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {
                "valid": False,
                "dataset_ref": str(root),
                "issues": [{"file": str(manifest_path), "issue": "invalid_manifest_json", "detail": str(exc)}],
            }

        files = manifest.get("files") or {}
        loaded: dict[str, list[dict]] = {}
        for key, expected_schema, required in (
            ("trade_samples", SCHEMA_VERSION, TRADE_REQUIRED_FIELDS),
            ("decision_samples", DECISION_SCHEMA_VERSION, DECISION_REQUIRED_FIELDS),
        ):
            meta = files.get(key) or {}
            file_path = Path(str(meta.get("path") or root / f"{key}.jsonl"))
            if not file_path.is_absolute() and not file_path.exists():
                file_path = root / file_path
            if not file_path.exists():
                issues.append({"file": str(file_path), "issue": "missing_file", "key": key})
                loaded[key] = []
                continue
            actual_sha = _file_sha256(file_path)
            if meta.get("sha256") and actual_sha != meta.get("sha256"):
                issues.append(
                    {
                        "file": str(file_path),
                        "key": key,
                        "issue": "sha256_mismatch",
                        "expected": meta.get("sha256"),
                        "actual": actual_sha,
                    }
                )
            items, parse_issues = _load_jsonl(file_path)
            issues.extend(parse_issues)
            if int(meta.get("count") or 0) != len(items):
                issues.append(
                    {
                        "file": str(file_path),
                        "key": key,
                        "issue": "count_mismatch",
                        "expected": int(meta.get("count") or 0),
                        "actual": len(items),
                    }
                )
            kind = "trade" if key == "trade_samples" else "decision"
            issues.extend(_schema_issues(items, schema=expected_schema, required=required, kind=kind))
            loaded[key] = items

        return {
            "valid": not issues,
            "dataset_id": str(manifest.get("dataset_id") or root.name),
            "dataset_ref": str(root),
            "manifest_path": str(manifest_path),
            "readiness": manifest.get("readiness") or {},
            "files": {
                "trade_samples": {"count": len(loaded.get("trade_samples") or [])},
                "decision_samples": {"count": len(loaded.get("decision_samples") or [])},
            },
            "issue_count": len(issues),
            "issues": issues[:100],
        }
