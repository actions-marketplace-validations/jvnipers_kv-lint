from typing import ClassVar, Optional

from ..linter import Document, Linter


class State:
    # One per run, shared by every rule set naming this type
    def __init__(self, linter: Linter, config) -> None:
        self.linter = linter
        self.config = config

    def finish(self) -> None:
        pass

    def write_reports(self, errors: int, warnings: int) -> None:
        pass


class RuleSet:
    name: ClassVar[str] = "generic"
    state: ClassVar[Optional[type[State]]] = None
    # Read quoted strings with \" escapes
    escapes: ClassVar[bool] = False
    # Severity overrides for this rule set's files
    checks: ClassVar[dict[str, str]] = {}
    # Option -> allowed values, the first being the default
    options: ClassVar[dict[str, tuple[str, ...]]] = {}

    def lint(
        self, state: Optional[State], docs: list[Document], options: dict[str, str]
    ) -> None:
        pass
