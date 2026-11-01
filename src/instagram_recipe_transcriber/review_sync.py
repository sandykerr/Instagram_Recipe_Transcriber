"""Pure, deterministic reconciliation of live review rows and local checkpoints.

Callers load checkpoints and row snapshots; this module neither reads stores nor
performs mutations. Live rows determine missing queue work only after a
resolution's decision-specific destination delivery has been checkpointed.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .models import (
    QueueStatus,
    ReviewArtifact,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
)


class _ImmutableModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReviewQueueRow(_ImmutableModel):
    """One Input Sheet row, including its current status and detail cell."""

    recipe_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    source_url: HttpUrl
    category: str = Field(min_length=1)
    queue_row_number: int = Field(ge=2)
    status: QueueStatus = QueueStatus.REVIEW
    description: str | None = None
    detail: str | None = None


class ActiveReviewRow(_ImmutableModel):
    """One live Review Sheet row; its location is retained for safe application."""

    recipe_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
    source_url: HttpUrl
    category: str = Field(min_length=1)
    review_row_number: int = Field(ge=2)
    review_document_url: HttpUrl
    description: str | None = None


class ReviewAppendAction(_ImmutableModel):
    """Restore an unresolved review using its existing document checkpoint."""

    queue_row: ReviewQueueRow
    review: ReviewArtifact


class ReviewRemovalAction(_ImmutableModel):
    """Remove a resolved active row, validating its live identity before deletion."""

    active_row: ActiveReviewRow
    resolution: ReviewResolutionArtifact


class ReviewQueueUpdateAction(_ImmutableModel):
    """Set the Input Sheet status and detail to the checkpoint's resolved result."""

    queue_row: ReviewQueueRow
    status: QueueStatus
    detail: str


class ReviewSyncConflictReason(StrEnum):
    CATEGORY_MISMATCH = "category_mismatch"
    CHECKPOINT_ID_MISMATCH = "checkpoint_id_mismatch"
    DUPLICATE_RECIPE_ID = "duplicate_recipe_id"
    DUPLICATE_SOURCE_URL = "duplicate_source_url"
    IDENTITY_MISMATCH = "identity_mismatch"
    INCOMPLETE_RESOLUTION = "incomplete_resolution"
    MISSING_CHECKPOINT = "missing_checkpoint"
    MISSING_QUEUE_ROW = "missing_queue_row"
    QUEUE_STATE_MISMATCH = "queue_state_mismatch"


class ReviewSyncConflict(_ImmutableModel):
    """An ambiguity blocking every mutation in an identity-connected group."""

    reason: ReviewSyncConflictReason
    recipe_ids: tuple[str, ...]
    source_urls: tuple[HttpUrl, ...]


class ReviewSyncPlan(_ImmutableModel):
    appends: tuple[ReviewAppendAction, ...] = ()
    removals: tuple[ReviewRemovalAction, ...] = ()
    queue_updates: tuple[ReviewQueueUpdateAction, ...] = ()
    conflicts: tuple[ReviewSyncConflict, ...] = ()


class ReviewSyncPlanner:
    """Plan from immutable snapshots without performing provider or store I/O.

    Rows sharing either a recipe ID or source URL form one identity component.
    Ambiguity anywhere in that component suppresses all of its actions, including
    otherwise valid updates/removals reached through a chain of collisions.
    Non-review work-in-progress queue states are never changed.
    """

    def __init__(
        self,
        *,
        reviews: Mapping[str, ReviewArtifact],
        resolutions: Mapping[str, ReviewResolutionArtifact],
    ) -> None:
        self._reviews = dict(reviews)
        self._resolutions = dict(resolutions)

    def plan(
        self,
        queue_rows: tuple[ReviewQueueRow, ...],
        active_rows: tuple[ActiveReviewRow, ...],
    ) -> ReviewSyncPlan:
        appends: list[ReviewAppendAction] = []
        removals: list[ReviewRemovalAction] = []
        queue_updates: list[ReviewQueueUpdateAction] = []
        conflicts: list[ReviewSyncConflict] = []

        for rows in _identity_groups(queue_rows, active_rows):
            queues = tuple(row for row in rows if isinstance(row, ReviewQueueRow))
            active = tuple(row for row in rows if isinstance(row, ActiveReviewRow))
            recipe_ids = tuple(sorted({row.recipe_id for row in rows}))
            source_urls = tuple(sorted({row.source_url for row in rows}, key=str))
            reasons = self._conflict_reasons(queues, active, recipe_ids, source_urls)
            if reasons:
                conflicts.extend(
                    ReviewSyncConflict(
                        reason=reason, recipe_ids=recipe_ids, source_urls=source_urls
                    )
                    for reason in sorted(reasons)
                )
                continue

            # The conflict gate guarantees exactly one unambiguous Input row.
            queue = queues[0]
            recipe_id = queue.recipe_id
            resolution = self._resolutions.get(recipe_id)
            if resolution is not None:
                if active:
                    removals.append(
                        ReviewRemovalAction(active_row=active[0], resolution=resolution)
                    )
                status = (
                    QueueStatus.PUBLISHED
                    if resolution.decision is ReviewDecisionStatus.ACCEPTED
                    else QueueStatus.REJECTED
                )
                detail = str(resolution.document.url)
                if queue.status is QueueStatus.REVIEW:
                    queue_updates.append(
                        ReviewQueueUpdateAction(queue_row=queue, status=status, detail=detail)
                    )
            elif queue.status is QueueStatus.REVIEW and not active:
                appends.append(ReviewAppendAction(queue_row=queue, review=self._reviews[recipe_id]))

        return ReviewSyncPlan(
            appends=tuple(appends),
            removals=tuple(removals),
            queue_updates=tuple(queue_updates),
            conflicts=tuple(conflicts),
        )

    def _conflict_reasons(
        self,
        queues: tuple[ReviewQueueRow, ...],
        active: tuple[ActiveReviewRow, ...],
        recipe_ids: tuple[str, ...],
        source_urls: tuple[HttpUrl, ...],
    ) -> set[ReviewSyncConflictReason]:
        reasons: set[ReviewSyncConflictReason] = set()
        for rows in (queues, active):
            if any(count > 1 for count in Counter(row.source_url for row in rows).values()):
                reasons.add(ReviewSyncConflictReason.DUPLICATE_SOURCE_URL)
            if any(count > 1 for count in Counter(row.recipe_id for row in rows).values()):
                reasons.add(ReviewSyncConflictReason.DUPLICATE_RECIPE_ID)
        if len(recipe_ids) > 1 or len(source_urls) > 1:
            reasons.add(ReviewSyncConflictReason.IDENTITY_MISMATCH)
        categories = {row.category for row in queues} | {row.category for row in active}
        if len(categories) > 1:
            reasons.add(ReviewSyncConflictReason.CATEGORY_MISMATCH)
        if not queues:
            reasons.add(ReviewSyncConflictReason.MISSING_QUEUE_ROW)

        for recipe_id in recipe_ids:
            review = self._reviews.get(recipe_id)
            resolution = self._resolutions.get(recipe_id)
            for checkpoint in (review, resolution):
                if checkpoint is not None and checkpoint.recipe_id != recipe_id:
                    reasons.add(ReviewSyncConflictReason.CHECKPOINT_ID_MISMATCH)
            needs_checkpoint = bool(active) or any(
                row.status is QueueStatus.REVIEW for row in queues
            )
            if needs_checkpoint and review is None and resolution is None:
                reasons.add(ReviewSyncConflictReason.MISSING_CHECKPOINT)
            if resolution is not None:
                accepted = resolution.decision is ReviewDecisionStatus.ACCEPTED
                delivered = (
                    resolution.master_row_written if accepted else resolution.rejected_row_written
                )
                if not delivered:
                    reasons.add(ReviewSyncConflictReason.INCOMPLETE_RESOLUTION)
                status = QueueStatus.PUBLISHED if accepted else QueueStatus.REJECTED
                if any(
                    row.status in {QueueStatus.PUBLISHED, QueueStatus.REJECTED}
                    and (row.status is not status or row.detail != str(resolution.document.url))
                    for row in queues
                ):
                    reasons.add(ReviewSyncConflictReason.QUEUE_STATE_MISMATCH)
        return reasons


def _identity_groups(
    queue_rows: tuple[ReviewQueueRow, ...], active_rows: tuple[ActiveReviewRow, ...]
) -> tuple[tuple[ReviewQueueRow | ActiveReviewRow, ...], ...]:
    """Group transitively by either identity, ordered by smallest recipe ID."""

    rows_by_id: dict[str, list[ReviewQueueRow | ActiveReviewRow]] = defaultdict(list)
    ids_by_url: dict[HttpUrl, set[str]] = defaultdict(set)
    all_rows: tuple[ReviewQueueRow | ActiveReviewRow, ...] = (*queue_rows, *active_rows)
    for row in all_rows:
        rows_by_id[row.recipe_id].append(row)
        ids_by_url[row.source_url].add(row.recipe_id)

    visited: set[str] = set()
    groups: list[tuple[ReviewQueueRow | ActiveReviewRow, ...]] = []
    for recipe_id in sorted(rows_by_id):
        if recipe_id in visited:
            continue
        pending = [recipe_id]
        group: list[ReviewQueueRow | ActiveReviewRow] = []
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            for row in rows_by_id[current]:
                group.append(row)
                pending.extend(ids_by_url[row.source_url] - visited)
        groups.append(tuple(group))
    return tuple(groups)
