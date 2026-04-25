"""Core shared configuration, models, and error types."""

from .config import PipelineConfig
from .errors import PipelineError, StageError
from .models import StageExecutionRecord
from .types import JsonDict, JsonValue

__all__ = [
    "PipelineConfig",
    "PipelineError",
    "StageError",
    "StageExecutionRecord",
    "JsonDict",
    "JsonValue",
]
