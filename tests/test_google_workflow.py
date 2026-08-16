from __future__ import annotations

from pathlib import Path

from pydantic import HttpUrl

from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.google_oauth import (
    GoogleOAuthServiceFactory,
    _run_local_authorization,
)
from instagram_recipe_transcriber.models import (
    AcquiredRecipe,
    PipelineResult,
    QueuedRecipe,
    RecipeCandidate,
    RecipeDocument,
    RecipeOutcome,
    ValidationArtifact,
)
from instagram_recipe_transcriber.publication import JsonPublicationStore
from instagram_recipe_transcriber.workflow import QueuedRecipeWorkflow


def _queued_recipe() -> QueuedRecipe:
    return QueuedRecipe(
        recipe_id="workflow-recipe",
        source_url=HttpUrl("https://www.instagram.com/reel/DVJBGzyk8E5/"),
        category="Desserts",
        queue_row_number=2,
    )


class _QueueReader:
    def read_next(self) -> QueuedRecipe:
        return _queued_recipe()


class _Acquirer:
    version = "fake-acquirer-v1"

    def acquire(self, queued_recipe: QueuedRecipe) -> AcquiredRecipe:
        from instagram_recipe_transcriber.models import RecipeJob

        return AcquiredRecipe(
            queued_recipe=queued_recipe,
            job=RecipeJob(recipe_id=queued_recipe.recipe_id, source_url=queued_recipe.source_url),
            metadata_path=Path("source.info.json"),
        )


class _Pipeline:
    def run(self, job: object) -> PipelineResult:
        return PipelineResult(
            recipe=RecipeCandidate(title="Published recipe"),
            validation=ValidationArtifact(outcome=RecipeOutcome.READY),
        )


class _DocumentWriter:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, recipe: RecipeCandidate, queued_recipe: QueuedRecipe) -> RecipeDocument:
        self.calls += 1
        return RecipeDocument(
            document_id="document-1",
            title="Published recipe",
            url=HttpUrl("https://docs.google.com/document/d/document-1/edit"),
        )


class _MasterWriter:
    def __init__(self) -> None:
        self.calls = 0

    def append(self, queued_recipe: QueuedRecipe, document: RecipeDocument) -> None:
        self.calls += 1


def test_workflow_persists_publication_before_master_write_and_avoids_duplicates(
    tmp_path: Path,
) -> None:
    writer = _DocumentWriter()
    master_writer = _MasterWriter()
    workflow = QueuedRecipeWorkflow(
        queue_reader=_QueueReader(),
        media_acquirer=_Acquirer(),
        pipeline_runner=_Pipeline(),
        document_writer=writer,
        master_writer=master_writer,
        publication_store=JsonPublicationStore(tmp_path),
    )

    first = workflow.process_next()
    second = workflow.process_next()

    assert first.publication is not None
    assert first.publication.master_row_written
    assert second.publication == first.publication
    assert writer.calls == 1
    assert master_writer.calls == 1


def test_oauth_factory_builds_three_services_without_authorizing(
    tmp_path: Path,
) -> None:
    config = GoogleWorkflowConfig(
        queue_spreadsheet_id="queue-sheet",
        master_spreadsheet_id="master-sheet",
        category_tabs=("Desserts",),
        oauth_client_path=tmp_path / "client.json",
        oauth_token_path=tmp_path / "token.json",
        working_root=tmp_path / "working",
    )
    calls: list[tuple[str, str, object]] = []

    def build_service(name: str, version: str, credentials: object) -> str:
        calls.append((name, version, credentials))
        return f"{name}-service"

    services = GoogleOAuthServiceFactory(
        config,
        credential_provider=lambda _config: "credentials",
        service_builder=build_service,
    ).create_services()

    assert services.sheets == "sheets-service"
    assert services.docs == "docs-service"
    assert services.drive == "drive-service"
    assert calls == [
        ("sheets", "v4", "credentials"),
        ("docs", "v1", "credentials"),
        ("drive", "v3", "credentials"),
    ]


def test_local_oauth_prints_url_instead_of_requiring_wsl_browser() -> None:
    class Flow:
        def __init__(self) -> None:
            self.kwargs: dict[str, object] = {}

        def run_local_server(self, **kwargs: object) -> str:
            self.kwargs = kwargs
            return "credentials"

    flow = Flow()

    credentials = _run_local_authorization(flow)

    assert credentials == "credentials"
    assert flow.kwargs["port"] == 0
    assert flow.kwargs["open_browser"] is False
    assert "Windows browser" in str(flow.kwargs["authorization_prompt_message"])
