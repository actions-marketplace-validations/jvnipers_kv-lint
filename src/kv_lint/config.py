import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

RULES = ("phrases", "radiopanel")


class ConfigError(Exception):
    pass


@dataclass
class FileSet:
    globs: list[str]
    rules: Optional[str] = None


@dataclass
class Config:
    root: Path
    files: list[FileSet] = field(default_factory=list)
    languages: Optional[str] = None
    report: Optional[str] = None
    json: Optional[str] = None


def _check_keys(table: dict, allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise ConfigError(f"unknown key(s) {', '.join(unknown)} in {where}")


def _optional_str(table: dict, key: str, where: str) -> Optional[str]:
    value = table.get(key)
    if value is not None and not isinstance(value, str):
        raise ConfigError(f"'{key}' in {where} must be a string")
    return value


def load_config(path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e

    _check_keys(data, {"languages", "report", "json", "files"}, path.name)
    config = Config(
        root=path.resolve().parent,
        languages=_optional_str(data, "languages", path.name),
        report=_optional_str(data, "report", path.name),
        json=_optional_str(data, "json", path.name),
    )

    for index, entry in enumerate(data.get("files", []), 1):
        where = f"[[files]] entry {index}"
        _check_keys(entry, {"glob", "rules"}, where)
        globs = entry.get("glob")
        if isinstance(globs, str):
            globs = [globs]
        if not globs or not all(isinstance(g, str) for g in globs):
            raise ConfigError(f"{where} needs 'glob' as a string or list of strings")
        rules = _optional_str(entry, "rules", where)
        if rules is not None and rules not in RULES:
            raise ConfigError(
                f"{where} has unknown rules '{rules}' (expected one of: {', '.join(RULES)})"
            )
        config.files.append(FileSet(globs, rules))

    if not config.files:
        raise ConfigError(f"{path.name} has no [[files]] entries")
    return config
