"""Domain-specific exceptions for recoverable pipeline failures."""


class RecipeTranscriberError(Exception):
    """Base exception for the application."""


class ArtifactPersistenceError(RecipeTranscriberError):
    """Raised when an artifact cannot be safely read or written."""


class PipelineOperationalError(RecipeTranscriberError):
    """Raised when a processing dependency has an operational failure."""
