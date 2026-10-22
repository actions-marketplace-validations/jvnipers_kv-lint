import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .checks import CHECKS, SEVERITIES
from .rules import RULE_SETS


class ConfigError(Exception):
    pass


@dataclass
class FileSet:
    globs: list[str]
    rules: Optional[str] = None
    options: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    root: Path
    files: list[FileSet] = field(default_factory=list)
    languages: Optional[str] = None
    report: Optional[str] = None
    json: Optional[str] = None
    markdown: Optional[str] = None
    checks: dict[str, str] = field(default_factory=dict)


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) {', '.join(unknown)} in {where}")


def _optional_str(table: dict, key: str, where: str) -> Optional[str]:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"'{key}' in {where} must be a string")
    return value


def check_severities(table: dict, where: str) -> dict[str, str]:
    for check, severity in table.items():
        if check not in CHECKS:
            raise ConfigError(f"unknown check '{check}' in {where}")
        if severity not in SEVERITIES:
            raise ConfigError(
                f"'{check}' in {where} must be one of: {', '.join(SEVERITIES)}"
            )
    return dict(table)


def check_options(rules: str, options: dict[str, str], where: str) -> dict[str, str]:
    allowed = RULE_SETS[rules].options
    for option, value in options.items():
        if value not in allowed[option]:
            raise ConfigError(
                f"{where} has unknown {option} '{value}' (expected one of: {', '.join(allowed[option])})"
            )
    return options


def load_config(path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        raise
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e

    _check_keys(
        data, {"languages", "report", "json", "markdown", "checks", "files"}, path.name
    )
    checks = data.get("checks", {})
    if not isinstance(checks, dict):
        raise ConfigError(f"'checks' in {path.name} must be a table")
    config = Config(
        root=path.resolve().parent,
        languages=_optional_str(data, "languages", path.name),
        report=_optional_str(data, "report", path.name),
        json=_optional_str(data, "json", path.name),
        markdown=_optional_str(data, "markdown", path.name),
        checks=check_severities(checks, f"[checks] in {path.name}"),
    )

    files = data.get("files", [])
    if not isinstance(files, list) or not all(isinstance(e, dict) for e in files):
        raise ConfigError(f"'files' in {path.name} must be [[files]] tables")
    for index, entry in enumerate(files, 1):
        where = f"[[files]] entry {index}"
        rules = _optional_str(entry, "rules", where)
        if rules is not None and rules not in RULE_SETS:
            raise ConfigError(
                f"{where} has unknown rules '{rules}' (expected one of: {', '.join(RULE_SETS)})"
            )
        option_names = set(RULE_SETS[rules].options) if rules else set()
        _check_keys(entry, {"glob", "rules", *option_names}, where)
        globs = entry.get("glob")
        if isinstance(globs, str):
            globs = [globs]
        if not globs or not all(isinstance(g, str) for g in globs):
            raise ConfigError(f"{where} needs 'glob' as a string or list of strings")
        options = {
            o: value
            for o in option_names
            if (value := _optional_str(entry, o, where)) is not None
        }
        if rules:
            check_options(rules, options, where)
        config.files.append(FileSet(globs, rules, options))

    if not config.files:
        raise ConfigError(f"{path.name} has no [[files]] entries")
    return config
