# "sourcemod": SourceMod gameconf (\x55\x8B, @symbol).
# "ida": cs2fixes-style plugins such as cs2kz, scanned by KHook ("48 8B ?? 05").

import re
from pathlib import Path
from typing import Optional

from .. import kv
from ..linter import Document
from .base import RuleSet, State

SM_BYTES = re.compile(r"(?:\\x[0-9A-Fa-f]{2})+")
# KHook splits on single spaces and reads the last two characters of each token,
# so anything else either fails the lookup or silently scans for other bytes
IDA_BYTES = re.compile(r"(?:[0-9A-Fa-f]{2}|\?\??)(?: (?:[0-9A-Fa-f]{2}|\?\??))*")
NUMBER = re.compile(r"-?(?:0[xX][0-9A-Fa-f]+|\d+)")
# A heuristic: only scanning the binary proves a signature unique
MIN_FIXED_BYTES = 5

PLATFORMS = {
    "sourcemod": ("windows", "linux", "mac", "windows64", "linux64", "mac64"),
    "ida": ("windows", "linux"),
}
SIGNATURE_KEYS = {"sourcemod": {"library"}, "ida": {"library", "allow_multi_match"}}
OFFSET_KEYS = {"sourcemod": {"class", "prop"}, "ida": set()}
# Pairs in DHooks "Functions" entries naming other gamedata entries
REFERENCES = {"signature": "Signatures", "offset": "Offsets", "address": "Addresses"}
SECTIONS = ("Signatures", "Offsets", "Addresses", "Patches", "Functions", "Keys")


def _scope(path: Path) -> Path:
    # SourceMod merges a master.games.txt folder (approximated as all of it), else x.txt with custom/x.txt
    folder = path.parent.parent if path.parent.name == "custom" else path.parent
    if (folder / "master.games.txt").is_file():
        return folder
    return folder / path.name


class Gamedata(State):
    def __init__(self, linter, config) -> None:
        super().__init__(linter, config)
        self._add_issue = linter.add_issue
        self.names: dict[Path, dict[str, set[str]]] = {}
        self.references: list[tuple[Path, str, str, str, int, str]] = []

    def lint(self, doc: Document, style: str) -> None:
        names = self.names.setdefault(
            _scope(doc.path), {section: set() for section in REFERENCES.values()}
        )
        # SourceMod streams the file, so repeated game and section blocks merge.
        # KeyValues FindKey (ida style) only reads the first. A repeated entry is lost either way.
        blocks: dict[tuple[str, ...], int] = {}
        entries: dict[tuple, int] = {}
        for root in doc.nodes:
            if root.key == "Game Master" and root.children is not None:
                self._master(doc, root)
                continue
            if root.key != "Games" or root.children is None:
                self._structure(
                    doc, root, f"Expected a \"Games\" section, found '{root.key}'"
                )
                continue
            for game in root.children:
                if game.children is None:
                    self._structure(
                        doc,
                        game,
                        f"Expected a game section, found key-value pair '{game.key}'",
                    )
                    continue
                if style == "ida":
                    self._once(
                        doc,
                        blocks,
                        (game.key.lower(),),
                        game,
                        f"Game section '{game.key}' repeats, only the first is read",
                    )
                # Blocks of one game with different "#supported" conditions apply to different games
                scope = (
                    game.key,
                    *sorted(
                        (c.key, c.value or "")
                        for s in game.children
                        if s.key == "#supported"
                        for c in s.children or []
                    ),
                )
                for section in game.children:
                    if section.children is None:
                        self._structure(
                            doc,
                            section,
                            f"Expected a section in '{game.key}', found key-value pair '{section.key}'",
                        )
                        continue
                    name = section.key
                    if style == "ida":
                        # KeyValues FindKey ignores case
                        name = next(
                            (s for s in SECTIONS if s.lower() == name.lower()), name
                        )
                        if name != section.key:
                            self._add_issue(
                                doc.name,
                                section.line,
                                "gamedata-key-case",
                                f"Section '{section.key}' in '{game.key}' should be '{name}'",
                                doc.context(section.line),
                            )
                        self._once(
                            doc,
                            blocks,
                            (game.key.lower(), name.lower()),
                            section,
                            f"Section '{section.key}' repeats in '{game.key}', only the first is read",
                        )
                    if name not in SECTIONS:
                        continue
                    for entry in section.children:
                        self._once(
                            doc,
                            entries,
                            (scope, name, entry.key),
                            entry,
                            f"'{entry.key}' is defined twice in '{game.key}' {name}",
                        )
                        if name == "Keys":
                            continue
                        if entry.children is None:
                            self._structure(
                                doc,
                                entry,
                                f"Expected a '{name}' entry section, found key-value pair '{entry.key}'",
                            )
                        else:
                            self._entry(doc, style, name, entry, names)

    def finish(self) -> None:
        for folder, section, name, file, line, entry in self.references:
            if name not in self.names[folder][section]:
                kind = next(k for k, s in REFERENCES.items() if s == section)
                self._add_issue(
                    file,
                    line,
                    "unknown-reference",
                    f"'{entry}' refers to {kind} '{name}', which isn't defined",
                )

    def _once(
        self, doc: Document, seen: dict, key: tuple, node: kv.Node, message: str
    ) -> None:
        first = seen.setdefault(key, node.line)
        if first != node.line:
            self._add_issue(
                doc.name,
                node.line,
                "duplicate-entry",
                message,
                f"First occurrence at line {first}",
            )

    def _master(self, doc: Document, root: kv.Node) -> None:
        # master.games.txt lists the folder's gamedata files, each with optional conditions
        for entry in root.children or []:
            if not (doc.path.parent / entry.key).is_file():
                self._add_issue(
                    doc.name,
                    entry.line,
                    "unknown-reference",
                    f"Master file lists '{entry.key}', which doesn't exist",
                )

    def _structure(self, doc: Document, node: kv.Node, message: str) -> None:
        self._add_issue(
            doc.name, node.line, "gamedata-structure", message, doc.context(node.line)
        )

    def _entry(self, doc, style, section, entry, names) -> None:
        if section == "Signatures":
            names["Signatures"].add(entry.key)
            self._signature(doc, style, entry)
        elif section == "Offsets":
            names["Offsets"].add(entry.key)
            self._offset(doc, style, entry)
        elif section == "Addresses" and style == "sourcemod":
            names["Addresses"].add(entry.key)
            self._address(doc, entry)
        elif section == "Patches" and style == "ida":
            self._patch(doc, entry)
        elif section == "Functions" and style == "sourcemod":
            self._references(doc, entry, entry)

    def _platform(
        self, doc: Document, style: str, kind: str, entry: kv.Node, child: kv.Node
    ) -> bool:
        if child.key in PLATFORMS[style]:
            return True
        if child.key.lower() in PLATFORMS[style]:
            if style == "sourcemod":
                self._add_issue(
                    doc.name,
                    child.line,
                    "unknown-gamedata-key",
                    f"Platform key '{child.key}' in {kind} '{entry.key}' is never read, SourceMod compares keys case-sensitively",
                    doc.context(child.line),
                )
                return False
            self._add_issue(
                doc.name,
                child.line,
                "gamedata-key-case",
                f"Platform key '{child.key}' in {kind} '{entry.key}' should be lowercase",
                doc.context(child.line),
            )
            return True
        self._add_issue(
            doc.name,
            child.line,
            "unknown-gamedata-key",
            f"Unknown key '{child.key}' in {kind} '{entry.key}' (expected: {', '.join(PLATFORMS[style])})",
            doc.context(child.line),
        )
        return False

    def _missing_platform(
        self, doc: Document, kind: str, entry: kv.Node, platforms: set[str]
    ) -> None:
        families = {p.rstrip("64") for p in platforms} & {"windows", "linux"}
        if len(families) == 1:
            missing = ({"windows", "linux"} - families).pop()
            self._add_issue(
                doc.name,
                entry.line,
                "missing-platform",
                f"{kind.capitalize()} '{entry.key}' has no {missing} entry",
            )

    def _bytes(
        self,
        doc: Document,
        style: str,
        kind: str,
        entry: kv.Node,
        child: kv.Node,
        wildcards: bool,
    ) -> None:
        value = child.value or ""
        where = f"{kind} '{entry.key}' ({child.key})"
        if value.startswith("@") and wildcards:
            if len(value) == 1 or any(c.isspace() for c in value):
                self._invalid(doc, child, f"Invalid symbol in {where}")
            return

        pattern, other = (
            (SM_BYTES, IDA_BYTES) if style == "sourcemod" else (IDA_BYTES, SM_BYTES)
        )
        if not pattern.fullmatch(value):
            if other.fullmatch(value):
                hint = (
                    "IDA-style bytes, which SourceMod reads as literal characters"
                    if style == "sourcemod"
                    else 'SourceMod-style \\x bytes, expected "48 8B ?? 05"'
                )
            elif style == "ida":
                hint = "expected two-digit hex bytes or ?/?? separated by single spaces"
            else:
                hint = r"expected \xHH bytes (\x2A as wildcard) or @symbol"
            self._invalid(doc, child, f"Invalid bytes in {where}: {hint}")
            return

        if style == "sourcemod":
            tokens = [t.upper() == "2A" for t in re.findall(r"\\x(..)", value)]
        else:
            tokens = ["?" in t for t in value.split(" ")]
        if not wildcards:
            if style == "ida" and any(tokens):
                self._invalid(
                    doc, child, f"Wildcards in {where}, patch bytes are written as-is"
                )
            return
        fixed = tokens.count(False)
        if fixed == 0:
            # Matches at the module's first byte:
            # SourceMod returns its base, cs2kz rejects the multi-match unless allow_multi_match
            check, problem = (
                "wildcard-signature",
                "is only wildcards, so it matches anywhere",
            )
        elif fixed < MIN_FIXED_BYTES:
            check, problem = (
                "short-signature",
                f"has only {fixed} fixed byte(s), so it may match more than once",
            )
        else:
            return
        self._add_issue(
            doc.name,
            child.line,
            check,
            f"{where[0].upper()}{where[1:]} {problem}",
            doc.context(child.line),
        )

    def _invalid(self, doc: Document, child: kv.Node, message: str) -> None:
        self._add_issue(
            doc.name, child.line, "invalid-signature", message, doc.context(child.line)
        )

    def _number(
        self, doc: Document, style: str, kind: str, entry: kv.Node, child: kv.Node
    ) -> None:
        value = (child.value or "").strip()
        if not NUMBER.fullmatch(value):
            self._add_issue(
                doc.name,
                child.line,
                "invalid-offset",
                f"{kind.capitalize()} '{entry.key}' ({child.key}) isn't a number: '{child.value}'",
                doc.context(child.line),
            )
        elif style == "sourcemod" and re.fullmatch(r"-?0\d+", value):
            # strtol stops at the first non-octal digit: "0108" is 010
            octal = re.split("[89]", value)[0]
            self._add_issue(
                doc.name,
                child.line,
                "octal-offset",
                f"{kind.capitalize()} '{entry.key}' ({child.key}) has a leading zero, which SourceMod's strtol reads as octal ({int(octal, 8)})",
                doc.context(child.line),
            )

    def _signature(self, doc: Document, style: str, entry: kv.Node) -> None:
        platforms, has_library = set(), False
        seen: dict[tuple, int] = {}
        for child in entry.children or []:
            self._once(
                doc,
                seen,
                (child.key,),
                child,
                f"'{child.key}' repeats in signature '{entry.key}'",
            )
            if child.value is None:
                self._structure(
                    doc,
                    child,
                    f"Unexpected section '{child.key}' in signature '{entry.key}'",
                )
            elif child.key in SIGNATURE_KEYS[style]:
                has_library |= child.key == "library"
            elif self._platform(doc, style, "signature", entry, child):
                platforms.add(child.key.lower())
                self._bytes(doc, style, "signature", entry, child, wildcards=True)
        if style == "ida" and not has_library:
            self._add_issue(
                doc.name,
                entry.line,
                "missing-library",
                f"Signature '{entry.key}' has no 'library', so it can't be resolved",
            )
        self._missing_platform(doc, "signature", entry, platforms)

    def _offset(self, doc: Document, style: str, entry: kv.Node) -> None:
        platforms, netprop = set(), False
        seen: dict[tuple, int] = {}
        for child in entry.children or []:
            self._once(
                doc,
                seen,
                (child.key,),
                child,
                f"'{child.key}' repeats in offset '{entry.key}'",
            )
            if child.value is None:
                self._structure(
                    doc,
                    child,
                    f"Unexpected section '{child.key}' in offset '{entry.key}'",
                )
            elif child.key in OFFSET_KEYS[style]:
                netprop = True
            elif self._platform(doc, style, "offset", entry, child):
                platforms.add(child.key.lower())
                self._number(doc, style, "offset", entry, child)
        if not netprop:
            self._missing_platform(doc, "offset", entry, platforms)

    def _address(self, doc: Document, entry: kv.Node) -> None:
        has_signature = False
        # A platform subsection holds read/offset entries for that platform only
        for scope in [entry] + [
            c for c in entry.children or [] if c.children is not None
        ]:
            last_offset = None
            for child in scope.children or []:
                if child.value is None:
                    if scope is entry:
                        self._platform(doc, "sourcemod", "address", entry, child)
                    continue
                if child.key == "signature":
                    has_signature = True
                    self.references.append(
                        (
                            _scope(doc.path),
                            "Signatures",
                            child.value,
                            doc.name,
                            child.line,
                            entry.key,
                        )
                    )
                elif child.key in ("read", "offset"):
                    if last_offset is not None:
                        self._structure(
                            doc,
                            last_offset,
                            f"'offset' in address '{entry.key}' must be its last read/offset entry",
                        )
                    self._number(doc, "sourcemod", "address", entry, child)
                    if child.key == "offset":
                        last_offset = child
                else:
                    self._add_issue(
                        doc.name,
                        child.line,
                        "unknown-gamedata-key",
                        f"Unknown key '{child.key}' in address '{entry.key}' (expected: signature, read, offset)",
                        doc.context(child.line),
                    )
        if not has_signature:
            self._structure(
                doc,
                entry,
                f"Address '{entry.key}' has no 'signature', so SourceMod drops it",
            )

    def _patch(self, doc: Document, entry: kv.Node) -> None:
        seen: dict[tuple, int] = {}
        for child in entry.children or []:
            self._once(
                doc,
                seen,
                (child.key,),
                child,
                f"'{child.key}' repeats in patch '{entry.key}'",
            )
            if child.value is None:
                self._structure(
                    doc,
                    child,
                    f"Unexpected section '{child.key}' in patch '{entry.key}'",
                )
            elif self._platform(doc, "ida", "patch", entry, child):
                self._bytes(doc, "ida", "patch", entry, child, wildcards=False)

    def _references(self, doc: Document, entry: kv.Node, node: kv.Node) -> None:
        for child in node.children or []:
            if child.value is not None and child.key in REFERENCES:
                section = REFERENCES[child.key]
                self.references.append(
                    (
                        _scope(doc.path),
                        section,
                        child.value,
                        doc.name,
                        child.line,
                        entry.key,
                    )
                )
            elif child.children is not None:
                self._references(doc, entry, child)


class GamedataRules(RuleSet):
    name = "gamedata"
    state = Gamedata
    # Replaced by duplicate-entry, which knows which repeats merge
    checks = {"duplicate-key": "off"}
    options = {"style": ("sourcemod", "ida")}

    def lint(
        self, state: Optional[State], docs: list[Document], options: dict[str, str]
    ) -> None:
        assert isinstance(state, Gamedata)
        for doc in docs:
            state.lint(doc, options["style"])
