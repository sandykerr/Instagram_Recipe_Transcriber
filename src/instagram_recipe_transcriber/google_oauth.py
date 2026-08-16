"""Local installed-app OAuth bootstrap for Google Workspace services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .config import GoogleWorkflowConfig
from .errors import PipelineOperationalError

GOOGLE_WORKFLOW_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
)

CredentialProvider = Callable[[GoogleWorkflowConfig], object]
ServiceBuilder = Callable[[str, str, object], Any]


@dataclass(frozen=True)
class GoogleWorkspaceServices:
    sheets: Any
    docs: Any
    drive: Any


class GoogleOAuthServiceFactory:
    """Authorizes one local user, refreshes the token, and builds Google services."""

    def __init__(
        self,
        config: GoogleWorkflowConfig,
        *,
        credential_provider: CredentialProvider | None = None,
        service_builder: ServiceBuilder | None = None,
    ) -> None:
        self._config = config
        self._credential_provider = credential_provider or _authorize
        self._service_builder = service_builder or _build_service

    def create_services(self) -> GoogleWorkspaceServices:
        credentials = self._credential_provider(self._config)
        return GoogleWorkspaceServices(
            sheets=self._service_builder("sheets", "v4", credentials),
            docs=self._service_builder("docs", "v1", credentials),
            drive=self._service_builder("drive", "v3", credentials),
        )


def _authorize(config: GoogleWorkflowConfig) -> object:
    if not config.oauth_client_path.is_file():
        raise PipelineOperationalError(
            f"Google OAuth client JSON does not exist: {config.oauth_client_path}"
        )
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]
    except ImportError as error:
        raise PipelineOperationalError(
            "Google workflow requires google-api-python-client and google-auth-oauthlib."
        ) from error

    credentials = None
    if config.oauth_token_path.is_file():
        credentials = Credentials.from_authorized_user_file(  # type: ignore[no-untyped-call]
            str(config.oauth_token_path), GOOGLE_WORKFLOW_SCOPES
        )
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(config.oauth_client_path), GOOGLE_WORKFLOW_SCOPES
            )
            credentials = _run_local_authorization(flow)
        config.oauth_token_path.parent.mkdir(parents=True, exist_ok=True)
        config.oauth_token_path.write_text(credentials.to_json(), encoding="utf-8")
        config.oauth_token_path.chmod(0o600)
    return credentials


def _run_local_authorization(flow: Any) -> Any:
    """Print an OAuth URL instead of requiring a browser executable inside WSL."""
    return flow.run_local_server(
        port=0,
        open_browser=False,
        authorization_prompt_message=(
            "Open this URL in your Windows browser, authorize access, and leave this "
            "terminal running:\n{url}\n"
        ),
    )


def _build_service(name: str, version: str, credentials: object) -> Any:
    try:
        from googleapiclient.discovery import build  # type: ignore[import-untyped]
    except ImportError as error:
        raise PipelineOperationalError(
            "Google workflow requires the google-api-python-client package."
        ) from error
    return build(name, version, credentials=credentials, cache_discovery=False)
