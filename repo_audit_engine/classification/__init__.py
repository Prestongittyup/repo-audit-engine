"""Code heat and dead-code classification modules."""

from .dead_code import build_dead_code_report, build_dead_code_report_from_artifact
from .engine_v2 import EvidenceClassifier
from .heat_engine import classify_code_heat, classify_code_heat_from_artifacts
from .scoring import compute_heat_score

__all__ = [
	"build_dead_code_report",
	"build_dead_code_report_from_artifact",
	"EvidenceClassifier",
	"classify_code_heat",
	"classify_code_heat_from_artifacts",
	"compute_heat_score",
]
