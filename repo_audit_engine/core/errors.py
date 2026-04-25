from __future__ import annotations


class PipelineError(RuntimeError):
    pass


class StageError(PipelineError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"[{stage}] {message}")
        self.stage = stage
