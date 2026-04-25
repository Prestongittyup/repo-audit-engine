"""Static analysis package."""

from .ast_parser import parse_python_file
from .static_analyzer import run_static_analysis
from .symbol_extractor import extract_top_level_symbols

__all__ = ["parse_python_file", "run_static_analysis", "extract_top_level_symbols"]
