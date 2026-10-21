# "phrases" and "radiopanel" share one language list and one report:
# menu files are named after the language names in it (german.txt), phrases use its codes.

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Optional

from . import kv
from .linter import Document, Linter


@dataclass
class PhraseBlock:
    name: str
    start_line: int
    end_line: int
    languages: dict = field(default_factory=dict)
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


class TranslationRules:

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

    KNOWN_COLOR_TAGS = {
        "default",
        "red",
        "lightred",
        "darkred",
        "bluegrey",
        "blue",
        "darkblue",
        "purple",
        "orchid",
        "yellow",
        "gold",
        "lightgreen",
        "green",
        "lime",
        "grey",
        "grey2",
    }

    # No space flag: "100% of" would otherwise parse as "% o"
    PRINTF_SPECIFIER = re.compile(
        r"%[-+#0]*(?:\d+|\*)?(?:\.(?:\d+|\*))?(?:hh|h|ll|l|L|z|j|t|I64)?[diuxXeEfgGcsp%]"
    )

    MENU_COMMAND_KEYS = {"hotkey", "label", "cmd"}

    def __init__(self, linter: Linter, languages_path: Optional[Path]):
        self.linter = linter
        self._add_issue = linter.add_issue

        self.all_phrase_blocks: dict[str, list[PhraseBlock]] = {}
        self.global_languages: set[str] = set()

        self.languages_source = (
            languages_path.name if languages_path else "default language list"
        )
        self.language_map, self.known_languages = self._parse_languages(languages_path)
        self._code_to_name = {v: k for k, v in self.language_map.items()}

        self.menu_dir: Optional[Path] = None
        self.menu_files: dict[str, list[MenuGroup]] = {}
        self.menu_languages: set[str] = set()

    def _default_language_data(self) -> tuple[dict[str, str], set[str]]:
        self.languages_source = "default language list"
        return dict(self.DEFAULT_LANGUAGE_MAP), set(self.DEFAULT_LANGUAGE_MAP.values())

    def _parse_languages(self, path: Optional[Path]) -> tuple[dict[str, str], set[str]]:
        if path is None:
            print("No languages file configured, using default language list")
            return self._default_language_data()

        if not path.exists():
            print(f"{path} not found, using default language list")
            return self._default_language_data()

        try:
            text = path.read_text(encoding="utf-8-sig")
        except Exception as e:
            print(f"Failed to read {path}: {e}, using default language list")
            return self._default_language_data()

        nodes, _ = kv.parse(text)
        name_to_code = {
            child.key: child.value
            for node in nodes
            if node.children
            for child in node.children
            if child.value is not None
        }

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
        tags = set(re.findall(r"\{([^}]+)\}", value))
        return {
            tag
            for tag in tags
            if tag.lower() not in self.KNOWN_COLOR_TAGS and not tag.startswith("#")
        }

    def _extract_printf_specifiers(self, value: str) -> list[str]:
        return [s for s in self.PRINTF_SPECIFIER.findall(value) if s != "%%"]

    def finish(self) -> None:
        for blocks in self.all_phrase_blocks.values():
            for block in blocks:
                if not block.ignored:
                    self.global_languages.update(block.languages.keys())

        self._check_language_consistency()

    def lint_phrases(self, doc: Document) -> None:
        tracker = self.linter.ignore_trackers[doc.name]
        blocks = []

        for root in doc.nodes:
            if root.children is None:
                self._add_issue(
                    doc.name,
                    root.line,
                    "error",
                    "Format",
                    f"Expected \"Phrases\" section, found key-value pair '{root.key}'",
                    doc.context(root.line),
                )
                continue

            for node in root.children:
                if node.children is None:
                    self._add_issue(
                        doc.name,
                        node.line,
                        "error",
                        "Format",
                        f"Expected phrase section, found key-value pair '{node.key}'",
                        doc.context(node.line),
                    )
                    continue

                block = PhraseBlock(
                    name=node.key, start_line=node.line, end_line=node.end_line
                )
                block.ignored = tracker.is_range_ignored(
                    block.start_line, block.end_line
                )

                for child in node.children:
                    if child.value is None:
                        self._add_issue(
                            doc.name,
                            child.line,
                            "error",
                            "Format",
                            f"Unexpected section '{child.key}' in phrase '{node.key}', expected \"key\" \"value\"",
                            doc.context(child.line),
                        )
                    elif child.key == "#format":
                        block.format_spec = child.value
                        block.placeholders = self._parse_format_spec(child.value)
                    elif child.key not in block.languages:
                        # Duplicates are reported by the generic duplicate key check
                        block.languages[child.key] = (child.line, child.value)

                blocks.append(block)

        self.all_phrase_blocks[doc.name] = blocks
        self._check_phrase_format_tags(doc, blocks)
        self._check_phrase_blocks(doc.name, blocks)

    def _parse_format_spec(self, format_value: str) -> set[str]:
        placeholders = set()
        for part in format_value.split(","):
            part = part.strip()
            if ":" in part:
                part = part.split(":")[0].strip()
            if part:
                placeholders.add(part)
        return placeholders

    def _check_phrase_format_tags(
        self, doc: Document, blocks: list[PhraseBlock]
    ) -> None:
        for block in blocks:
            valid_placeholders = set(block.placeholders)
            if "en" in block.languages:
                valid_placeholders.update(
                    self._extract_placeholders(block.languages["en"][1])
                )

            for line_num, value in block.languages.values():
                for tag in re.findall(r"\{([^}]+)\}", value):
                    tag_lower = tag.lower()
                    if (
                        tag_lower not in self.KNOWN_COLOR_TAGS
                        and tag not in valid_placeholders
                        and not re.match(r"^\d+$", tag)
                        and not tag.startswith("#")
                    ):
                        self._add_issue(
                            doc.name,
                            line_num,
                            "warning",
                            "Format",
                            f"Unknown format tag: {{{tag}}}",
                            doc.context(line_num),
                        )

    def _check_phrase_blocks(self, filename: str, blocks: list[PhraseBlock]) -> None:
        for block in blocks:
            if block.ignored:
                continue

            if "en" not in block.languages:
                self._add_issue(
                    filename,
                    block.start_line,
                    "error",
                    "Missing",
                    f"Missing English ('en') translation for phrase '{block.name}'",
                )

            for lang, (line_num, value) in block.languages.items():
                if not value.strip():
                    self._add_issue(
                        filename,
                        line_num,
                        "warning",
                        "Empty",
                        f"Empty translation for language '{lang}' in phrase '{block.name}'",
                    )
                elif value.rstrip().endswith("[...]"):
                    self._add_issue(
                        filename,
                        line_num,
                        "warning",
                        "Truncated",
                        f"Translation appears to be truncated for language '{lang}' in phrase '{block.name}'",
                        value[:60] + "...",
                    )

            if "en" in block.languages:
                en_value = block.languages["en"][1]
                en_placeholders = self._extract_placeholders(en_value)
                en_specifiers = self._extract_printf_specifiers(en_value)

                for lang, (line_num, value) in block.languages.items():
                    if lang == "en":
                        continue

                    lang_placeholders = self._extract_placeholders(value)
                    missing = en_placeholders - lang_placeholders

                    if missing:
                        self._add_issue(
                            filename,
                            line_num,
                            "error",
                            "Placeholder",
                            f"Missing placeholder(s) {{{', '.join(sorted(missing))}}} in '{lang}' translation for '{block.name}' (present in 'en')",
                        )

                    # printf args are positional, so order matters as well as count
                    lang_specifiers = self._extract_printf_specifiers(value)
                    if lang_specifiers != en_specifiers:
                        self._add_issue(
                            filename,
                            line_num,
                            "error",
                            "Specifier",
                            f"Format specifiers ({' '.join(lang_specifiers) or 'none'}) in '{lang}' translation for '{block.name}' don't match 'en' ({' '.join(en_specifiers) or 'none'})",
                        )

    def _check_language_consistency(self) -> None:
        if self.global_languages:
            print(
                f"Languages found across all files: {sorted(self.global_languages)}\n"
            )

        for filename, blocks in sorted(self.all_phrase_blocks.items()):
            for block in blocks:
                if block.ignored:
                    continue
                for lang in sorted(block.languages.keys()):
                    if lang not in self.known_languages:
                        line_num = block.languages[lang][0]
                        self._add_issue(
                            filename,
                            line_num,
                            "error",
                            "Unknown",
                            f"Unknown language code '{lang}' in phrase '{block.name}' (not in {self.languages_source})",
                        )

    def lint_radiopanel(self, docs: list[Document]) -> None:
        if not docs:
            return

        english = next((d for d in docs if d.path.name == "english.txt"), None)
        self.menu_dir = (english or docs[0]).path.parent
        menu_dir_name = self.linter.display_name(self.menu_dir) + "/"

        if english is None:
            if not (self.menu_dir / "english.txt").exists():
                self._add_issue(
                    menu_dir_name,
                    0,
                    "error",
                    "Missing",
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
            self._add_issue(doc.name, node.line, "error", "Syntax", "Empty group name")
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
                    "error",
                    "Syntax",
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
                    "error",
                    "Structural",
                    f"Command key '{key}' found at group level in '{group.name}' - likely a missing or extra brace",
                )
            else:
                self._add_issue(
                    doc.name,
                    child.line,
                    "warning",
                    "Unknown Key",
                    f"Unknown key '{key}' in group '{group.name}' (expected: hotkey, title, timeout)",
                )

        return group

    def _parse_menu_command(
        self, doc: Document, group: MenuGroup, node: kv.Node
    ) -> MenuCommand:
        if not node.key:
            self._add_issue(
                doc.name, node.line, "error", "Syntax", "Empty command name"
            )
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
                    "error",
                    "Syntax",
                    "Empty key in key-value pair",
                    doc.context(child.line),
                )
            elif child.key in self.MENU_COMMAND_KEYS:
                setattr(command, child.key, child.value)
            else:
                self._add_issue(
                    doc.name,
                    child.line,
                    "warning",
                    "Unknown Key",
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
                    "warning",
                    "Missing",
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
                    "warning",
                    "Mismatch",
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
                        "warning",
                        "Missing",
                        f"Missing command '{en_cmd_name}' in group '{en_group_name}' (exists in english.txt)",
                    )
                    continue

                lang_cmd = lang_commands[en_cmd_name]

                if en_cmd.label and not lang_cmd.label:
                    self._add_issue(
                        menu_prefix,
                        lang_cmd.start_line,
                        "warning",
                        "Missing",
                        f"Missing label for command '{en_cmd_name}' in group '{en_group_name}'",
                    )

                for prop in ("hotkey", "cmd"):
                    en_val = getattr(en_cmd, prop)
                    lang_val = getattr(lang_cmd, prop)
                    if en_val is not None and lang_val is None:
                        self._add_issue(
                            menu_prefix,
                            lang_cmd.start_line,
                            "warning",
                            "Missing",
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
                            "warning",
                            "Mismatch",
                            f"{prop.capitalize()} mismatch for '{en_cmd_name}': english='{en_val}', {filename}='{lang_val}'",
                        )

            for lang_cmd_name in sorted(lang_commands.keys()):
                if lang_cmd_name not in en_commands:
                    lang_cmd = lang_commands[lang_cmd_name]
                    self._add_issue(
                        menu_prefix,
                        lang_cmd.start_line,
                        "warning",
                        "Extra",
                        f"Extra command '{lang_cmd_name}' in group '{en_group_name}' not in english.txt",
                    )

        for lang_group_name in sorted(lang_group_map.keys()):
            if lang_group_name not in english_group_map:
                self._add_issue(
                    menu_prefix,
                    lang_group_map[lang_group_name].start_line,
                    "warning",
                    "Extra",
                    f"Extra menu group '{lang_group_name}' not in english.txt",
                )

    def _check_missing_menu_languages(self, menu_dir_name: str) -> None:
        missing_menus = []

        for lang_code in self.known_languages:
            lang_name = self._code_to_name.get(lang_code)
            if lang_name and lang_name not in self.menu_languages:
                menu_file = self.menu_dir / f"{lang_name}.txt"
                if not menu_file.exists():
                    missing_menus.append((lang_name, lang_code))

        for lang_name, lang_code in sorted(missing_menus, key=lambda x: x[0]):
            self._add_issue(
                menu_dir_name,
                0,
                "info",
                "Missing",
                f"Missing menu translation file: {lang_name}.txt (for language code '{lang_code}')",
            )

    def _collect_missing_data(self) -> tuple[dict, dict, int, int, int]:
        total_missing = 0
        total_phrases = 0
        phrases_with_missing = 0
        missing_per_language: dict[str, int] = defaultdict(int)
        missing_by_file: dict[str, list] = {}

        for filename, blocks in sorted(self.all_phrase_blocks.items()):
            if not blocks:
                continue

            file_missing_report = []

            for block in blocks:
                if block.ignored:
                    continue
                if "en" not in block.languages:
                    continue

                total_phrases += 1
                missing = self.global_languages - set(block.languages.keys())

                if missing:
                    phrases_with_missing += 1
                    sorted_missing = sorted(missing)
                    file_missing_report.append(
                        {
                            "phrase": block.name,
                            "line": block.start_line,
                            "missing_languages": sorted_missing,
                            "existing_languages": sorted(block.languages.keys()),
                            "english_text": block.languages.get("en", (0, ""))[1],
                        }
                    )

                    for lang in missing:
                        missing_per_language[lang] += 1
                    total_missing += len(missing)

            if file_missing_report:
                missing_by_file[filename] = file_missing_report

        return (
            missing_by_file,
            dict(missing_per_language),
            total_missing,
            total_phrases,
            phrases_with_missing,
        )

    def _collect_missing_menu_data(self) -> dict:
        if "english.txt" not in self.menu_files:
            return {}

        english_groups = self.menu_files["english.txt"]
        missing_data = {}

        for lang_code in sorted(self.known_languages):
            lang_name = self._code_to_name.get(lang_code)
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

    def _get_report_data(self) -> tuple:
        if not hasattr(self, "_cached_report_data"):
            self._cached_report_data = (
                self._collect_missing_data(),
                self._collect_missing_menu_data(),
            )
        return self._cached_report_data

    @staticmethod
    def _coverage_pct(translated: int, total: int) -> float:
        return (translated / total * 100) if total > 0 else 0

    def generate_missing_languages_report(
        self, output_file: Optional[Path] = None
    ) -> str:
        output = StringIO()
        sorted_languages = sorted(self.global_languages)

        (
            missing_by_file,
            missing_per_language,
            total_missing,
            total_phrases,
            phrases_with_missing,
        ), missing_menu_data = self._get_report_data()

        output.write("=" * 70 + "\n")
        output.write("MISSING TRANSLATIONS REPORT\n")
        output.write(f"Generated for: {self.linter.root}\n")
        output.write("=" * 70 + "\n\n")

        output.write("SUMMARY\n")
        output.write("-" * 70 + "\n")
        output.write(f"Total languages: {len(self.global_languages)}\n")
        output.write(f"Total phrases: {total_phrases}\n")
        output.write(f"Phrases with missing translations: {phrases_with_missing}\n")
        output.write(f"Total missing translation entries: {total_missing}\n\n")

        output.write("LANGUAGE COVERAGE (Phrases)\n")
        output.write("-" * 70 + "\n\n")

        for lang in sorted_languages:
            missing_count = missing_per_language.get(lang, 0)
            translated = total_phrases - missing_count
            coverage = self._coverage_pct(translated, total_phrases)
            bar_length = int(coverage / 5)
            bar = "█" * bar_length + "░" * (20 - bar_length)
            lang_display = self._get_language_display_name(lang)
            output.write(
                f"  {lang_display:20} [{bar}] {coverage:5.1f}% ({translated}/{total_phrases})\n"
            )

        output.write("\n")
        output.write(
            f"Global languages ({len(sorted_languages)}): {', '.join(sorted_languages)}\n"
        )

        if missing_menu_data or self.menu_files:
            output.write("\n" + "=" * 70 + "\n")
            output.write("MENU TRANSLATION STATUS\n")
            output.write("-" * 70 + "\n\n")

            for lang_code in sorted(self.known_languages):
                lang_name = self._code_to_name.get(lang_code, lang_code)

                if lang_code in missing_menu_data:
                    data = missing_menu_data[lang_code]
                    if not data["file_exists"]:
                        output.write(
                            f"  {lang_code:5} ({lang_name:12}): File missing\n"
                        )
                    else:
                        issues = len(data["missing_groups"]) + len(
                            data["missing_commands"]
                        )
                        output.write(
                            f"  {lang_code:5} ({lang_name:12}): {issues} missing items\n"
                        )
                elif f"{lang_name}.txt" in self.menu_files:
                    output.write(f"  {lang_code:5} ({lang_name:12}): Complete\n")

        output.write("\n" + "=" * 70 + "\n")
        output.write("DETAILED MISSING TRANSLATIONS (Phrases)\n")
        output.write("=" * 70 + "\n")

        for filename, file_report in sorted(missing_by_file.items()):
            output.write(f"\n{filename}\n")
            output.write("-" * 70 + "\n")

            for item in sorted(file_report, key=lambda x: x["line"]):
                output.write(f"  Line {item['line']}: \"{item['phrase']}\"\n")
                output.write(
                    f"    Missing ({len(item['missing_languages'])}): {', '.join(item['missing_languages'])}\n"
                )

        if missing_menu_data:
            output.write("\n" + "=" * 70 + "\n")
            output.write("DETAILED MISSING MENU TRANSLATIONS\n")
            output.write("=" * 70 + "\n")

            for lang_code, data in sorted(missing_menu_data.items()):
                output.write(f"\n{data['name']}.txt ({lang_code})\n")
                output.write("-" * 70 + "\n")

                if not data["file_exists"]:
                    output.write(" Entire file is missing\n")
                else:
                    if data["missing_groups"]:
                        output.write(
                            f"  Missing groups: {', '.join(data['missing_groups'])}\n"
                        )
                    if data["missing_commands"]:
                        for cmd in data["missing_commands"]:
                            output.write(
                                f"  Missing command: {cmd['command']} (in group {cmd['group']})\n"
                            )

        output.write("\n" + "=" * 70 + "\n")

        report_content = output.getvalue()

        if output_file:
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(report_content)
                print(f"Missing translations report written to: {output_file}")
            except Exception as e:
                print(f"Failed to write report to {output_file}: {e}")

        return report_content

    def generate_missing_languages_json(
        self, output_file: Optional[Path] = None
    ) -> dict:
        sorted_languages = sorted(self.global_languages)
        (
            missing_by_file,
            missing_per_language,
            total_missing,
            total_phrases,
            phrases_with_missing,
        ), missing_menu_data = self._get_report_data()

        languages_data = {}
        for lang in sorted_languages:
            missing_count = missing_per_language.get(lang, 0)
            translated = total_phrases - missing_count
            coverage = self._coverage_pct(translated, total_phrases)

            missing_phrases = []
            for filename, file_report in sorted(missing_by_file.items()):
                for item in file_report:
                    if lang in item["missing_languages"]:
                        missing_phrases.append(
                            {
                                "file": filename,
                                "phrase": item["phrase"],
                                "line": item["line"],
                                "english_text": item["english_text"],
                            }
                        )

            languages_data[lang] = {
                "name": self._code_to_name.get(lang, lang),
                "coverage_percent": round(coverage, 2),
                "translated": translated,
                "missing": missing_count,
                "missing_phrases": missing_phrases,
                "menu": missing_menu_data.get(
                    lang,
                    {"file_exists": True, "missing_groups": [], "missing_commands": []},
                ),
            }

        files_data = {}
        for filename, file_report in sorted(missing_by_file.items()):
            files_data[filename] = {
                "phrases_with_missing": len(file_report),
                "phrases": [
                    {
                        "name": item["phrase"],
                        "line": item["line"],
                        "english_text": item["english_text"],
                        "existing_languages": item["existing_languages"],
                        "missing_languages": item["missing_languages"],
                    }
                    for item in sorted(file_report, key=lambda x: x["line"])
                ],
            }

        json_data = {
            "summary": {
                "total_languages": len(self.global_languages),
                "total_phrases": total_phrases,
                "phrases_with_missing": phrases_with_missing,
                "total_missing_entries": total_missing,
                "all_languages": sorted_languages,
                "known_languages_from_config": sorted(self.known_languages),
            },
            "languages": languages_data,
            "files": files_data,
            "menu": {
                "languages_with_menu": sorted(self.menu_languages),
                "missing": missing_menu_data,
            },
        }

        if output_file:
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(json_data, f, indent=2, ensure_ascii=False)
                print(f"Missing translations JSON written to: {output_file}")
            except Exception as e:
                print(f"Failed to write JSON to {output_file}: {e}")

        return json_data
