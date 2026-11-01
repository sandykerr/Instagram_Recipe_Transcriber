from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import HttpUrl

from instagram_recipe_transcriber.acquisition import recipe_id_for_url
from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.models import (
    RecipeDocument,
    ReviewArtifact,
    ReviewDecisionStatus,
    ReviewResolutionArtifact,
)
from instagram_recipe_transcriber.publication import JsonReviewResolutionStore, JsonReviewStore


def _command() -> Any:
    path = Path(__file__).parent / "scripts" / "sync_google_review_state.py"
    spec = importlib.util.spec_from_file_location("sync_google_review_state", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Sheets:
    def __init__(self, *, conflict: bool = False, malformed: bool = False) -> None:
        self.mutations: list[tuple[str, dict[str, Any]]] = []
        self.gets: list[dict[str, Any]] = []
        self.conflict = conflict
        self.malformed = malformed

    def spreadsheets(self) -> Sheets:
        return self

    def values(self) -> Sheets:
        return self

    def get(self, **kwargs: Any) -> SimpleNamespace:
        self.gets.append(kwargs)
        if "range" not in kwargs:
            response = {"sheets": [{"properties": {"title": "Desserts", "sheetId": 7}}]}
        else:
            values = {
                ("input", "'Desserts'!A2:D"): [
                    ["https://example.com/restore", "Restore", "Review"],
                    ["https://example.com/resolve", "Resolve", "Review", "old"],
                ],
                ("input", "'Empty'!A2:D"): [],
                ("review", "'Desserts'!A2:F"): [
                    [
                        "https://example.com/resolve",
                        "Resolve",
                        "Accepted",
                        "https://example.com/doc",
                    ],
                ],
                ("review", "'Empty'!A2:F"): [],
                ("review", "'Desserts'!A2:F2"): [["https://example.com/resolve"]],
            }
            if self.conflict:
                values[("input", "'Desserts'!A2:D")].append(
                    ["https://example.com/no-checkpoint", "", "Review"]
                )
            if self.malformed:
                values[("review", "'Empty'!A2:F")] = [["", "orphan"]]
            response = {"values": values[(kwargs["spreadsheetId"], kwargs["range"])]}
        return SimpleNamespace(execute=lambda: response)

    def append(self, **kwargs: Any) -> SimpleNamespace:
        self.mutations.append(("append", kwargs))
        return SimpleNamespace(execute=lambda: {})

    def update(self, **kwargs: Any) -> SimpleNamespace:
        self.mutations.append(("update", kwargs))
        return SimpleNamespace(execute=lambda: {})

    def batchUpdate(self, **kwargs: Any) -> SimpleNamespace:
        self.mutations.append(("delete", kwargs))
        return SimpleNamespace(execute=lambda: {})


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sheets: Sheets) -> tuple[Any, Path]:
    config = GoogleWorkflowConfig(
        queue_spreadsheet_id="input",
        master_spreadsheet_id="unused",
        review_spreadsheet_id="review",
        category_tabs=("Desserts", "Empty"),
        oauth_client_path=tmp_path / "client.json",
        oauth_token_path=tmp_path / "token.json",
        working_root=tmp_path / "work",
    )
    config_path = tmp_path / "config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    for slug in ("restore", "resolve"):
        recipe_id = recipe_id_for_url(f"https://example.com/{slug}")
        document = RecipeDocument(
            document_id=slug, title=slug, url=HttpUrl(f"https://example.com/{slug}-doc")
        )
        JsonReviewStore(config.working_root / "review-publication").save(
            ReviewArtifact(recipe_id=recipe_id, document=document, review_row_written=True)
        )
        if slug == "resolve":
            JsonReviewResolutionStore(config.working_root / "review-resolution").save(
                ReviewResolutionArtifact(
                    recipe_id=recipe_id,
                    document=document,
                    decision=ReviewDecisionStatus.ACCEPTED,
                    master_row_written=True,
                )
            )
    command = _command()
    monkeypatch.setattr(
        command,
        "GoogleOAuthServiceFactory",
        lambda _: SimpleNamespace(
            create_services=lambda: SimpleNamespace(sheets=sheets),
        ),
    )
    return command, config_path


def test_command_defaults_to_deterministic_dry_run_without_any_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sheets = Sheets()
    command, config_path = _setup(tmp_path, monkeypatch, sheets)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*.json")}

    assert command.main(["--config", str(config_path)]) == 0
    first = capsys.readouterr().out
    assert command.main(["--config", str(config_path)]) == 0
    assert capsys.readouterr().out == first
    assert "Planned: append=1 remove=1 update=1 conflict=0" in first
    assert "Dry-run" in first
    ids = sorted(
        recipe_id_for_url(f"https://example.com/{slug}") for slug in ("restore", "resolve")
    )
    assert f"Affected recipe IDs: {', '.join(ids)}" in first
    assert sheets.mutations == []
    assert before == {path: path.read_bytes() for path in tmp_path.rglob("*.json")}
    assert len([call for call in sheets.gets if "range" in call]) == 8


def test_command_execute_applies_checkpoint_payloads_and_prints_completed_counts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sheets = Sheets()
    command, config_path = _setup(tmp_path, monkeypatch, sheets)

    assert command.main(["--config", str(config_path), "--execute-writes"]) == 0
    assert "Completed: append=1 remove=1 update=1" in capsys.readouterr().out
    assert len(sheets.mutations) == 3
    calls = dict(sheets.mutations)
    assert set(calls) == {"append", "update", "delete"}
    assert calls["append"]["spreadsheetId"] == "review"
    assert calls["append"]["body"] == {
        "values": [
            [
                "https://example.com/restore",
                "Restore",
                "review",
                "https://example.com/restore-doc",
                "",
                "",
            ]
        ]
    }
    assert calls["update"]["spreadsheetId"] == "input"
    assert calls["update"]["range"] == "'Desserts'!C3:D3"
    assert calls["update"]["body"] == {"values": [["published", "https://example.com/resolve-doc"]]}
    assert calls["delete"]["spreadsheetId"] == "review"


@pytest.mark.parametrize("malformed", [False, True])
def test_command_refuses_conflicts_and_malformed_rows_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    malformed: bool,
) -> None:
    sheets = Sheets(conflict=not malformed, malformed=malformed)
    command, config_path = _setup(tmp_path, monkeypatch, sheets)

    assert command.main(["--config", str(config_path), "--execute-writes"]) != 0
    output = capsys.readouterr()
    assert sheets.mutations == []
    assert "Completed:" not in output.out
    if malformed:
        assert "Empty" in output.err and "2" in output.err
    else:
        assert "conflict=1" in output.out
        assert "missing_checkpoint" in output.out
        assert recipe_id_for_url("https://example.com/no-checkpoint") in output.out


@pytest.mark.parametrize("decision", list(ReviewDecisionStatus))
def test_command_incomplete_resolution_blocks_even_unrelated_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    decision: ReviewDecisionStatus,
) -> None:
    sheets = Sheets()
    command, config_path = _setup(tmp_path, monkeypatch, sheets)
    store = JsonReviewResolutionStore(tmp_path / "work" / "review-resolution")
    recipe_id = recipe_id_for_url("https://example.com/resolve")
    resolution = store.load(recipe_id)
    assert resolution is not None
    store.save(
        resolution.model_copy(
            update={
                "decision": decision,
                "master_row_written": False,
                "rejected_row_written": False,
            }
        )
    )

    assert command.main(["--config", str(config_path), "--execute-writes"]) != 0

    assert sheets.mutations == []
    assert "Planned: append=1 remove=0 update=0 conflict=1" in capsys.readouterr().out
