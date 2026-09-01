from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path


RESULT_FIELDNAMES = [
    "id",
    "url",
    "domain",
    "title",
    "snippet",
    "relevanceScore",
    "timestamp",
    "feedback",
    "notes",
    "feedbackSubmitted",
    "feedbackSubmittedAt",
    "status",
    "pred",
    "reason",
    "query",
]

FEEDBACK_FIELDNAMES = [
    "timestamp",
    "resultId",
    "feedback",
    "notes",
]

QUERY_LOG_FIELDNAMES = [
    "role",
    "content",
]

COMPONENT_METRICS_FIELDNAMES = [
    "timestamp",
    "component",
    "operation",
    "provider",
    "model",
    "latencyMs",
    "promptTokens",
    "completionTokens",
    "totalTokens",
    "estimatedCostUsd",
    "status",
    "error",
    "meta",
]

FETCH_METRICS_FIELDNAMES = [
    "timestamp",
    "url",
    "domain",
    "fetchMs",
    "statusCode",
    "bytesRead",
    "contentType",
    "outcome",
    "retryCount",
    "cooldownApplied",
]


class SessionArtifacts:
    def __init__(self, artifact_dir: Path):
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

        self.results_csv_path = self.artifact_dir / "results.csv"
        self.feedback_csv_path = self.artifact_dir / "feedback.csv"
        self.query_log_csv_path = self.artifact_dir / "query_log.csv"
        self.component_metrics_csv_path = self.artifact_dir / "component_metrics.csv"
        self.fetch_metrics_csv_path = self.artifact_dir / "fetch_metrics.csv"
        self.checkpoint_json_path = self.artifact_dir / "checkpoint.json"
        self.meta_json_path = self.artifact_dir / "meta.json"

    def write_meta(self, payload: dict) -> None:
        with self.meta_json_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    def append_result(self, result_payload: dict) -> None:
        self._append_csv_row(
            self.results_csv_path,
            RESULT_FIELDNAMES,
            result_payload,
        )

    def append_feedback(self, result_id: str, feedback: str | None, notes: str | None) -> None:
        row = {
            "timestamp": datetime.utcnow().isoformat(),
            "resultId": result_id,
            "feedback": feedback,
            "notes": notes or "",
        }
        self._append_csv_row(self.feedback_csv_path, FEEDBACK_FIELDNAMES, row)

    def update_result_feedback(self, result_payload: dict) -> None:
        if not self.results_csv_path.exists():
            return

        with self.results_csv_path.open("r", newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            rows = list(reader)
            fieldnames = list(reader.fieldnames or RESULT_FIELDNAMES)

        target_id = str(result_payload.get("id", ""))
        if not target_id:
            return

        updated = False
        for row in rows:
            if row.get("id") == target_id:
                for key in RESULT_FIELDNAMES:
                    if key in result_payload:
                        row[key] = result_payload.get(key)
                updated = True
                break

        if not updated:
            return

        with self.results_csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})

    def write_query_log(self, query_conversation: list[dict]) -> None:
        with self.query_log_csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=QUERY_LOG_FIELDNAMES)
            writer.writeheader()
            for item in query_conversation:
                writer.writerow(
                    {
                        "role": item.get("role", ""),
                        "content": item.get("content", ""),
                    }
                )

    def append_component_metric(self, metric_row: dict) -> None:
        self._append_csv_row(
            self.component_metrics_csv_path,
            COMPONENT_METRICS_FIELDNAMES,
            metric_row,
        )

    def write_component_metrics(self, metric_rows: list[dict]) -> None:
        with self.component_metrics_csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=COMPONENT_METRICS_FIELDNAMES)
            writer.writeheader()
            for row in metric_rows:
                writer.writerow({key: row.get(key) for key in COMPONENT_METRICS_FIELDNAMES})

    def append_fetch_metric(self, metric_row: dict) -> None:
        self._append_csv_row(
            self.fetch_metrics_csv_path,
            FETCH_METRICS_FIELDNAMES,
            metric_row,
        )

    def write_fetch_metrics(self, metric_rows: list[dict]) -> None:
        with self.fetch_metrics_csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FETCH_METRICS_FIELDNAMES)
            writer.writeheader()
            for row in metric_rows:
                writer.writerow({key: row.get(key) for key in FETCH_METRICS_FIELDNAMES})

    def write_checkpoint(
        self,
        *,
        visited: set[str],
        query_gen_conv: list[dict],
        all_queries: list[str],
        stats: dict,
        start_time: datetime,
        end_time: datetime | None,
    ) -> None:
        payload = {
            "visited": sorted(list(visited)),
            "query_gen_conv": query_gen_conv,
            "all_queries": all_queries,
            "stats": stats,
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat() if end_time else None,
            "updated_at": datetime.utcnow().isoformat(),
        }

        with self.checkpoint_json_path.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)

    @staticmethod
    def _append_csv_row(path: Path, fieldnames: list[str], row: dict) -> None:
        write_header = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerow({key: row.get(key) for key in fieldnames})
