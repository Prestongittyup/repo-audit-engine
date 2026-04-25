from __future__ import annotations

from typing import Dict, List, Union

JsonPrimitive = Union[str, int, float, bool, None]
JsonValue = Union[JsonPrimitive, Dict[str, "JsonValue"], List["JsonValue"]]
JsonDict = Dict[str, JsonValue]
