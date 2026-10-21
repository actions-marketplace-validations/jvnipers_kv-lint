import re

import pytest

from kv_lint.checks import CHECKS
from kv_lint.cli import lint, main
from kv_lint.config import Config, ConfigError, FileSet, load_config
from kv_lint.rules import RULE_SETS
from test_linter import CLEAN, LANGUAGES, run, summary, write

# Line 4 is a duplicate key with trailing whitespace, %s is an optional comment on it
DUPLICATE_WITH_WHITESPACE = '"Root"\n{\n\t"k"\t"1"\n\t"k"\t"2" %s \n}\n'


def generic(root, text):
    write(root, {"a.txt": text})
    return run(root, FileSet(["*.txt"]))


def test_rule_sets_only_name_known_checks():
    for rule_set in RULE_SETS.values():
        assert set(rule_set.checks) <= set(CHECKS)


@pytest.mark.parametrize(
    "comment, left",
    [
        ("// lint-ignore", []),
        ("// lint-ignore trailing-whitespace", ["duplicate-key"]),
        ("// lint-ignore duplicate-key, trailing-whitespace -- legacy file", []),
    ],
)
def test_lint_ignore_by_check(tmp_path, comment, left):
    issues = generic(tmp_path, DUPLICATE_WITH_WHITESPACE % comment)
    assert [i.check for i in issues] == left


def test_lint_ignore_next_and_block_by_check(tmp_path):
    issues = generic(
        tmp_path,
        "\n".join(
            [
                '"Root"',
                "{",
                '\t"k"\t"1"',
                "\t// lint-ignore-next duplicate-key",
                '\t"k"\t"2" ',
                "\t// lint-ignore-start trailing-whitespace",
                '\t"k"\t"3" ',
                "\t// lint-ignore-end",
                "}",
                "",
            ]
        ),
    )
    assert [(i.line, i.check) for i in issues] == [
        (5, "trailing-whitespace"),
        (7, "duplicate-key"),
    ]


def test_unknown_check_in_ignore_is_reported_and_hides_nothing(tmp_path):
    issues = generic(
        tmp_path, DUPLICATE_WITH_WHITESPACE % "// lint-ignore duplicate-keys"
    )
    assert sorted((i.line, i.check) for i in issues) == [
        (4, "duplicate-key"),
        (4, "trailing-whitespace"),
        (4, "unknown-ignore"),
    ]


def test_severity_precedence(tmp_path):
    write(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES,
            "translations/p.phrases.txt": CLEAN.replace(
                '"de"', '"en"\t"again"\n\t\t"de"'
            ),
            "cfg/a.cfg": DUPLICATE_WITH_WHITESPACE % "",
        },
    )
    filesets = [
        FileSet(["translations/*.phrases.txt"], "phrases"),
        FileSet(["cfg/*.cfg"]),
    ]
    # The phrases rule set makes duplicates errors, generic files keep the warning
    assert [
        (i.file, i.severity)
        for i in run(tmp_path, *filesets)
        if i.check == "duplicate-key"
    ] == [("translations/p.phrases.txt", "error"), ("cfg/a.cfg", "warning")]

    config = Config(
        tmp_path,
        filesets,
        "translations/config.txt",
        checks={"duplicate-key": "info", "trailing-whitespace": "off"},
    )
    linter, _ = lint(config)
    assert summary(linter.issues) == [
        ("cfg/a.cfg", 4, "info", "duplicate-key"),
        ("translations/p.phrases.txt", 7, "info", "duplicate-key"),
    ]


def test_config_checks_table(tmp_path):
    path = tmp_path / "kv-lint.toml"
    path.write_text(
        '[checks]\ntrailing-whitespace = "off"\n\n[[files]]\nglob = "*.txt"\n',
        encoding="utf-8",
    )
    assert load_config(path).checks == {"trailing-whitespace": "off"}


@pytest.mark.parametrize(
    "text, message",
    [
        (
            '[checks]\nno-such-check = "off"\n[[files]]\nglob = "a"\n',
            "unknown check 'no-such-check'",
        ),
        (
            '[checks]\nsyntax = "fatal"\n[[files]]\nglob = "a"\n',
            "'syntax' in [checks] in kv-lint.toml must be one of",
        ),
        (
            'checks = "off"\n[[files]]\nglob = "a"\n',
            "'checks' in kv-lint.toml must be a table",
        ),
    ],
)
def test_config_checks_errors(tmp_path, text, message):
    path = tmp_path / "kv-lint.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(path)


def test_cli_check_overrides_config(tmp_path, monkeypatch):
    write(
        tmp_path,
        {
            "a.txt": DUPLICATE_WITH_WHITESPACE % "",
            "kv-lint.toml": '[checks]\nduplicate-key = "error"\n\n[[files]]\nglob = "*.txt"\n',
        },
    )
    monkeypatch.chdir(tmp_path)
    assert main([]) == 1
    assert main(["--check", "duplicate-key=warning"]) == 0
    assert (
        main(["a.txt", "--check", "syntax=off", "--check", "duplicate-key=error"]) == 1
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--check", "duplicate-key"],
        ["--check", "nope=off"],
        ["--check", "syntax=fatal"],
    ],
)
def test_cli_check_errors(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["a.txt", *args])
    assert exc.value.code == 2
