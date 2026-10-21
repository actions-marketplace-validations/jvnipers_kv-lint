import re
import textwrap

import pytest

from kv_lint import kv
from kv_lint.cli import github_annotation, lint, main
from kv_lint.config import Config, ConfigError, FileSet, load_config

LANGUAGES = """\
"Languages"
{
	"english"	"en"
	"german"	"de"
}
"""

CLEAN = """\
"Phrases"
{
	"Hello"
	{
		"#format"	"name:s"
		"en"	"{green}Hello {name}"
		"de"	"{green}Hallo {name}"
	}
}
"""

MENU = """\
"RadioPanel.txt"
{
	"Groups"
	{
		"menu_main"
		{
			"title"	"%s"
			"Commands"
			{
				"Save"
				{
					"hotkey"	"1"
					"label"	"%s"
					"cmd"	"kz_save"
				}
			}
		}
	}
}
"""


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")
    return root


def run(root, *filesets, languages="translations/config.txt"):
    linter, _ = lint(Config(root=root, files=list(filesets), languages=languages))
    return linter.issues


def summary(issues):
    return sorted((i.file, i.line, i.issue_type, i.category) for i in issues)


def phrases(root, text, name="translations/plugin.phrases.txt"):
    write(root, {"translations/config.txt": LANGUAGES, name: text})
    return run(root, FileSet(["translations/*.phrases.txt"], "phrases"))


def test_parse_tree_with_braces_on_key_line_unquoted_and_conditionals():
    nodes, errors = kv.parse(
        'Root {\n  key value [$WIN64]\n  "sub" { "a" "1" } // c\n}\n'
    )
    assert errors == []
    [root] = nodes
    assert (root.key, root.line, root.end_line) == ("Root", 1, 4)
    key, sub = root.children
    assert (key.key, key.value, key.condition) == ("key", "value", "[$WIN64]")
    assert (sub.key, sub.children[0].value) == ("sub", "1")


def test_parse_block_comment_keeps_line_numbers():
    nodes, errors = kv.parse('/* a\n b */\n"Root"\n{\n}\n')
    assert errors == [] and nodes[0].line == 3


@pytest.mark.parametrize(
    "escapes, value", [(True, 'say \\"hi\\"'), (False, "C:\\dir\\")]
)
def test_parse_escapes(escapes, value):
    nodes, errors = kv.parse(f'"Root"\n{{\n\t"k"\t"{value}"\n}}\n', escapes=escapes)
    assert errors == [] and nodes[0].children[0].value == value


@pytest.mark.parametrize(
    "text, expected",
    [
        ('"Root"\n{\n\t"k"\t"v\n}\n', [(3, "Unclosed string")]),
        ('"Root"\n{\n}\n}\n', [(4, "Unmatched closing brace '}'")]),
        ('"Root"\n{\n\t"a"\n\t{\n}\n', [(2, "Unclosed opening brace '{'")]),
        ('"Root"\n{\n\t"k"\n}\n', [(3, "Key 'k' has no value")]),
        ('/* open\n"Root" {}\n', [(1, "Unclosed block comment")]),
    ],
)
def test_parse_errors(text, expected):
    _, errors = kv.parse(text)
    assert errors == expected


def test_generic_duplicates_are_case_sensitive_warnings_and_skip_conditionals(
    tmp_path,
):
    write(
        tmp_path,
        {"cfg/a.cfg": """\
    "Root"
    {
    	"Key"	"1"
    	"key"	"2"
    	"key"	"3"
    	"os"	"a"	[$WIN64]
    	"os"	"b"	[$LINUX]
    }
    """},
    )
    issues = run(tmp_path, FileSet(["cfg/*.cfg"]))
    assert summary(issues) == [("cfg/a.cfg", 5, "warning", "Duplicate")]


def test_generic_whitespace_and_lint_ignore(tmp_path):
    write(
        tmp_path,
        {"a.txt": '"Root"\n{\n\t"a"\t"1" \n\n\n\t"b"\t"2"  // lint-ignore\n}\n'},
    )
    issues = run(tmp_path, FileSet(["*.txt"]))
    assert [(i.line, i.message) for i in issues] == [
        (3, "Line has trailing whitespace"),
        (5, "Multiple consecutive empty lines"),
    ]


def test_file_matched_twice_uses_first_entry(tmp_path):
    write(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES,
            "translations/p.phrases.txt": CLEAN.replace("Hallo {name}", "Hallo"),
        },
    )
    issues = run(
        tmp_path,
        FileSet(["translations/*.txt"]),
        FileSet(["translations/*.phrases.txt"], "phrases"),
    )
    assert issues == []


def test_clean_phrases(tmp_path):
    assert phrases(tmp_path, CLEAN) == []


def test_missing_placeholder(tmp_path):
    issues = phrases(tmp_path, CLEAN.replace("Hallo {name}", "Hallo"))
    assert summary(issues) == [
        ("translations/plugin.phrases.txt", 7, "error", "Placeholder")
    ]


@pytest.mark.parametrize(
    "de, ok",
    [
        ("%s hat %d Punkte (100%%)", True),
        ("%d Punkte für %s", False),
        ("%s hat Punkte", False),
    ],
)
def test_printf_specifiers_must_match_en(tmp_path, de, ok):
    issues = phrases(
        tmp_path,
        f"""\
    "Phrases"
    {{
    	"Score"
    	{{
    		"en"	"%s has %d points (100%%), 100% of them"
    		"de"	"{de}"
    	}}
    }}
    """,
    )
    assert [(i.category, i.line) for i in issues] == ([] if ok else [("Specifier", 6)])


def test_duplicate_language_is_error_and_unknown_language(tmp_path):
    issues = phrases(
        tmp_path,
        """\
    "Phrases"
    {
    	"Hello" // comment after the name
    	{
    		"en"	"Hello \\"you\\""
    		"en"	"Hello again"
    		"xx"	"???"
    	}
    }
    """,
    )
    assert {(i.issue_type, i.category, i.line) for i in issues} == {
        ("error", "Duplicate", 6),
        ("error", "Unknown", 7),
    }


def test_phrase_structure_errors(tmp_path):
    issues = phrases(
        tmp_path,
        """\
    "Phrases"
    {
    	"loose"	"pair"
    	"Hello"
    	{
    		"en"	"Hello"
    		"nested"
    		{
    		}
    	}
    }
    """,
    )
    assert summary(issues) == [
        ("translations/plugin.phrases.txt", 3, "error", "Format"),
        ("translations/plugin.phrases.txt", 7, "error", "Format"),
    ]


def test_lint_ignore_line(tmp_path):
    text = CLEAN.replace(
        '"de"	"{green}Hallo {name}"', '"de"	"{green}Hallo"	// lint-ignore'
    )
    assert phrases(tmp_path, text) == []


def menu(root, files):
    write(root, {"translations/config.txt": LANGUAGES, **files})
    return run(root, FileSet(["menu/*.txt"], "radiopanel"))


def test_radiopanel_complete(tmp_path):
    assert menu(tmp_path, {"menu/english.txt": MENU, "menu/german.txt": MENU}) == []


def test_radiopanel_mismatch_and_missing_language_file(tmp_path):
    german = MENU.replace('"cmd"	"kz_save"', '"cmd"	"kz_load"').replace(
        '"hotkey"	"1"', '"hotkey"	"1"\n"oops"	"x"'
    )
    issues = menu(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES.replace(
                '"german"	"de"', '"german"	"de"\n"dutch"	"nl"'
            ),
            "menu/english.txt": MENU,
            "menu/german.txt": german,
        },
    )
    assert {(i.file, i.category) for i in issues} == {
        ("menu/german.txt", "Mismatch"),
        ("menu/german.txt", "Unknown Key"),
        ("menu/", "Missing"),
    }


def test_radiopanel_missing_english(tmp_path):
    issues = menu(tmp_path, {"menu/german.txt": MENU})
    assert summary(issues) == [("menu/", 0, "error", "Missing")]


def test_load_config(tmp_path):
    path = tmp_path / "kv-lint.toml"
    path.write_text(
        textwrap.dedent("""\
    languages = "translations/config.txt"
    report = "missing.txt"

    [[files]]
    glob = "translations/*.phrases.txt"
    rules = "phrases"

    [[files]]
    glob = ["cfg/*.cfg", "gamedata/*.txt"]
    """),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.root == tmp_path.resolve()
    assert (config.languages, config.report, config.json) == (
        "translations/config.txt",
        "missing.txt",
        None,
    )
    assert config.files == [
        FileSet(["translations/*.phrases.txt"], "phrases"),
        FileSet(["cfg/*.cfg", "gamedata/*.txt"]),
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ('[[files]]\nglob = "a"\nrule = "phrases"\n', "unknown key(s) rule"),
        ('[[files]]\nglob = "a"\nrules = "nope"\n', "unknown rules 'nope'"),
        ('[[files]]\nrules = "phrases"\n', "needs 'glob'"),
        ('languages = "x"\n', "no [[files]] entries"),
    ],
)
def test_config_errors(tmp_path, text, message):
    path = tmp_path / "kv-lint.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(path)


def test_cli_with_config_writes_reports_and_exit_code(tmp_path, monkeypatch):
    write(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES,
            "translations/plugin.phrases.txt": CLEAN.replace("Hallo {name}", "Hallo"),
            "kv-lint.toml": """\
        languages = "translations/config.txt"
        json = "out/missing.json"

        [[files]]
        glob = "translations/*.phrases.txt"
        rules = "phrases"
        """,
        },
    )
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main([]) == 1
    assert (tmp_path / "out/missing.json").exists()


def test_cli_ad_hoc_globs(tmp_path, monkeypatch):
    write(
        tmp_path,
        {"cfg/a.cfg": '"Root"\n{\n\t"k"\t"v"\n}\n', "cfg/b.cfg": '"Root"\n{\n'},
    )
    monkeypatch.chdir(tmp_path)
    assert main(["cfg/a.cfg"]) == 0
    assert main(["cfg/*.cfg"]) == 1


def test_cli_missing_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_annotation_anchors_to_relative_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    [issue] = phrases(tmp_path, CLEAN.replace("Hallo {name}", "Hallo"))
    assert github_annotation(issue, tmp_path / issue.file).startswith(
        "::error file=translations/plugin.phrases.txt,line=7,title=Placeholder::Missing placeholder(s)"
    )


def test_annotation_escapes_and_skips_non_file(tmp_path):
    [issue] = menu(tmp_path, {"menu/german.txt": MENU})
    assert github_annotation(issue, tmp_path / issue.file) == (
        "::error title=Missing::Missing english.txt - this is the reference file for menu translations"
    )
    issue.context = "100%\nmore"
    assert github_annotation(issue, None).endswith("translations%0A100%25%0Amore")


def test_cli_output_only_written_when_there_are_issues(tmp_path, monkeypatch):
    write(
        tmp_path,
        {"cfg/a.cfg": '"Root"\n{\n\t"k"\t"v"\n}\n', "cfg/b.cfg": '"Root"\n{\n'},
    )
    monkeypatch.chdir(tmp_path)
    main(["cfg/a.cfg", "--output", "clean.txt"])
    main(["cfg/b.cfg", "--output", "results.txt"])
    assert not (tmp_path / "clean.txt").exists()
    assert "cfg/b.cfg:2: [ERROR] Syntax: Unclosed opening brace" in (
        tmp_path / "results.txt"
    ).read_text(encoding="utf-8")


def test_cli_inline_rules_without_config(tmp_path, monkeypatch):
    write(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES,
            "translations/plugin.phrases.txt": CLEAN.replace(
                "Hallo {name}", 'Hallo \\"{name}\\"'
            ),
            "configs/core.cfg": '"Root"\n{\n\t"k"\t"C:\dir\\"\n}\n',
        },
    )
    monkeypatch.chdir(tmp_path)
    args = [
        "--phrases",
        "translations/*.phrases.txt",
        "--languages",
        "translations/config.txt",
        "translations/*.txt",
        "configs/*.cfg",
        "--json",
        "missing.json",
    ]
    assert main(args) == 0
    assert (tmp_path / "missing.json").exists()

    (tmp_path / "translations/plugin.phrases.txt").write_text(
        CLEAN.replace("Hallo {name}", "Hallo %s"), encoding="utf-8"
    )
    assert main(args) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--config", "kv-lint.toml", "--phrases", "*.phrases.txt"],
        ["--config", "kv-lint.toml", "cfg/*.cfg"],
        ["--languages", "translations/config.txt"],
    ],
)
def test_cli_inline_argument_errors(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2
