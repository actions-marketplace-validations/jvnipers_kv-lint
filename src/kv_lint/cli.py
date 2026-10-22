import argparse
import glob
import os
from pathlib import Path
from typing import Optional

from .checks import SEVERITIES
from .config import (
    Config,
    ConfigError,
    FileSet,
    check_severities,
    load_config,
)
from .linter import Issue, Linter, relative_name
from .rules import GENERIC, RULE_SETS, State


def _escape_data(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(value: str) -> str:
    return _escape_data(value).replace(":", "%3A").replace(",", "%2C")


def github_annotation(issue: Issue, path: Optional[Path]) -> str:
    level = "error" if issue.severity == "error" else "warning"
    props = []
    # Directory-level issues (e.g. "menu/") can't be anchored to a file
    if path is not None and path.is_file():
        props.append(f"file={_escape_property(relative_name(path))}")
        if issue.line > 0:
            props.append(f"line={issue.line}")
    props.append(f"title={_escape_property(issue.check)}")
    message = issue.message
    if issue.context:
        message += f"\n{issue.context}"
    return f"::{level} {','.join(props)}::{_escape_data(message)}"


def lint(config: Config) -> tuple[Linter, list[State]]:
    linter = Linter(config.root, config.checks)
    states: dict[type, State] = {}
    for fileset in config.files:
        state_type = RULE_SETS[fileset.rules].state if fileset.rules else None
        if state_type and state_type not in states:
            states[state_type] = state_type(linter, config)

    seen: set[Path] = set()
    for fileset in config.files:
        rule_set = RULE_SETS[fileset.rules] if fileset.rules else GENERIC
        options = {
            option: fileset.options.get(option, values[0])
            for option, values in rule_set.options.items()
        }
        matches = set()
        for pattern in fileset.globs:
            found = {
                path
                for match in glob.glob(pattern, root_dir=config.root, recursive=True)
                if (path := (config.root / match).resolve()).is_file()
            }
            if not found:
                linter.add_issue(
                    pattern, 0, "no-matching-files", f"No files match '{pattern}'"
                )
            matches |= found
        # A file matched by several entries only gets the first entry's rules
        paths = sorted(matches - seen)
        seen.update(paths)

        label = ", ".join(fileset.globs)
        if not paths:
            if matches:
                print(f"Files matching {label} are already checked by an earlier entry")
            continue
        shown = f" ({', '.join(options.values())})" if options else ""
        rules = f" with {rule_set.name}{shown} rules" if fileset.rules else ""
        print(f"Checking {len(paths)} file(s) matching {label}{rules}")

        loaded = (
            # Valve's own files may be UTF-16, SourceMod and mm-utils only read UTF-8
            linter.load(
                p,
                escapes=rule_set.escapes,
                defaults=rule_set.checks,
                utf16=rule_set is GENERIC,
            )
            for p in paths
        )
        docs = [doc for doc in loaded if doc]
        rule_set.lint(states.get(rule_set.state), docs, options)

    for state in states.values():
        state.finish()
    print()
    return linter, list(states.values())


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
    for rule_set in RULE_SETS.values():
        parser.add_argument(
            f"--{rule_set.name}",
            action="append",
            default=[],
            metavar="GLOB",
            help=f"check matching files with the {rule_set.name} rules, repeatable",
        )
        for option, values in rule_set.options.items():
            parser.add_argument(
                f"--{rule_set.name}-{option}",
                choices=values,
                help=f"{option} for --{rule_set.name} files (default: {values[0]})",
            )
    parser.add_argument("--languages", help="languages file for phrases/radiopanel")
    parser.add_argument("--report", help="write the missing translations report")
    parser.add_argument("--json", help="write the missing translations JSON report")
    parser.add_argument("--markdown", help="write the translations summary as markdown")
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="ID=SEVERITY",
        help=f"override a check's severity ({', '.join(SEVERITIES)}), repeatable",
    )
    parser.add_argument(
        "--output",
        help="also write the issues to this file, only when there are any",
    )
    args = parser.parse_args(argv)

    def arg(name: str):
        return getattr(args, name.replace("-", "_"))

    overrides = {}
    for item in args.check:
        check, sep, severity = item.partition("=")
        if not sep:
            parser.error(f"--check expects ID=SEVERITY, got '{item}'")
        overrides[check] = severity

    rule_args = [
        f"{rule_set.name}-{option}" if option else rule_set.name
        for rule_set in RULE_SETS.values()
        for option in (None, *rule_set.options)
    ]
    # Without any of these, the config file is used. Globs are relative to the cwd.
    inline = [args.files, args.languages, args.report, args.json, args.markdown]
    try:
        overrides = check_severities(overrides, "--check")
        if any(inline) or any(arg(name) for name in rule_args):
            if args.config:
                parser.error(
                    "--config can't be combined with files or rule/report options"
                )
            # Rule sets first, as a file only gets the first matching entry's rules
            files = []
            for rule_set in RULE_SETS.values():
                globs = arg(rule_set.name)
                options = {
                    option: arg(f"{rule_set.name}-{option}")
                    for option in rule_set.options
                    if arg(f"{rule_set.name}-{option}")
                }
                if options and not globs:
                    parser.error(
                        f"--{rule_set.name}-{next(iter(options))} needs --{rule_set.name}"
                    )
                if globs:
                    files.append(FileSet(globs, rule_set.name, options))
            if args.files:
                files.append(FileSet(args.files))
            if not files:
                parser.error("no files to check, pass globs or a rule set option")
            config = Config(
                Path.cwd(), files, args.languages, args.report, args.json, args.markdown
            )
        else:
            path = args.config or "kv-lint.toml"
            try:
                config = load_config(Path(path))
            except FileNotFoundError:
                parser.error(f"{path} not found. Create it or pass files to check")
    except ConfigError as e:
        parser.error(str(e))
    config.checks = {**config.checks, **overrides}

    linter, states = lint(config)
    summary = linter.summary()
    print(summary)

    errors = sum(1 for i in linter.issues if i.severity == "error")
    warnings = sum(1 for i in linter.issues if i.severity == "warning")

    # Info is expected noise (e.g. untranslated menus), kept to the summary and output file
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for issue in linter.issues:
            if issue.severity != "info":
                print(github_annotation(issue, linter.root / issue.file))

    # Exit 2: setup problem, not lint errors
    try:
        if args.output:
            output = Path(args.output)
            # A stale file would read as this run's issues
            if linter.issues:
                output.write_text(summary + "\n", encoding="utf-8")
            else:
                output.unlink(missing_ok=True)
        for state in states:
            state.write_reports(errors, warnings)
    except OSError as e:
        print(f"Failed to write: {e}")
        return 2

    return 1 if errors > 0 else 0
