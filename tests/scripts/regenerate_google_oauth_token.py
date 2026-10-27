"""Regenerate the local Google OAuth token without running a workflow.

This utility performs browser-based OAuth and builds authenticated Google service
clients, but it does not read or modify recipe Sheets, Docs, or Drive files.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from instagram_recipe_transcriber.config import GoogleWorkflowConfig
from instagram_recipe_transcriber.google_oauth import GoogleOAuthServiceFactory

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=REPOSITORY_ROOT / "google_workflow_config.json",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and replace an existing token file",
    )
    return parser.parse_args()


def load_config(path: Path) -> GoogleWorkflowConfig:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"Google workflow config does not exist: {resolved}")
    return GoogleWorkflowConfig.model_validate_json(resolved.read_text(encoding="utf-8"))


def prepare_token_path(path: Path, *, force: bool) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise RuntimeError(f"Configured OAuth token path is not a file: {path}")
    if not force:
        raise RuntimeError(
            f"OAuth token already exists: {path}\n"
            "Pass --force to explicitly delete and replace it."
        )
    path.unlink()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    if not config.oauth_client_path.is_file():
        raise FileNotFoundError(
            f"Google OAuth client JSON does not exist: {config.oauth_client_path}"
        )

    prepare_token_path(config.oauth_token_path, force=args.force)
    GoogleOAuthServiceFactory(config).create_services()

    if not config.oauth_token_path.is_file() or config.oauth_token_path.stat().st_size == 0:
        raise RuntimeError("OAuth completed without creating a nonempty token file")
    print(f"Google OAuth token regenerated: {config.oauth_token_path}")
    print("No recipe Sheet, Doc, or Drive workflow write was performed.")


if __name__ == "__main__":
    main()
