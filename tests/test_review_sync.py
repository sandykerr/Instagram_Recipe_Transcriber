from __future__ import annotations

from itertools import permutations

import pytest
from pydantic import HttpUrl, ValidationError

from instagram_recipe_transcriber.models import (
    QueueStatus,
    RecipeDocument,
    ReviewArtifact,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
)
from instagram_recipe_transcriber.review_sync import (
    ActiveReviewRow,
    ReviewQueueRow,
    ReviewSyncConflictReason,
    ReviewSyncPlan,
    ReviewSyncPlanner,
)


def _queue(recipe_id: str = "pasta", **updates: object) -> ReviewQueueRow:
    values: dict[str, object] = {
        "recipe_id": recipe_id,
        "source_url": f"https://www.instagram.com/reel/{recipe_id}/",
        "category": "Main Courses",
        "queue_row_number": 2,
        "status": QueueStatus.REVIEW,
        "description": "Pasta for dinner",
    }
    values.update(updates)
    return ReviewQueueRow.model_validate(values)


def _active(recipe_id: str = "pasta", **updates: object) -> ActiveReviewRow:
    values: dict[str, object] = {
        "recipe_id": recipe_id,
        "source_url": f"https://www.instagram.com/reel/{recipe_id}/",
        "category": "Main Courses",
        "review_row_number": 2,
        "review_document_url": f"https://docs.google.com/document/d/{recipe_id}/edit",
        "description": "Pasta for dinner",
    }
    values.update(updates)
    return ActiveReviewRow.model_validate(values)


def _document(identifier: str) -> RecipeDocument:
    return RecipeDocument(
        document_id=identifier,
        title="Pasta",
        url=HttpUrl(f"https://docs.google.com/document/d/{identifier}/edit"),
    )


def _review(recipe_id: str = "pasta", *, written: bool = False) -> ReviewArtifact:
    return ReviewArtifact(
        recipe_id=recipe_id,
        document=_document(recipe_id),
        review_row_written=written,
    )


def _resolution(
    decision: ReviewDecisionStatus = ReviewDecisionStatus.ACCEPTED,
    recipe_id: str = "pasta",
) -> ReviewResolutionArtifact:
    return ReviewResolutionArtifact(
        recipe_id=recipe_id,
        decision=decision,
        document=_document(f"final-{recipe_id}"),
        master_row_written=decision is ReviewDecisionStatus.ACCEPTED,
        rejected_row_written=decision is ReviewDecisionStatus.REJECTED,
    )


def _assert_no_mutations(plan: ReviewSyncPlan) -> None:
    assert plan.appends == ()
    assert plan.removals == ()
    assert plan.queue_updates == ()


def test_unresolved_review_already_present_is_a_noop() -> None:
    plan = ReviewSyncPlanner(reviews={"pasta": _review()}, resolutions={}).plan(
        (_queue(),), (_active(),)
    )

    assert plan == ReviewSyncPlan()


@pytest.mark.parametrize("written", [False, True])
def test_unresolved_review_missing_live_row_appends_even_if_checkpoint_says_written(
    written: bool,
) -> None:
    queue = _queue()
    review = _review(written=written)
    plan = ReviewSyncPlanner(reviews={"pasta": review}, resolutions={}).plan((queue,), ())

    assert len(plan.appends) == 1
    assert plan.appends[0].queue_row == queue
    assert plan.appends[0].review == review
    assert plan.removals == plan.queue_updates == plan.conflicts == ()


@pytest.mark.parametrize(
    ("decision", "status"),
    [
        (ReviewDecisionStatus.ACCEPTED, QueueStatus.PUBLISHED),
        (ReviewDecisionStatus.REJECTED, QueueStatus.REJECTED),
    ],
)
def test_resolution_updates_queue_to_authoritative_status_and_resulting_document(
    decision: ReviewDecisionStatus, status: QueueStatus
) -> None:
    queue = _queue()
    resolution = _resolution(decision)
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": resolution}).plan((queue,), ())

    assert len(plan.queue_updates) == 1
    assert plan.queue_updates[0].queue_row == queue
    assert plan.queue_updates[0].status is status
    assert plan.queue_updates[0].detail == "https://docs.google.com/document/d/final-pasta/edit"
    assert plan.appends == plan.removals == plan.conflicts == ()


@pytest.mark.parametrize("removed", [False, True])
def test_resolved_active_row_is_removed_regardless_of_checkpoint_flag(removed: bool) -> None:
    active = _active()
    resolution = _resolution().model_copy(update={"review_row_removed": removed})
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": resolution}).plan(
        (_queue(),), (active,)
    )

    assert len(plan.removals) == 1
    assert plan.removals[0].active_row == active
    assert plan.removals[0].resolution == resolution
    assert len(plan.queue_updates) == 1
    assert plan.appends == plan.conflicts == ()


@pytest.mark.parametrize(
    ("decision", "status"),
    [
        (ReviewDecisionStatus.ACCEPTED, QueueStatus.PUBLISHED),
        (ReviewDecisionStatus.REJECTED, QueueStatus.REJECTED),
    ],
)
def test_already_synchronized_resolution_is_a_noop(
    decision: ReviewDecisionStatus, status: QueueStatus
) -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution(decision)}).plan(
        (_queue(status=status, detail="https://docs.google.com/document/d/final-pasta/edit"),),
        (),
    )

    assert plan == ReviewSyncPlan()


@pytest.mark.parametrize("decision", list(ReviewDecisionStatus))
@pytest.mark.parametrize("active_rows", [(), (_active(),)])
def test_incomplete_destination_delivery_blocks_resolution_actions(
    decision: ReviewDecisionStatus, active_rows: tuple[ActiveReviewRow, ...]
) -> None:
    resolution = _resolution(decision).model_copy(
        update={
            "master_row_written": decision is ReviewDecisionStatus.REJECTED,
            "rejected_row_written": decision is ReviewDecisionStatus.ACCEPTED,
        }
    )
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": resolution}).plan(
        (_queue(),), active_rows
    )

    _assert_no_mutations(plan)
    assert [conflict.reason.value for conflict in plan.conflicts] == ["incomplete_resolution"]


@pytest.mark.parametrize("decision", list(ReviewDecisionStatus))
@pytest.mark.parametrize("status", [QueueStatus.PUBLISHED, QueueStatus.REJECTED])
@pytest.mark.parametrize("detail", [None, "old document"])
def test_conflicting_terminal_queue_state_blocks_all_resolution_actions(
    decision: ReviewDecisionStatus, status: QueueStatus, detail: str | None
) -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution(decision)}).plan(
        (_queue(status=status, detail=detail),), (_active(),)
    )

    _assert_no_mutations(plan)
    assert [conflict.reason.value for conflict in plan.conflicts] == ["queue_state_mismatch"]


@pytest.mark.parametrize("decision", list(ReviewDecisionStatus))
def test_opposite_terminal_status_blocks_even_with_the_correct_detail(
    decision: ReviewDecisionStatus,
) -> None:
    status = (
        QueueStatus.REJECTED if decision is ReviewDecisionStatus.ACCEPTED else QueueStatus.PUBLISHED
    )
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution(decision)}).plan(
        (_queue(status=status, detail="https://docs.google.com/document/d/final-pasta/edit"),),
        (_active(),),
    )

    _assert_no_mutations(plan)
    assert [conflict.reason.value for conflict in plan.conflicts] == ["queue_state_mismatch"]


@pytest.mark.parametrize("active_rows", [(), (_active(),)])
def test_checkpointless_review_is_a_conflict_with_no_mutations(
    active_rows: tuple[ActiveReviewRow, ...],
) -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={}).plan((_queue(),), active_rows)

    _assert_no_mutations(plan)
    assert len(plan.conflicts) == 1
    assert plan.conflicts[0].reason is ReviewSyncConflictReason.MISSING_CHECKPOINT
    assert plan.conflicts[0].recipe_ids == ("pasta",)


@pytest.mark.parametrize("category", ["Main Courses", "Desserts"])
def test_duplicate_active_url_suppresses_removal_and_queue_update(category: str) -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution()}).plan(
        (_queue(),), (_active(), _active(category=category, review_row_number=7))
    )

    _assert_no_mutations(plan)
    assert ReviewSyncConflictReason.DUPLICATE_SOURCE_URL in {
        conflict.reason for conflict in plan.conflicts
    }


@pytest.mark.parametrize("category", ["Main Courses", "Desserts"])
def test_duplicate_queue_url_across_recipe_ids_suppresses_all_associated_actions(
    category: str,
) -> None:
    plan = ReviewSyncPlanner(
        reviews={"pasta": _review()}, resolutions={"other": _resolution(recipe_id="other")}
    ).plan(
        (
            _queue(),
            _queue("other", source_url=_queue().source_url, category=category, queue_row_number=9),
        ),
        (_active("other", source_url=_queue().source_url),),
    )

    _assert_no_mutations(plan)
    assert all(conflict.recipe_ids == ("other", "pasta") for conflict in plan.conflicts)
    assert ReviewSyncConflictReason.DUPLICATE_SOURCE_URL in {
        conflict.reason for conflict in plan.conflicts
    }


def test_duplicate_recipe_id_with_different_urls_suppresses_append() -> None:
    plan = ReviewSyncPlanner(reviews={"pasta": _review()}, resolutions={}).plan(
        (_queue(), _queue(source_url="https://www.instagram.com/reel/other/", queue_row_number=8)),
        (),
    )

    _assert_no_mutations(plan)
    assert ReviewSyncConflictReason.DUPLICATE_RECIPE_ID in {
        conflict.reason for conflict in plan.conflicts
    }


@pytest.mark.parametrize("resolved", [False, True])
def test_mismatched_category_blocks_every_action_for_the_record(resolved: bool) -> None:
    plan = ReviewSyncPlanner(
        reviews={"pasta": _review()},
        resolutions={"pasta": _resolution()} if resolved else {},
    ).plan((_queue(),), (_active(category="Desserts"),))

    _assert_no_mutations(plan)
    assert plan.conflicts[0].reason is ReviewSyncConflictReason.CATEGORY_MISMATCH


def test_same_recipe_id_with_different_source_urls_is_an_identity_conflict() -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution()}).plan(
        (_queue(),), (_active(source_url="https://www.instagram.com/reel/other/"),)
    )

    _assert_no_mutations(plan)
    assert plan.conflicts[0].reason is ReviewSyncConflictReason.IDENTITY_MISMATCH


def test_same_url_with_different_recipe_ids_is_an_identity_conflict() -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution()}).plan(
        (_queue(),), (_active("other", source_url=_queue().source_url),)
    )

    _assert_no_mutations(plan)
    assert plan.conflicts[0].reason is ReviewSyncConflictReason.IDENTITY_MISMATCH


def test_collision_connected_records_are_all_blocked_but_independent_record_can_append() -> None:
    plan = ReviewSyncPlanner(
        reviews={name: _review(name) for name in ("pasta", "other", "third", "safe")},
        resolutions={},
    ).plan(
        (
            _queue(),
            _queue("other", source_url=_queue().source_url, queue_row_number=3),
            _queue("safe", queue_row_number=4),
        ),
        (_active("other"), _active("third", source_url=_active("other").source_url)),
    )

    assert [action.queue_row.recipe_id for action in plan.appends] == ["safe"]
    assert plan.removals == plan.queue_updates == ()
    assert all(conflict.recipe_ids == ("other", "pasta", "third") for conflict in plan.conflicts)


@pytest.mark.parametrize("kind", ["review", "resolution"])
def test_checkpoint_recipe_id_mismatch_is_a_conflict(kind: str) -> None:
    plan = ReviewSyncPlanner(
        reviews={"pasta": _review("wrong")} if kind == "review" else {},
        resolutions={"pasta": _resolution(recipe_id="wrong")} if kind == "resolution" else {},
    ).plan((_queue(),), ())

    _assert_no_mutations(plan)
    assert plan.conflicts[0].reason is ReviewSyncConflictReason.CHECKPOINT_ID_MISMATCH


def test_orphan_active_row_is_reported_without_removal() -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={"pasta": _resolution()}).plan(
        (), (_active(),)
    )

    _assert_no_mutations(plan)
    assert plan.conflicts[0].reason is ReviewSyncConflictReason.MISSING_QUEUE_ROW


@pytest.mark.parametrize("status", [QueueStatus.PENDING, QueueStatus.PROCESSING, QueueStatus.ERROR])
@pytest.mark.parametrize("resolved", [False, True])
def test_non_review_work_in_progress_queue_rows_are_never_changed(
    status: QueueStatus, resolved: bool
) -> None:
    plan = ReviewSyncPlanner(
        reviews={"pasta": _review()},
        resolutions={"pasta": _resolution()} if resolved else {},
    ).plan((_queue(status=status),), ())

    assert plan == ReviewSyncPlan()


def test_unrelated_non_review_queue_row_does_not_need_checkpoint() -> None:
    plan = ReviewSyncPlanner(reviews={}, resolutions={}).plan(
        (_queue(status=QueueStatus.PENDING),), ()
    )

    assert plan == ReviewSyncPlan()


def test_plan_is_deterministic_under_input_and_mapping_reordering() -> None:
    queues = (_queue("zebra"), _queue("apple", queue_row_number=3), _queue(queue_row_number=4))
    active = (_active("zebra"), _active("apple", review_row_number=3))
    reviews = {"pasta": _review()}
    resolutions = {name: _resolution(recipe_id=name) for name in ("zebra", "apple")}
    planner = ReviewSyncPlanner(reviews=reviews, resolutions=resolutions)
    expected = planner.plan(queues, active)

    assert [action.active_row.recipe_id for action in expected.removals] == ["apple", "zebra"]
    assert [action.queue_row.recipe_id for action in expected.queue_updates] == ["apple", "zebra"]
    for queue_order in permutations(queues):
        for active_order in permutations(active):
            assert planner.plan(queue_order, active_order) == expected
    assert (
        ReviewSyncPlanner(
            reviews=reviews, resolutions=dict(reversed(tuple(resolutions.items())))
        ).plan(queues, active)
        == expected
    )


def test_conflicts_are_deterministic_under_input_reordering() -> None:
    queues = (_queue(), _queue("other", source_url=_queue().source_url, category="Desserts"))
    active = (_active(), _active(review_row_number=8))
    planner = ReviewSyncPlanner(reviews={}, resolutions={})
    expected = planner.plan(queues, active)

    assert expected.conflicts
    for queue_order in permutations(queues):
        for active_order in permutations(active):
            assert planner.plan(queue_order, active_order) == expected


def test_planner_copies_checkpoint_mappings_instead_of_observing_later_mutation() -> None:
    reviews = {"pasta": _review()}
    planner = ReviewSyncPlanner(reviews=reviews, resolutions={})
    reviews.clear()

    plan = planner.plan((_queue(),), ())

    assert len(plan.appends) == 1
    assert plan.conflicts == ()


def test_snapshots_and_action_plan_are_immutable() -> None:
    queue = _queue()
    plan = ReviewSyncPlanner(reviews={"pasta": _review()}, resolutions={}).plan((queue,), ())

    with pytest.raises(ValidationError, match="frozen"):
        queue.status = QueueStatus.PUBLISHED
    with pytest.raises(ValidationError, match="frozen"):
        plan.appends = ()
    with pytest.raises(ValidationError, match="frozen"):
        plan.appends[0].review = _review("other")
