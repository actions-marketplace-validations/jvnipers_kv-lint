import argparse
import glob
import os
from pathlib import Path
from typing import Optional

from .config import Config, ConfigError, FileSet, load_config
from .linter import Issue, Linter
from .translations import TranslationRules


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def github_annotation(issue: Issue, path: Optional[Path]) -> str:
    level = "error" if issue.issue_type == "error" else "warning"
    props = []
    # Directory-level issues (e.g. "menu/") can't be anchored to a file
    if path is not None and path.is_file():
        props.append(f"file={_escape_property(Path(os.path.relpath(path)).as_posix())}")
        if issue.line > 0:
            props.append(f"line={issue.line}")
    props.append(f"title={_escape_property(issue.category)}")
    message = issue.message
    if issue.context:
        message += f"\n{issue.context}"
    return f"::{level} {','.join(props)}::{_escape_data(message)}"


def lint(config: Config) -> tuple[Linter, Optional[TranslationRules]]:
    linter = Linter(config.root)
    translations = None
    if any(fileset.rules for fileset in config.files):
        languages = config.root / config.languages if config.languages else None
        translations = TranslationRules(linter, languages)

    seen: set[Path] = set()
    for fileset in config.files:
        matches = {
            (config.root / match).resolve()
            for pattern in fileset.globs
            for match in glob.glob(pattern, root_dir=config.root, recursive=True)
        }
        # A file matched by several entries only gets the first entry's rules
        paths = sorted(p for p in matches - seen if p.is_file())
        seen.update(paths)

        label = ", ".join(fileset.globs)
        if not paths:
            print(f"No files match {label}")
            continue
        rules = f" with {fileset.rules} rules" if fileset.rules else ""
        print(f"Checking {len(paths)} file(s) matching {label}{rules}")

        # Phrase files are read with escapes, and a repeated phrase or language is always a bug
        phrases = fileset.rules == "phrases"
        loaded = (
            linter.load(
                p, escapes=phrases, duplicates="error" if phrases else "warning"
            )
            for p in paths
        )
        docs = [doc for doc in loaded if doc]
        if translations is None:
            continue
        if fileset.rules == "phrases":
            for doc in docs:
                translations.lint_phrases(doc)
        elif fileset.rules == "radiopanel":
            translations.lint_radiopanel(docs)

    if translations:
        translations.finish()
    print()
    return linter, translations


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kv-lint",
        description="Lint Valve KeyValues files.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="globs to check with the generic KeyValues checks only",
    )
    parser.add_argument(
        "--config",
        help="config file. Globs in it are relative to its directory (default: kv-lint.toml)",
    )
    parser.add_argument(
        "--phrases",
        action="append",
        default=[],
        metavar="GLOB",
        help="check matching files with the phrases rules, repeatable",
    )
    parser.add_argument(
        "--radiopanel",
        action="append",
        default=[],
        metavar="GLOB",
        help="check matching files with the radiopanel rules, repeatable",
    )
    parser.add_argument("--languages", help="languages file for phrases/radiopanel")
    parser.add_argument("--report", help="write the missing translations report")
    parser.add_argument("--json", help="write the missing translations JSON report")
    parser.add_argument(
        "--output",
        help="also write the issues to this file, only when there are any",
    )
    args = parser.parse_args(argv)

    # Without any of these, the config file is used. Globs are relative to the cwd.
    inline = [
        args.files,
        args.phrases,
        args.radiopanel,
        args.languages,
        args.report,
        args.json,
    ]
    if any(inline):
        if args.config:
            parser.error("--config can't be combined with files or rule/report options")
        # Rule sets first, as a file only gets the first matching entry's rules
        files = [FileSet(args.phrases, "phrases")] if args.phrases else []
        if args.radiopanel:
            files.append(FileSet(args.radiopanel, "radiopanel"))
        if args.files:
            files.append(FileSet(args.files))
        if not files:
            parser.error("no files to check, pass globs, --phrases or --radiopanel")
        config = Config(Path.cwd(), files, args.languages, args.report, args.json)
    else:
        path = args.config or "kv-lint.toml"
        try:
            config = load_config(Path(path))
        except FileNotFoundError:
            parser.error(f"{path} not found. Create it or pass files to check")
        except ConfigError as e:
            parser.error(str(e))

    linter, translations = lint(config)
    summary = linter.summary()
    print(summary)
    if args.output and linter.issues:
        Path(args.output).write_text(summary + "\n", encoding="utf-8")

    if translations:
        if config.report:
            translations.generate_missing_languages_report(config.root / config.report)
        if config.json:
            translations.generate_missing_languages_json(config.root / config.json)

    if os.environ.get("GITHUB_ACTIONS") == "true":
        for issue in linter.issues:
            print(github_annotation(issue, linter.root / issue.file))

    errors = sum(1 for i in linter.issues if i.issue_type == "error")
    return 1 if errors > 0 else 0
