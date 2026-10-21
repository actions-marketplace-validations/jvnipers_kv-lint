import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import kv


@dataclass
class Issue:
    file: str
    line: int
    issue_type: str
    category: str
    message: str
    context: Optional[str] = None

    def __str__(self) -> str:
        prefix = self.issue_type.upper()
        msg = f"{self.file}:{self.line}: [{prefix}] {self.category}: {self.message}"
        if self.context:
            msg += f"\n    Context: {self.context}"
        return msg


class IgnoreTracker:

    IGNORE_START = "lint-ignore-start"
    IGNORE_END = "lint-ignore-end"
    IGNORE_NEXT = "lint-ignore-next"
    IGNORE_LINE = "lint-ignore"

    def __init__(self, lines: list[str]):
        self.ignored_lines: set[int] = set()
        self._parse_ignore_comments(lines)

    def _parse_ignore_comments(self, lines: list[str]) -> None:
        in_ignored_block = False
        ignore_next = False

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            if self.IGNORE_START in stripped:
                in_ignored_block = True
                self.ignored_lines.add(line_num)
                continue

            if self.IGNORE_END in stripped:
                in_ignored_block = False
                self.ignored_lines.add(line_num)
                continue

            if self.IGNORE_NEXT in stripped and self.IGNORE_START not in stripped:
                ignore_next = True
                self.ignored_lines.add(line_num)
                continue

            if in_ignored_block:
                self.ignored_lines.add(line_num)
                continue

            if ignore_next:
                self.ignored_lines.add(line_num)
                ignore_next = False
                continue

            if self.IGNORE_LINE in stripped:
                comment_part = stripped.split("//")[-1] if "//" in stripped else ""
                if self.IGNORE_LINE in comment_part:
                    if (
                        self.IGNORE_START not in comment_part
                        and self.IGNORE_END not in comment_part
                        and self.IGNORE_NEXT not in comment_part
                    ):
                        self.ignored_lines.add(line_num)

    def is_ignored(self, line_num: int) -> bool:
        return line_num in self.ignored_lines

    def is_range_ignored(self, start_line: int, end_line: int) -> bool:
        return all(self.is_ignored(ln) for ln in range(start_line, end_line + 1))


@dataclass
class Document:
    name: str  # relative to the lint root
    path: Path
    lines: list[str]
    nodes: list[kv.Node]

    def context(self, line_num: int) -> Optional[str]:
        if 0 < line_num <= len(self.lines):
            return self.lines[line_num - 1].strip()[:80]
        return None


class Linter:

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.issues: list[Issue] = []
        self.ignore_trackers: dict[str, IgnoreTracker] = {}

    def display_name(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.root)).as_posix()

    def add_issue(
        self,
        filename: str,
        line: int,
        issue_type: str,
        category: str,
        message: str,
        context: Optional[str] = None,
    ) -> None:
        if filename in self.ignore_trackers:
            if self.ignore_trackers[filename].is_ignored(line):
                return

        self.issues.append(
            Issue(
                file=filename,
                line=line,
                issue_type=issue_type,
                category=category,
                message=message,
                context=context,
            )
        )

    def load(
        self, path: Path, escapes: bool = False, duplicates: str = "warning"
    ) -> Optional[Document]:
        name = self.display_name(path)
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            self.add_issue(name, 0, "error", "Encoding", "File is not valid UTF-8")
            return None
        except Exception as e:
            self.add_issue(name, 0, "error", "File", f"Could not read file: {e}")
            return None

        lines = text.splitlines()
        self.ignore_trackers[name] = IgnoreTracker(lines)
        nodes, errors = kv.parse(text, escapes)
        doc = Document(name, path, lines, nodes)

        for line_num, message in errors:
            self.add_issue(
                name, line_num, "error", "Syntax", message, doc.context(line_num)
            )
        self._check_whitespace(name, lines)
        self._check_duplicate_keys(name, nodes, None, duplicates)
        return doc

    def _check_whitespace(self, filename: str, lines: list[str]) -> None:
        prev_line_empty = False

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            if any(
                x in stripped
                for x in [
                    IgnoreTracker.IGNORE_START,
                    IgnoreTracker.IGNORE_END,
                    IgnoreTracker.IGNORE_NEXT,
                ]
            ):
                prev_line_empty = False
                continue

            if line != line.rstrip():
                self.add_issue(
                    filename,
                    line_num,
                    "warning",
                    "Formatting",
                    "Line has trailing whitespace",
                )

            if stripped == "":
                if prev_line_empty:
                    self.add_issue(
                        filename,
                        line_num,
                        "warning",
                        "Formatting",
                        "Multiple consecutive empty lines",
                    )
                prev_line_empty = True
            else:
                prev_line_empty = False

    def _check_duplicate_keys(
        self, filename: str, nodes: list[kv.Node], parent: Optional[str], severity: str
    ) -> None:
        # A lookup returns the first match, but mm-utils kv::LoadFile passes repeats on,
        # so this is only an error where the caller says so. [$X] keys repeat by design.
        # Case-sensitive like mm-utils, where "None" and "none" are separate phrases.
        seen: dict[str, int] = {}
        where = f"in '{parent}'" if parent is not None else "at top level"

        for node in nodes:
            if node.condition is None:
                if node.key in seen:
                    self.add_issue(
                        filename,
                        node.line,
                        severity,
                        "Duplicate",
                        f"Duplicate key '{node.key}' {where}",
                        f"First occurrence at line {seen[node.key]}",
                    )
                else:
                    seen[node.key] = node.line
            if node.children is not None:
                self._check_duplicate_keys(filename, node.children, node.key, severity)

    def summary(self) -> str:
        if not self.issues:
            return "No issues found!"

        lines = []
        by_file: dict[str, list[Issue]] = defaultdict(list)
        for issue in self.issues:
            by_file[issue.file].append(issue)

        def counts(issues: list[Issue], plural: str) -> str:
            errors = sum(1 for i in issues if i.issue_type == "error")
            warnings = sum(1 for i in issues if i.issue_type == "warning")
            infos = sum(1 for i in issues if i.issue_type == "info")
            text = f"{errors} error{plural}, {warnings} warning{plural}"
            return text + (f", {infos} info" if infos else "")

        lines.append("=" * 70)
        lines.append(f"SUMMARY: {counts(self.issues, '(s)')}")
        lines.append("=" * 70)

        for filename in sorted(by_file.keys()):
            issues = by_file[filename]
            lines.append(f"\n{filename} ({counts(issues, 's')})")
            lines.append("-" * 50)
            for issue in sorted(issues, key=lambda x: (x.line, x.message)):
                lines.append(f"  {issue}")

        lines.append("\n" + "=" * 70)
        lines.append(f"TOTAL: {counts(self.issues, '(s)')} in {len(by_file)} file(s)")
        lines.append("=" * 70)
        return "\n".join(lines)
