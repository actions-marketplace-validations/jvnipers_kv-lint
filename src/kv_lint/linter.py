import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from . import kv
from .checks import CHECKS


@dataclass
class Issue:
    file: str
    line: int
    severity: str
    check: str
    message: str
    context: Optional[str] = None

    def __str__(self) -> str:
        msg = f"{self.file}:{self.line}: [{self.severity.upper()}] {self.check}: {self.message}"
        if self.context:
            msg += f"\n    Context: {self.context}"
        return msg


def relative_name(path: Path, start: Optional[Path] = None) -> str:
    try:
        return Path(os.path.relpath(path, start)).as_posix()
    except ValueError:
        # Windows: path on another drive
        return path.as_posix()


# "// lint-ignore[-start|-end|-next] [check ids] [-- reason]"
IGNORE_COMMENT = re.compile(r"//\s*lint-ignore(-start|-end|-next)?(?![\w-])(.*)")


class IgnoreTracker:

    def __init__(self, lines: list[str]):
        # Line -> ignored check ids, None meaning every check
        self.ignored: dict[int, Optional[set[str]]] = {}
        self.unknown_ids: list[tuple[int, str]] = []
        # Line of a lint-ignore-start that no -end closes
        self.unclosed: Optional[int] = None
        # False when inactive, otherwise ids as above
        block: Union[bool, None, set[str]] = False
        pending: Union[bool, None, set[str]] = False

        for line_num, line in enumerate(lines, 1):
            match = IGNORE_COMMENT.search(line)
            if match and match.group(1):
                ids = self._ids(line_num, match.group(2))
                self._add(line_num, None)
                if match.group(1) == "-start":
                    block = ids
                    self.unclosed = line_num
                elif match.group(1) == "-end":
                    block = False
                    self.unclosed = None
                else:
                    pending = ids
                continue

            if match:
                self._add(line_num, self._ids(line_num, match.group(2)))
            if block is not False:
                self._add(line_num, block)
            if pending is not False:
                self._add(line_num, pending)
                pending = False

    def _ids(self, line_num: int, text: str) -> Optional[set[str]]:
        tokens = [t for t in re.split(r"[\s,]+", text.split("--")[0]) if t]
        if not tokens:
            return None
        self.unknown_ids += [(line_num, t) for t in tokens if t not in CHECKS]
        return {t for t in tokens if t in CHECKS}

    def _add(self, line_num: int, ids: Optional[set[str]]) -> None:
        if ids is None:
            self.ignored[line_num] = None
        elif self.ignored.get(line_num, set()) is not None:
            self.ignored.setdefault(line_num, set()).update(ids)

    def is_ignored(self, line_num: int, check: str) -> bool:
        if line_num not in self.ignored:
            return False
        ids = self.ignored[line_num]
        return ids is None or check in ids

    def is_range_ignored(self, start_line: int, end_line: int) -> bool:
        return all(
            line_num in self.ignored and self.ignored[line_num] is None
            for line_num in range(start_line, end_line + 1)
        )


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

    def __init__(self, root: Path, overrides: Optional[dict[str, str]] = None):
        self.root = root.resolve()
        self.issues: list[Issue] = []
        self.ignore_trackers: dict[str, IgnoreTracker] = {}
        self.overrides = overrides or {}
        self.file_defaults: dict[str, dict[str, str]] = {}

    def display_name(self, path: Path) -> str:
        return relative_name(path, self.root)

    def severity(self, filename: str, check: str) -> str:
        if check in self.overrides:
            return self.overrides[check]
        return self.file_defaults.get(filename, {}).get(check, CHECKS[check])

    def add_issue(
        self,
        filename: str,
        line: int,
        check: str,
        message: str,
        context: Optional[str] = None,
    ) -> None:
        severity = self.severity(filename, check)
        if severity == "off":
            return
        tracker = self.ignore_trackers.get(filename)
        if tracker and tracker.is_ignored(line, check):
            return
        self.issues.append(Issue(filename, line, severity, check, message, context))

    def load(
        self,
        path: Path,
        escapes: bool = False,
        defaults: Optional[dict[str, str]] = None,
        utf16: bool = False,
    ) -> Optional[Document]:
        name = self.display_name(path)
        self.file_defaults[name] = defaults or {}
        try:
            data = path.read_bytes()
            if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                if not utf16:
                    self.add_issue(
                        name, 0, "encoding", "File is UTF-16, expected UTF-8"
                    )
                    return None
                text = data.decode("utf-16")
            else:
                text = data.decode("utf-8-sig")
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            self.add_issue(name, 0, "encoding", "File is not valid UTF-8")
            return None
        except Exception as e:
            self.add_issue(name, 0, "unreadable-file", f"Could not read file: {e}")
            return None

        # Not splitlines(): it also breaks on U+2028 etc., the parser doesn't
        lines = text.removesuffix("\n").split("\n")
        tracker = IgnoreTracker(lines)
        # Reported before the tracker is registered, which would hide its own comment lines
        for line_num, check in tracker.unknown_ids:
            self.add_issue(
                name,
                line_num,
                "unknown-ignore",
                f"Unknown check '{check}' in lint-ignore comment",
            )
        if tracker.unclosed:
            self.add_issue(
                name,
                tracker.unclosed,
                "unclosed-ignore",
                "lint-ignore-start has no lint-ignore-end, so the rest of the file is ignored",
            )
        self.ignore_trackers[name] = tracker
        nodes, errors = kv.parse(text, escapes)
        doc = Document(name, path, lines, nodes)

        for line_num, message in errors:
            self.add_issue(name, line_num, "syntax", message, doc.context(line_num))
        self._check_whitespace(name, lines)
        self._check_duplicate_keys(name, nodes, None)
        return doc

    def _check_whitespace(self, filename: str, lines: list[str]) -> None:
        prev_line_empty = False

        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()

            marker = IGNORE_COMMENT.search(line)
            if marker and marker.group(1):
                prev_line_empty = False
                continue

            if line != line.rstrip():
                self.add_issue(
                    filename,
                    line_num,
                    "trailing-whitespace",
                    "Line has trailing whitespace",
                )

            if stripped == "":
                if prev_line_empty:
                    self.add_issue(
                        filename,
                        line_num,
                        "consecutive-blank-lines",
                        "Multiple consecutive empty lines",
                    )
                prev_line_empty = True
            else:
                prev_line_empty = False

    def _check_duplicate_keys(
        self, filename: str, nodes: list[kv.Node], parent: Optional[str]
    ) -> None:
        # A lookup returns the first match, but mm-utils kv::LoadFile passes repeats on,
        # so the default severity is up to the rule set. [$X] keys repeat by design.
        # Case-sensitive like mm-utils, where "None" and "none" are separate phrases.
        seen: dict[str, int] = {}
        where = f"in '{parent}'" if parent is not None else "at top level"

        for node in nodes:
            if node.condition is None:
                if node.key in seen:
                    self.add_issue(
                        filename,
                        node.line,
                        "duplicate-key",
                        f"Duplicate key '{node.key}' {where}",
                        f"First occurrence at line {seen[node.key]}",
                    )
                else:
                    seen[node.key] = node.line
            if node.children is not None:
                self._check_duplicate_keys(filename, node.children, node.key)

    def summary(self) -> str:
        if not self.issues:
            return "No issues found!"

        lines = []
        by_file: dict[str, list[Issue]] = defaultdict(list)
        for issue in self.issues:
            by_file[issue.file].append(issue)

        def counts(issues: list[Issue], plural: str) -> str:
            errors = sum(1 for i in issues if i.severity == "error")
            warnings = sum(1 for i in issues if i.severity == "warning")
            infos = sum(1 for i in issues if i.severity == "info")
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
