"""Reconcile Input/Review Sheets from local checkpoints, dry-run by default.

Only --execute-writes permits Sheets mutations. No media, OpenAI, Docs, Drive,
or checkpoint writes are performed. OAuth may refresh its local token file.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.errors import ArtifactPersistenceError, PipelineOperationalError
from instagram_recipe_transcriber.google_adapters import (
    GoogleSheetsReviewSyncApplier,
    GoogleSheetsReviewSyncSnapshotReader,
)
from instagram_recipe_transcriber.google_oauth import GoogleOAuthServiceFactory
from instagram_recipe_transcriber.publication import JsonReviewResolutionStore, JsonReviewStore
from instagram_recipe_transcriber.review_sync import ReviewSyncPlan, ReviewSyncPlanner

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def synchronize(config: GoogleWorkflowConfig, sheets: Any, *, execute_writes: bool) -> int:
    if not config.review_spreadsheet_id:
        raise ValueError("A Review Sheet must be configured")
    queue, active = GoogleSheetsReviewSyncSnapshotReader(
        sheets,
        queue_spreadsheet_id=config.queue_spreadsheet_id,
        review_spreadsheet_id=config.review_spreadsheet_id,
        categories=config.category_tabs,
    ).read()
    recipe_ids = sorted({row.recipe_id for row in (*queue, *active)})
    review_store = JsonReviewStore(config.working_root / "review-publication")
    resolution_store = JsonReviewResolutionStore(config.working_root / "review-resolution")
    reviews = {}
    resolutions = {}
    for recipe_id in recipe_ids:
        review = review_store.load(recipe_id)
        resolution = resolution_store.load(recipe_id)
        if review is not None:
            reviews[recipe_id] = review
        if resolution is not None:
            resolutions[recipe_id] = resolution
    plan = ReviewSyncPlanner(reviews=reviews, resolutions=resolutions).plan(queue, active)
    print(_counts("Planned", plan) + f" conflict={len(plan.conflicts)}")
    affected = sorted(
        {action.queue_row.recipe_id for action in plan.appends}
        | {action.active_row.recipe_id for action in plan.removals}
        | {action.queue_row.recipe_id for action in plan.queue_updates}
        | {recipe_id for conflict in plan.conflicts for recipe_id in conflict.recipe_ids}
    )
    print(f"Affected recipe IDs: {', '.join(affected) if affected else '(none)'}")
    for conflict in plan.conflicts:
        print(f"Conflict: {conflict.reason.value} | {', '.join(conflict.recipe_ids)}")
    if not execute_writes:
        print("Dry-run: no Sheets mutations; pass --execute-writes to apply this plan.")
        return 0
    if plan.conflicts:
        print("Refusing writes: resolve every conflict before executing.", file=sys.stderr)
        return 1
    GoogleSheetsReviewSyncApplier(
        sheets,
        queue_spreadsheet_id=config.queue_spreadsheet_id,
        review_spreadsheet_id=config.review_spreadsheet_id,
    ).apply(plan)
    print(_counts("Completed", plan))
    return 0


def _counts(label: str, plan: ReviewSyncPlan) -> str:
    return (
        f"{label}: append={len(plan.appends)} remove={len(plan.removals)} "
        f"update={len(plan.queue_updates)}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=REPOSITORY_ROOT / "google_workflow_config.json"
    )
    parser.add_argument(
        "--execute-writes",
        action="store_true",
        help="Apply the conflict-free plan to Google Sheets",
    )
    args = parser.parse_args(argv)
    try:
        config = GoogleWorkflowConfig.model_validate_json(
            args.config.expanduser().resolve().read_text(encoding="utf-8")
        )
        if not config.review_spreadsheet_id:
            raise ValueError("A Review Sheet must be configured")
        services = GoogleOAuthServiceFactory(config).create_services()
        return synchronize(config, services.sheets, execute_writes=args.execute_writes)
    except (
        OSError,
        ValueError,
        ValidationError,
        ArtifactPersistenceError,
        PipelineOperationalError,
    ) as error:
        print(f"Review synchronization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
