# "phrases" and "radiopanel" share one language list and one report:
# menu files are named after the language names in it (german.txt), phrases use its codes.

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import kv
from ..linter import Document, Linter
from .base import RuleSet, State

TAG = re.compile(r"\{([^{}]*)\}")
# cs2kz prints "{{" as "{": "{{KZ}" shows "{KZ}"
ESCAPED_BRACE = re.compile(r"\{\{[^{}]*\}?")


@dataclass
class Entry:
    file: str
    line: int
    value: str


@dataclass
class PhraseBlock:
    name: str
    file: str
    start_line: int
    end_line: int
    style: str = "sourcemod"
    languages: dict[str, Entry] = field(default_factory=dict)
    format_spec: Optional[str] = None
    placeholders: set = field(default_factory=set)
    ignored: bool = False


@dataclass
class MenuCommand:
    name: str
    start_line: int
    end_line: int
    hotkey: Optional[str] = None
    label: Optional[str] = None
    cmd: Optional[str] = None


@dataclass
class MenuGroup:
    name: str
    start_line: int
    end_line: int
    hotkey: Optional[str] = None
    title: Optional[str] = None
    timeout: Optional[str] = None
    commands: list = field(default_factory=list)


def _sections(node: kv.Node) -> list[kv.Node]:
    return [c for c in node.children if c.is_section] if node.children else []


def format_args(text: str) -> str:
    # Port of mm-utils FormatArgs (translations.cpp); a translation is dropped when it differs from the name's
    args = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "%":
            i += 1
            continue
        at = i + 1
        if at < n and text[at] == "%":
            i = at + 1
            continue
        while at < n and text[at] in "-+ #0123456789.*":
            if text[at] == "*":
                args.append("*")
            at += 1
        while at < n and text[at] in "hlLjzt":
            args.append(text[at])
            at += 1
        known = at < n and text[at] in "diouxXeEfFgGaAcsp"
        args.append(text[at] if known else "?")
        i = at + 1 if known else at
    return "".join(args)


class Translations(State):

    DEFAULT_LANGUAGE_MAP = {
        "arabic": "ar",
        "brazilian": "pt",
        "bulgarian": "bg",
        "czech": "cze",
        "danish": "da",
        "dutch": "nl",
        "english": "en",
        "finnish": "fi",
        "french": "fr",
        "german": "de",
        "greek": "el",
        "hebrew": "he",
        "hungarian": "hu",
        "italian": "it",
        "japanese": "jp",
        "koreana": "ko",
        "korean": "ko",
        "latvian": "lv",
        "lithuanian": "lt",
        "norwegian": "no",
        "polish": "pl",
        "portuguese": "pt_p",
        "romanian": "ro",
        "russian": "ru",
        "schinese": "chi",
        "slovak": "sk",
        "spanish": "es",
        "swedish": "sv",
        "tchinese": "zho",
        "thai": "th",
        "turkish": "tr",
        "ukrainian": "ua",
        "vietnamese": "vi",
    }

    # Shared by cs2kz utils_print.cpp and mm-utils chat_colors.cpp
    COLOR_TAGS = {
        "default",
        "darkred",
        "purple",
        "green",
        "olive",
        "lime",
        "red",
        "grey",
        "yellow",
        "bluegrey",
        "blue",
        "darkblue",
        "grey2",
        "orchid",
        "lightred",
        "gold",
    }
    # CS:GO SourceMod calls \x05 lightgreen (cs2kz: olive); unknown colors count as placeholders
    SOURCEMOD_COLOR_TAGS = COLOR_TAGS | {"lightgreen"}

    # No space flag: "100% of" would otherwise parse as "% o"
    PRINTF_SPECIFIER = re.compile(
        r"%[-+#0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|z|j|t|I64)?[diuxXeEfgGcsp%]"
    )

    MENU_COMMAND_KEYS = {"hotkey", "label", "cmd"}

    def __init__(self, linter: Linter, config) -> None:
        super().__init__(linter, config)
        self._add_issue = linter.add_issue
        languages_path = config.root / config.languages if config.languages else None

        # Base file -> its phrases, with language folder files merged in
        self.all_phrase_blocks: dict[str, list[PhraseBlock]] = {}
        self._phrases: dict[tuple[str, str], PhraseBlock] = {}
        self.docs: dict[str, Document] = {}
        self.global_languages: set[str] = set()

        self.languages_source = (
            languages_path.name if languages_path else "default language list"
        )
        self.language_map, self.known_languages = self._parse_languages(languages_path)
        # "koreana" and "korean" are both "ko"
        self._code_to_names: dict[str, list[str]] = defaultdict(list)
        for name, code in self.language_map.items():
            self._code_to_names[code].append(name)
        self._code_to_name = {c: names[0] for c, names in self._code_to_names.items()}

        self.menu_dir: Optional[Path] = None
        self.menu_files: dict[str, list[MenuGroup]] = {}
        self.menu_languages: set[str] = set()
        self.menu_file_count = 0
        self._report: Optional[dict] = None

    def _default_language_data(self) -> tuple[dict[str, str], set[str]]:
        self.languages_source = "default language list"
        return dict(self.DEFAULT_LANGUAGE_MAP), set(self.DEFAULT_LANGUAGE_MAP.values())

    def _parse_languages(self, path: Optional[Path]) -> tuple[dict[str, str], set[str]]:
        if path is None:
            print("No languages file configured, using default language list")
            return self._default_language_data()

        try:
            text = path.read_text(encoding="utf-8-sig")
        except Exception as e:
            self._add_issue(
                self.linter.display_name(path),
                0,
                "unreadable-file",
                f"Could not read languages file, using default language list: {e}",
            )
            return self._default_language_data()

        nodes, _ = kv.parse(text)
        name_to_code = {
            child.key: child.value
            for node in nodes
            if node.children
            for child in node.children
            if child.value is not None
        }
        # config.txt maps names to codes ("english" "en"), SourceMod's languages.cfg the other way
        if "en" in name_to_code and "en" not in name_to_code.values():
            name_to_code = {name.lower(): code for code, name in name_to_code.items()}

        if name_to_code:
            print(f"Loaded {len(name_to_code)} language entries from {path.name}")
            return name_to_code, set(name_to_code.values())
        else:
            print(f"No languages found in {path.name}, using default language list")
            return self._default_language_data()

    def _get_language_display_name(self, code: str) -> str:
        if code in self._code_to_name:
            return f"{code} ({self._code_to_name[code]})"
        return code

    def _extract_placeholders(self, value: str) -> set[str]:
        return {
            tag
            for tag in TAG.findall(ESCAPED_BRACE.sub("", value))
            if tag.lower() not in self.SOURCEMOD_COLOR_TAGS and not tag.startswith("#")
        }

    def _extract_printf_specifiers(self, value: str) -> list[str]:
        return [s for s in self.PRINTF_SPECIFIER.findall(value) if s != "%%"]

    def _context(self, entry: Entry) -> Optional[str]:
        return self.docs[entry.file].context(entry.line)

    def finish(self) -> None:
        for blocks in self.all_phrase_blocks.values():
            for block in blocks:
                if not block.ignored:
                    self.global_languages.update(block.languages.keys())

        for blocks in self.all_phrase_blocks.values():
            for block in blocks:
                if block.style == "mm-utils":
                    self._check_mm_utils_phrase(block)
                else:
                    self._check_phrase_format_tags(block)
                self._check_phrase_block(block)

        self._check_language_consistency()

    def _base_file(self, doc: Document) -> str:
        # SourceMod: translations/de/x.phrases.txt adds "de" to translations/x.phrases.txt
        base = doc.path.parent.parent / doc.path.name
        if doc.path.parent.name in self.known_languages and base.is_file():
            return self.linter.display_name(base)
        return doc.name

    def lint_phrases(self, doc: Document, style: str) -> None:
        self.docs[doc.name] = doc
        tracker = self.linter.ignore_trackers[doc.name]
        base = self._base_file(doc)
        if style == "mm-utils" and base != doc.name:
            self._add_issue(
                doc.name,
                0,
                "phrase-structure",
                "mm-utils only loads *.phrases.txt from the translations folder itself, so this file is never read",
            )
            return
        blocks = self.all_phrase_blocks.setdefault(base, [])
        # mm-utils shares one phrase table across files
        scope = base if style == "sourcemod" else ""

        for root in doc.nodes:
            if root.children is None:
                self._add_issue(
                    doc.name,
                    root.line,
                    "phrase-structure",
                    f"Expected \"Phrases\" section, found key-value pair '{root.key}'",
                    doc.context(root.line),
                )
                continue

            for node in root.children:
                if node.children is None:
                    self._add_issue(
                        doc.name,
                        node.line,
                        "phrase-structure",
                        f"Expected phrase section, found key-value pair '{node.key}'",
                        doc.context(node.line),
                    )
                    continue

                block = PhraseBlock(
                    name=node.key,
                    file=doc.name,
                    start_line=node.line,
                    end_line=node.end_line,
                    style=style,
                )
                block.ignored = tracker.is_range_ignored(
                    block.start_line, block.end_line
                )

                first_keys: dict[str, str] = {}
                for child in node.children:
                    if child.value is None:
                        self._add_issue(
                            doc.name,
                            child.line,
                            "phrase-structure",
                            f"Unexpected section '{child.key}' in phrase '{node.key}', expected \"key\" \"value\"",
                            doc.context(child.line),
                        )
                    elif child.key == "#format":
                        block.format_spec = child.value
                        block.placeholders = self._parse_format_spec(child.value)
                    elif style == "mm-utils":
                        # mm-utils lowercases keys, last repeat wins. Exact repeats: generic check
                        lang = child.key.lower()
                        first = first_keys.setdefault(lang, child.key)
                        if first != child.key:
                            self._add_issue(
                                doc.name,
                                child.line,
                                "duplicate-key",
                                f"Duplicate '{lang}' translation for phrase '{node.key}', mm-utils ignores the case of '{child.key}'",
                                f"Also at line {block.languages[lang].line}",
                            )
                        block.languages[lang] = Entry(doc.name, child.line, child.value)
                    elif child.key not in block.languages:
                        # Duplicates are reported by the generic duplicate key check
                        block.languages[child.key] = Entry(
                            doc.name, child.line, child.value
                        )

                existing = self._phrases.get((scope, node.key))
                if existing is None:
                    self._phrases[(scope, node.key)] = block
                    blocks.append(block)
                elif existing.file != doc.name:
                    self._merge(
                        existing, block, style == "sourcemod" and block.file == base
                    )
                # A phrase repeated in one file is a duplicate key, and lookups take the first

    def _merge(
        self, block: PhraseBlock, other: PhraseBlock, other_is_base: bool
    ) -> None:
        # The base file's phrase sets the location, #format and ignore state
        if other_is_base:
            block.file, block.start_line, block.end_line = (
                other.file,
                other.start_line,
                other.end_line,
            )
            block.ignored, block.style = other.ignored, other.style
        if block.format_spec is None:
            block.format_spec, block.placeholders = (
                other.format_spec,
                other.placeholders,
            )
        if other.ignored and not other_is_base:
            return
        for lang, entry in other.languages.items():
            if lang not in block.languages:
                block.languages[lang] = entry
                continue
            kept, dropped = (
                (entry, block.languages[lang])
                if other_is_base
                else (block.languages[lang], entry)
            )
            block.languages[lang] = kept
            self._add_issue(
                dropped.file,
                dropped.line,
                "duplicate-key",
                f"Duplicate '{lang}' translation for phrase '{block.name}'",
                f"Also at {kept.file}:{kept.line}",
            )

    def _parse_format_spec(self, format_value: str) -> set[str]:
        # cs2kz names them ("player:s"), SourceMod numbers them ("{1:s},{2:d}")
        placeholders = set()
        for part in format_value.split(","):
            part = part.strip().strip("{}")
            if ":" in part:
                part = part.split(":")[0].strip()
            if part:
                placeholders.add(part)
        return placeholders

    def _tags(self, block: PhraseBlock, lang: str, entry: Entry) -> list[str]:
        value = entry.value
        if block.style == "sourcemod":
            value = ESCAPED_BRACE.sub("", value)
        stray = TAG.sub("", value)
        for brace in "{}":
            if brace in stray:
                self._add_issue(
                    entry.file,
                    entry.line,
                    "unmatched-brace",
                    f"Unmatched '{brace}' in '{lang}' translation for '{block.name}'",
                    self._context(entry),
                )
        return TAG.findall(value)

    def _check_phrase_format_tags(self, block: PhraseBlock) -> None:
        valid_placeholders = set(block.placeholders)
        if "en" in block.languages:
            valid_placeholders.update(
                self._extract_placeholders(block.languages["en"].value)
            )

        for lang, entry in block.languages.items():
            for tag in self._tags(block, lang, entry):
                if tag != tag.lower() and tag.lower() in self.SOURCEMOD_COLOR_TAGS:
                    self._add_issue(
                        entry.file,
                        entry.line,
                        "color-tag-case",
                        f"Color tag {{{tag}}} should be lowercase, cs2kz prints it as text",
                        self._context(entry),
                    )
                elif (
                    tag.lower() not in self.SOURCEMOD_COLOR_TAGS
                    and tag not in valid_placeholders
                    and not re.match(r"^\d+$", tag)
                    and not tag.startswith("#")
                ):
                    self._add_issue(
                        entry.file,
                        entry.line,
                        "unknown-tag",
                        f"Unknown format tag: {{{tag}}}",
                        self._context(entry),
                    )

    def _check_mm_utils_phrase(self, block: PhraseBlock) -> None:
        name_args = format_args(block.name)
        # Non-color tags in the name are filled by the plugin itself (e.g. cs2menus FillTemplate)
        placeholders = set(TAG.findall(block.name)) - self.COLOR_TAGS
        for lang, entry in block.languages.items():
            tags = self._tags(block, lang, entry)
            missing = placeholders - set(tags)
            if not block.ignored and missing:
                self._add_issue(
                    entry.file,
                    entry.line,
                    "missing-placeholder",
                    f"Missing placeholder(s) {{{', '.join(sorted(missing))}}} in '{lang}' translation for '{block.name}' (present in the phrase name)",
                )
            for tag in tags:
                if tag not in self.COLOR_TAGS and tag not in placeholders:
                    self._add_issue(
                        entry.file,
                        entry.line,
                        "unknown-tag",
                        f"Unknown color tag: {{{tag}}}",
                        self._context(entry),
                    )
            if not block.ignored and format_args(entry.value) != name_args:
                self._add_issue(
                    entry.file,
                    entry.line,
                    "format-mismatch",
                    f"Format arguments ({format_args(entry.value) or 'none'}) in '{lang}' translation for '{block.name}' "
                    f"don't match the phrase name ({name_args or 'none'}), so mm-utils leaves it out",
                )

    def _check_phrase_block(self, block: PhraseBlock) -> None:
        if block.ignored:
            return

        if "en" not in block.languages:
            self._add_issue(
                block.file,
                block.start_line,
                "missing-english",
                f"Missing English ('en') translation for phrase '{block.name}'",
            )

        for lang, entry in block.languages.items():
            if not entry.value.strip():
                self._add_issue(
                    entry.file,
                    entry.line,
                    "empty-translation",
                    f"Empty translation for language '{lang}' in phrase '{block.name}'",
                )
            elif entry.value.rstrip().endswith("[...]"):
                self._add_issue(
                    entry.file,
                    entry.line,
                    "truncated-translation",
                    f"Translation appears to be truncated for language '{lang}' in phrase '{block.name}'",
                    entry.value[:60] + "...",
                )

        if "en" in block.languages and block.style == "sourcemod":
            en_value = block.languages["en"].value
            en_placeholders = self._extract_placeholders(en_value)
            en_specifiers = self._extract_printf_specifiers(en_value)

            for lang, entry in block.languages.items():
                if lang == "en":
                    continue

                missing = en_placeholders - self._extract_placeholders(entry.value)
                if missing:
                    self._add_issue(
                        entry.file,
                        entry.line,
                        "missing-placeholder",
                        f"Missing placeholder(s) {{{', '.join(sorted(missing))}}} in '{lang}' translation for '{block.name}' (present in 'en')",
                    )

                # printf args are positional, so order matters as well as count
                lang_specifiers = self._extract_printf_specifiers(entry.value)
                if lang_specifiers != en_specifiers:
                    self._add_issue(
                        entry.file,
                        entry.line,
                        "format-mismatch",
                        f"Format specifiers ({' '.join(lang_specifiers) or 'none'}) in '{lang}' translation for '{block.name}' don't match 'en' ({' '.join(en_specifiers) or 'none'})",
                    )

    def _check_language_consistency(self) -> None:
        if self.global_languages:
            print(
                f"Languages found across all files: {sorted(self.global_languages)}\n"
            )

        for _, blocks in sorted(self.all_phrase_blocks.items()):
            for block in blocks:
                if block.ignored:
                    continue
                for lang in sorted(block.languages.keys()):
                    if lang not in self.known_languages:
                        entry = block.languages[lang]
                        self._add_issue(
                            entry.file,
                            entry.line,
                            "unknown-language",
                            f"Unknown language code '{lang}' in phrase '{block.name}' (not in {self.languages_source})",
                        )

    def lint_radiopanel(self, docs: list[Document]) -> None:
        if not docs:
            return
        self.menu_file_count += len(docs)

        english = next((d for d in docs if d.path.name == "english.txt"), None)
        self.menu_dir = (english or docs[0]).path.parent
        menu_dir_name = self.linter.display_name(self.menu_dir) + "/"

        if english is None:
            if not (self.menu_dir / "english.txt").exists():
                self._add_issue(
                    menu_dir_name,
                    0,
                    "missing-menu-reference",
                    "Missing english.txt - this is the reference file for menu translations",
                )
            return

        english_groups = self._parse_menu_file(english)
        self.menu_files["english.txt"] = english_groups
        self.menu_languages.add("english")

        for doc in docs:
            if doc is english:
                continue

            self.menu_languages.add(doc.path.stem)
            lang_groups = self._parse_menu_file(doc)
            self.menu_files[doc.path.name] = lang_groups
            self._compare_menu_to_english(doc, lang_groups, english_groups)

        self._check_missing_menu_languages(menu_dir_name)

    def _parse_menu_file(self, doc: Document) -> list[MenuGroup]:
        # RadioPanel.txt { Groups { <group> { kv..., Commands { <command> { kv... } } } } }
        groups = []

        for panel in doc.nodes:
            for groups_node in _sections(panel):
                for group_node in _sections(groups_node):
                    groups.append(self._parse_menu_group(doc, group_node))

        return groups

    def _parse_menu_group(self, doc: Document, node: kv.Node) -> MenuGroup:
        if not node.key:
            self._add_issue(doc.name, node.line, "menu-structure", "Empty group name")
        group = MenuGroup(name=node.key, start_line=node.line, end_line=node.end_line)

        for child in node.children or []:
            if child.value is None:
                if child.key == "Commands":
                    for command_node in _sections(child):
                        group.commands.append(
                            self._parse_menu_command(doc, group, command_node)
                        )
                continue

            key = child.key
            if not key:
                self._add_issue(
                    doc.name,
                    child.line,
                    "menu-structure",
                    "Empty key in key-value pair",
                    doc.context(child.line),
                )
            elif key == "hotkey":
                group.hotkey = child.value
            elif key == "title":
                group.title = child.value
            elif key == "timeout":
                group.timeout = child.value
            elif key in self.MENU_COMMAND_KEYS:
                self._add_issue(
                    doc.name,
                    child.line,
                    "menu-structure",
                    f"Command key '{key}' found at group level in '{group.name}' - likely a missing or extra brace",
                )
            else:
                self._add_issue(
                    doc.name,
                    child.line,
                    "unknown-menu-key",
                    f"Unknown key '{key}' in group '{group.name}' (expected: hotkey, title, timeout)",
                )

        return group

    def _parse_menu_command(
        self, doc: Document, group: MenuGroup, node: kv.Node
    ) -> MenuCommand:
        if not node.key:
            self._add_issue(doc.name, node.line, "menu-structure", "Empty command name")
        command = MenuCommand(
            name=node.key, start_line=node.line, end_line=node.end_line
        )

        for child in node.children or []:
            if child.value is None:
                continue
            if not child.key:
                self._add_issue(
                    doc.name,
                    child.line,
                    "menu-structure",
                    "Empty key in key-value pair",
                    doc.context(child.line),
                )
            elif child.key in self.MENU_COMMAND_KEYS:
                setattr(command, child.key, child.value)
            else:
                self._add_issue(
                    doc.name,
                    child.line,
                    "unknown-menu-key",
                    f"Unknown key '{child.key}' in command '{command.name}' (expected: hotkey, label, cmd)",
                )

        return command

    def _compare_menu_to_english(
        self,
        doc: Document,
        lang_groups: list[MenuGroup],
        english_groups: list[MenuGroup],
    ) -> None:
        menu_prefix = doc.name
        filename = doc.path.name
        english_group_map = {g.name: g for g in english_groups}
        lang_group_map = {g.name: g for g in lang_groups}

        for en_group_name in sorted(english_group_map.keys()):
            en_group = english_group_map[en_group_name]

            if en_group_name not in lang_group_map:
                self._add_issue(
                    menu_prefix,
                    0,
                    "missing-menu-entry",
                    f"Missing menu group '{en_group_name}' (exists in english.txt)",
                )
                continue

            lang_group = lang_group_map[en_group_name]

            if (
                en_group.timeout
                and lang_group.timeout
                and en_group.timeout != lang_group.timeout
            ):
                self._add_issue(
                    menu_prefix,
                    lang_group.start_line,
                    "menu-mismatch",
                    f"Timeout mismatch for group '{en_group_name}': english='{en_group.timeout}', {filename}='{lang_group.timeout}'",
                )

            en_commands = {c.name: c for c in en_group.commands}
            lang_commands = {c.name: c for c in lang_group.commands}

            for en_cmd_name in sorted(en_commands.keys()):
                en_cmd = en_commands[en_cmd_name]

                if en_cmd_name not in lang_commands:
                    self._add_issue(
                        menu_prefix,
                        lang_group.start_line,
                        "missing-menu-entry",
                        f"Missing command '{en_cmd_name}' in group '{en_group_name}' (exists in english.txt)",
                    )
                    continue

                lang_cmd = lang_commands[en_cmd_name]

                if en_cmd.label and not lang_cmd.label:
                    self._add_issue(
                        menu_prefix,
                        lang_cmd.start_line,
                        "missing-menu-entry",
                        f"Missing label for command '{en_cmd_name}' in group '{en_group_name}'",
                    )

                for prop in ("hotkey", "cmd"):
                    en_val = getattr(en_cmd, prop)
                    lang_val = getattr(lang_cmd, prop)
                    if en_val is not None and lang_val is None:
                        self._add_issue(
                            menu_prefix,
                            lang_cmd.start_line,
                            "missing-menu-entry",
                            f"Missing '{prop}' property for command '{en_cmd_name}' in group '{en_group_name}'",
                        )
                    elif (
                        en_val is not None
                        and lang_val is not None
                        and en_val != lang_val
                    ):
                        self._add_issue(
                            menu_prefix,
                            lang_cmd.start_line,
                            "menu-mismatch",
                            f"{prop.capitalize()} mismatch for '{en_cmd_name}': english='{en_val}', {filename}='{lang_val}'",
                        )

            for lang_cmd_name in sorted(lang_commands.keys()):
                if lang_cmd_name not in en_commands:
                    lang_cmd = lang_commands[lang_cmd_name]
                    self._add_issue(
                        menu_prefix,
                        lang_cmd.start_line,
                        "extra-menu-entry",
                        f"Extra command '{lang_cmd_name}' in group '{en_group_name}' not in english.txt",
                    )

        for lang_group_name in sorted(lang_group_map.keys()):
            if lang_group_name not in english_group_map:
                self._add_issue(
                    menu_prefix,
                    lang_group_map[lang_group_name].start_line,
                    "extra-menu-entry",
                    f"Extra menu group '{lang_group_name}' not in english.txt",
                )

    def _menu_name(self, code: str) -> Optional[str]:
        names = self._code_to_names.get(code, [])
        for name in names:
            if f"{name}.txt" in self.menu_files or (
                self.menu_dir is not None and (self.menu_dir / f"{name}.txt").exists()
            ):
                return name
        return names[0] if names else None

    def _check_missing_menu_languages(self, menu_dir_name: str) -> None:
        missing_menus = []

        for lang_code in self.known_languages:
            lang_name = self._menu_name(lang_code)
            if lang_name and lang_name not in self.menu_languages:
                menu_file = self.menu_dir / f"{lang_name}.txt"
                if not menu_file.exists():
                    missing_menus.append((lang_name, lang_code))

        for lang_name, lang_code in sorted(missing_menus, key=lambda x: x[0]):
            self._add_issue(
                menu_dir_name,
                0,
                "missing-menu-file",
                f"Missing menu translation file: {lang_name}.txt (for language code '{lang_code}')",
            )

    def _collect_missing_menu_data(self) -> dict:
        if "english.txt" not in self.menu_files:
            return {}

        english_groups = self.menu_files["english.txt"]
        missing_data = {}

        for lang_code in sorted(self.known_languages):
            lang_name = self._menu_name(lang_code)
            if not lang_name:
                continue

            menu_file = f"{lang_name}.txt"

            if menu_file not in self.menu_files:
                if not (self.menu_dir / menu_file).exists():
                    missing_data[lang_code] = {
                        "name": lang_name,
                        "file_exists": False,
                        "missing_groups": sorted([g.name for g in english_groups]),
                        "missing_commands": [],
                    }
            else:
                lang_groups = self.menu_files[menu_file]
                lang_group_map = {g.name: g for g in lang_groups}

                missing_groups = []
                missing_commands = []

                for en_group in english_groups:
                    if en_group.name not in lang_group_map:
                        missing_groups.append(en_group.name)
                    else:
                        lang_group = lang_group_map[en_group.name]
                        lang_cmd_names = {c.name for c in lang_group.commands}

                        for en_cmd in en_group.commands:
                            if en_cmd.name not in lang_cmd_names:
                                missing_commands.append(
                                    {"group": en_group.name, "command": en_cmd.name}
                                )

                if missing_groups or missing_commands:
                    missing_data[lang_code] = {
                        "name": lang_name,
                        "file_exists": True,
                        "missing_groups": sorted(missing_groups),
                        "missing_commands": sorted(
                            missing_commands, key=lambda x: (x["group"], x["command"])
                        ),
                    }

        return missing_data

    def report_data(self) -> dict:
        if self._report is not None:
            return self._report

        total_phrases = total_missing = 0
        missing_per_language: dict[str, int] = defaultdict(int)
        files = {}

        for filename, blocks in sorted(self.all_phrase_blocks.items()):
            phrases = []
            for block in blocks:
                if block.ignored or "en" not in block.languages:
                    continue
                total_phrases += 1
                missing = sorted(self.global_languages - block.languages.keys())
                if not missing:
                    continue
                for lang in missing:
                    missing_per_language[lang] += 1
                total_missing += len(missing)
                phrases.append(
                    {
                        "name": block.name,
                        "line": block.start_line,
                        "english_text": block.languages["en"].value,
                        "existing_languages": sorted(block.languages.keys()),
                        "missing_languages": missing,
                    }
                )
            if phrases:
                phrases.sort(key=lambda x: x["line"])
                files[filename] = {
                    "phrases_with_missing": len(phrases),
                    "phrases": phrases,
                }

        menu_missing = self._collect_missing_menu_data()
        languages = {}
        for lang in sorted(self.global_languages):
            translated = total_phrases - missing_per_language[lang]
            languages[lang] = {
                "name": self._code_to_name.get(lang, lang),
                "coverage_percent": round(
                    self._coverage_pct(translated, total_phrases), 2
                ),
                "translated": translated,
                "missing": missing_per_language[lang],
                "missing_phrases": [
                    {
                        "file": filename,
                        "phrase": phrase["name"],
                        "line": phrase["line"],
                        "english_text": phrase["english_text"],
                    }
                    for filename, file in files.items()
                    for phrase in file["phrases"]
                    if lang in phrase["missing_languages"]
                ],
                "menu": menu_missing.get(
                    lang,
                    {"file_exists": True, "missing_groups": [], "missing_commands": []},
                ),
            }

        self._report = {
            "summary": {
                "total_languages": len(self.global_languages),
                "total_phrases": total_phrases,
                "phrases_with_missing": sum(
                    f["phrases_with_missing"] for f in files.values()
                ),
                "total_missing_entries": total_missing,
                "all_languages": sorted(self.global_languages),
                "known_languages_from_config": sorted(self.known_languages),
            },
            "languages": languages,
            "files": files,
            "menu": {
                "languages_with_menu": sorted(self.menu_languages),
                "missing": menu_missing,
            },
        }
        return self._report

    @staticmethod
    def _coverage_pct(translated: int, total: int) -> float:
        return (translated / total * 100) if total > 0 else 0

    def _coverage_lines(self) -> list[str]:
        data = self.report_data()
        total = data["summary"]["total_phrases"]
        lines = []
        for lang, info in data["languages"].items():
            coverage = self._coverage_pct(info["translated"], total)
            bar_length = int(coverage / 5)
            bar = "█" * bar_length + "░" * (20 - bar_length)
            lang_display = self._get_language_display_name(lang)
            lines.append(
                f"  {lang_display:20} [{bar}] {coverage:5.1f}% ({info['translated']}/{total})"
            )
        return lines

    def _menu_status_lines(self) -> list[str]:
        missing = self.report_data()["menu"]["missing"]
        lines = []
        for lang_code in sorted(self.known_languages):
            lang_name = self._menu_name(lang_code) or lang_code
            if lang_code in missing:
                data = missing[lang_code]
                if not data["file_exists"]:
                    status = "File missing"
                else:
                    count = len(data["missing_groups"]) + len(data["missing_commands"])
                    status = f"{count} missing items"
            elif f"{lang_name}.txt" in self.menu_files:
                status = "Complete"
            else:
                continue
            lines.append(f"  {lang_code:5} ({lang_name:12}): {status}")
        return lines

    def text_report(self) -> str:
        data = self.report_data()
        summary = data["summary"]
        rule, heavy = "-" * 70, "=" * 70
        out = [
            heavy,
            "MISSING TRANSLATIONS REPORT",
            f"Generated for: {self.linter.root}",
            heavy,
            "",
            "SUMMARY",
            rule,
            f"Total languages: {summary['total_languages']}",
            f"Total phrases: {summary['total_phrases']}",
            f"Phrases with missing translations: {summary['phrases_with_missing']}",
            f"Total missing translation entries: {summary['total_missing_entries']}",
            "",
            "LANGUAGE COVERAGE (Phrases)",
            rule,
            "",
            *self._coverage_lines(),
            "",
            f"Global languages ({len(summary['all_languages'])}): {', '.join(summary['all_languages'])}",
        ]

        menu_missing = data["menu"]["missing"]
        if menu_missing or self.menu_files:
            out += [
                "",
                heavy,
                "MENU TRANSLATION STATUS",
                rule,
                "",
                *self._menu_status_lines(),
            ]

        out += ["", heavy, "DETAILED MISSING TRANSLATIONS (Phrases)", heavy]
        for filename, file in data["files"].items():
            out += ["", filename, rule]
            for phrase in file["phrases"]:
                langs = phrase["missing_languages"]
                out.append(f"  Line {phrase['line']}: \"{phrase['name']}\"")
                out.append(f"    Missing ({len(langs)}): {', '.join(langs)}")

        if menu_missing:
            out += ["", heavy, "DETAILED MISSING MENU TRANSLATIONS", heavy]
            for lang_code, menu in menu_missing.items():
                out += ["", f"{menu['name']}.txt ({lang_code})", rule]
                if not menu["file_exists"]:
                    out.append(" Entire file is missing")
                    continue
                if menu["missing_groups"]:
                    out.append(f"  Missing groups: {', '.join(menu['missing_groups'])}")
                for cmd in menu["missing_commands"]:
                    out.append(
                        f"  Missing command: {cmd['command']} (in group {cmd['group']})"
                    )

        out += ["", heavy, ""]
        return "\n".join(out)

    def markdown_report(self, errors: int, warnings: int) -> str:
        summary = self.report_data()["summary"]
        rows = [
            ("Linter Errors", errors),
            ("Linter Warnings", warnings),
            ("Total Languages", summary["total_languages"]),
            ("Total Phrases", summary["total_phrases"]),
            ("Phrases Missing Translations", summary["phrases_with_missing"]),
            ("Total Missing Entries", summary["total_missing_entries"]),
        ]
        if self.menu_file_count:
            rows.append(("Menu Files", self.menu_file_count))

        out = ["## Status", "", "| Metric | Value |", "|--------|-------|"]
        out += [f"| **{name}** | {value} |" for name, value in rows]
        out += ["", "## Language Coverage (Phrases)", "", "```"]
        out += self._coverage_lines() or ["  No coverage data available"]
        out += ["```", ""]
        if self.menu_file_count:
            out += ["## Menu Translation Status", "", "```"]
            out += self._menu_status_lines() or ["  No menu data available"]
            out += ["```", ""]
        return "\n".join(out)

    @staticmethod
    def write(path: Path, content: str, label: str) -> None:
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"Missing translations {label} written to: {path}")

    def write_reports(self, errors: int, warnings: int) -> None:
        root = self.config.root
        if self.config.report:
            self.write(root / self.config.report, self.text_report(), "report")
        if self.config.json:
            data = json.dumps(self.report_data(), indent=2, ensure_ascii=False)
            self.write(root / self.config.json, data, "JSON")
        if self.config.markdown:
            markdown = self.markdown_report(errors, warnings)
            self.write(root / self.config.markdown, markdown, "markdown")


class PhrasesRules(RuleSet):
    name = "phrases"
    state = Translations
    escapes = True
    # A repeated phrase or language is always a bug
    checks = {"duplicate-key": "error"}
    options = {"style": ("sourcemod", "mm-utils")}

    def lint(
        self, state: Translations, docs: list[Document], options: dict[str, str]
    ) -> None:
        for doc in docs:
            state.lint_phrases(doc, options["style"])


class RadioPanelRules(RuleSet):
    name = "radiopanel"
    state = Translations

    def lint(
        self, state: Translations, docs: list[Document], options: dict[str, str]
    ) -> None:
        state.lint_radiopanel(docs)
