import os
import re
import textwrap
from pathlib import Path

import pytest

from kv_lint import kv
from kv_lint.cli import github_annotation, lint, main
from kv_lint.config import Config, ConfigError, FileSet, load_config
from kv_lint.linter import relative_name
from kv_lint.rules.translations import format_args

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
    return sorted((i.file, i.line, i.severity, i.check) for i in issues)


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
    assert summary(issues) == [("cfg/a.cfg", 5, "warning", "duplicate-key")]


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


def test_unicode_line_separator_keeps_line_numbers(tmp_path):
    write(tmp_path, {"a.txt": '"Root"\n{\n\t"a"\t"x y"\n\t"b"\t"2" \n}\n'})
    issues = run(tmp_path, FileSet(["*.txt"]))
    assert [(i.line, i.check) for i in issues] == [(4, "trailing-whitespace")]


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

    text = CLEAN.replace("{green}Hello", "{olive}Hello")
    assert phrases(tmp_path, text) == []


def test_csgo_lightgreen_is_a_color_not_a_placeholder(tmp_path):
    assert phrases(tmp_path, CLEAN.replace("{green}Hello", "{lightgreen}Hello")) == []


def test_color_tag_case(tmp_path):
    issues = phrases(tmp_path, CLEAN.replace("{green}Hallo", "{Green}Hallo"))
    assert summary(issues) == [
        ("translations/plugin.phrases.txt", 7, "warning", "color-tag-case")
    ]


def test_double_brace_is_a_literal_brace(tmp_path):
    text = CLEAN.replace("{green}Hello", "{{KZ} {green}Hello").replace(
        "{green}Hallo", "{{ {green}Hallo"
    )
    assert phrases(tmp_path, text) == []


def test_missing_placeholder(tmp_path):
    issues = phrases(tmp_path, CLEAN.replace("Hallo {name}", "Hallo"))
    assert summary(issues) == [
        ("translations/plugin.phrases.txt", 7, "error", "missing-placeholder")
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
    assert [(i.check, i.line) for i in issues] == (
        [] if ok else [("format-mismatch", 6)]
    )


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
    assert {(i.severity, i.check, i.line) for i in issues} == {
        ("error", "duplicate-key", 6),
        ("error", "unknown-language", 7),
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
        ("translations/plugin.phrases.txt", 3, "error", "phrase-structure"),
        ("translations/plugin.phrases.txt", 7, "error", "phrase-structure"),
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
    assert {(i.file, i.check) for i in issues} == {
        ("menu/german.txt", "menu-mismatch"),
        ("menu/german.txt", "unknown-menu-key"),
        ("menu/", "missing-menu-file"),
    }


def test_radiopanel_missing_english(tmp_path):
    issues = menu(tmp_path, {"menu/german.txt": MENU})
    assert summary(issues) == [("menu/", 0, "error", "missing-menu-reference")]


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
        ('[files]\nglob = "a"\n', "must be [[files]] tables"),
        ('files = "a"\n', "must be [[files]] tables"),
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
        "::error file=translations/plugin.phrases.txt,line=7,title=missing-placeholder::Missing placeholder(s)"
    )


def test_annotation_escapes_and_skips_non_file(tmp_path):
    [issue] = menu(tmp_path, {"menu/german.txt": MENU})
    assert github_annotation(issue, tmp_path / issue.file) == (
        "::error title=missing-menu-reference::Missing english.txt - this is the reference file for menu translations"
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
    assert "cfg/b.cfg:2: [ERROR] syntax: Unclosed opening brace" in (
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
            "configs/core.cfg": '"Root"\n{\n\t"k"\t"C:\\dir\\"\n}\n',
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


def test_missing_menu_language_file_is_info_without_annotation(
    tmp_path, monkeypatch, capsys
):
    languages = LANGUAGES.replace('"german"\t"de"', '"german"\t"de"\n"dutch"\t"nl"')
    files = {
        "translations/config.txt": languages,
        "menu/english.txt": MENU,
        "menu/german.txt": MENU,
    }
    [issue] = menu(tmp_path, files)
    assert (issue.file, issue.severity) == ("menu/", "info")
    assert str(issue).startswith(
        "menu/:0: [INFO] missing-menu-file: Missing menu translation file: dutch.txt"
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    capsys.readouterr()
    assert (
        main(
            [
                "--radiopanel",
                "menu/*.txt",
                "--languages",
                "translations/config.txt",
                "--output",
                "out.txt",
            ]
        )
        == 0
    )
    assert "::" not in capsys.readouterr().out
    assert "[INFO] missing-menu-file: Missing menu translation file: dutch.txt" in (
        tmp_path / "out.txt"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "text, args",
    [
        ("%s has %d", "sd"),
        ("100%% done", ""),
        ("100% of them", "o"),
        ("%5.2f and %-08ld", "fld"),
        ("%*d", "*d"),
        ("50%", "?"),
        ("100% win", "?"),
    ],
)
def test_format_args_matches_mm_utils(text, args):
    assert format_args(text) == args


def mm_phrases(root, text):
    write(
        root, {"translations/config.txt": LANGUAGES, "translations/p.phrases.txt": text}
    )
    return run(
        root, FileSet(["translations/*.phrases.txt"], "phrases", {"style": "mm-utils"})
    )


def test_mm_utils_args_checked_against_phrase_name_including_en(tmp_path):
    issues = mm_phrases(
        tmp_path,
        """\
    "Phrases"
    {
    	"%s banned %s"
    	{
    		"en"	"{green}%s{default} banned %s"
    		"de"	"%s hat %s zu 100% gebannt"
    	}
    	"Took %d seconds"
    	{
    		"en"	"Took %.1f seconds"
    	}
    }
    """,
    )
    assert [(i.line, i.check) for i in issues] == [
        (6, "format-mismatch"),
        (10, "format-mismatch"),
    ]


def test_mm_utils_colors_and_name_placeholders(tmp_path):
    issues = mm_phrases(
        tmp_path,
        """\
    "Phrases"
    {
    	"Page {n}"
    	{
    		"en"	"{olive}Page {n}"
    		"de"	"{lightgreen}Seite"
    	}
    }
    """,
    )
    assert sorted((i.line, i.check, i.message) for i in issues) == [
        (
            6,
            "missing-placeholder",
            "Missing placeholder(s) {n} in 'de' translation for 'Page {n}' (present in the phrase name)",
        ),
        (6, "unknown-tag", "Unknown color tag: {lightgreen}"),
    ]


@pytest.mark.parametrize(
    "text, message",
    [
        ('[[files]]\nglob = "a"\nstyle = "mm-utils"\n', "unknown key(s) style"),
        (
            '[[files]]\nglob = "a"\nrules = "phrases"\nstyle = "nope"\n',
            "unknown style 'nope'",
        ),
    ],
)
def test_config_style_errors(tmp_path, text, message):
    path = tmp_path / "kv-lint.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(path)


def test_cli_phrases_style_and_markdown(tmp_path, monkeypatch):
    # "de" matches "en", but neither matches the phrase name: only mm-utils cares
    write(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES,
            "translations/p.phrases.txt": '"Phrases"\n{\n\t"Hi %s"\n\t{\n\t\t"en"\t"Hi %d"\n\t\t"de"\t"Hallo %d"\n\t}\n}\n',
        },
    )
    monkeypatch.chdir(tmp_path)
    args = [
        "--phrases",
        "translations/*.phrases.txt",
        "--languages",
        "translations/config.txt",
        "--markdown",
        "summary.md",
    ]
    assert main(args) == 0
    assert main([*args, "--phrases-style", "mm-utils"]) == 1

    markdown = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert "| **Linter Errors** | 2 |" in markdown
    assert "| **Total Phrases** | 1 |" in markdown
    assert "Menu Files" not in markdown
    assert "  de (german)          [████████████████████] 100.0% (1/1)" in markdown


def test_cli_phrases_style_needs_phrases(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--phrases-style", "mm-utils", "cfg/*.cfg"])
    assert exc.value.code == 2


def test_mm_utils_language_keys_ignore_case_and_last_repeat_wins(tmp_path):
    issues = mm_phrases(
        tmp_path,
        """\
    "Phrases"
    {
    	"%s left"
    	{
    		"en"	"%s left"
    		"EN"	"%d left"
    		"DE"	"%s ging"
    	}
    }
    """,
    )
    assert [(i.line, i.check) for i in issues] == [
        (6, "duplicate-key"),
        (6, "format-mismatch"),
    ]


def test_mm_utils_phrases_share_one_table_and_skip_language_folders(tmp_path):
    yes = '"Phrases"\n{\n\t"Yes"\n\t{\n\t\t"en"\t"Yes"\n\t}\n}\n'
    write(
        tmp_path,
        {
            "translations/config.txt": LANGUAGES,
            "translations/a.phrases.txt": yes,
            "translations/b.phrases.txt": yes.replace(
                '"Yes"\n\t}', '"Yes"\n\t\t"de"\t"Ja"\n\t}'
            ),
            "translations/de/a.phrases.txt": yes,
                
            
        },
    )
    issues = run(
        tmp_path,
        FileSet(["translations/**/*.phrases.txt"], "phrases", {"style": "mm-utils"}),
    )
    assert summary(issues) == [
        ("translations/b.phrases.txt", 5, "error", "duplicate-key"),
        ("translations/de/a.phrases.txt", 0, "error", "phrase-structure"),
    ]


def test_menu_file_for_any_name_of_a_language(tmp_path):
    languages = LANGUAGES.replace(
        '"german"\t"de"', '"german"\t"de"\n"koreana"\t"ko"\n"korean"\t"ko"'
    )
    files = {"translations/config.txt": languages, "menu/english.txt": MENU}
    files |= {"menu/german.txt": MENU, "menu/koreana.txt": MENU}
    assert menu(tmp_path, files) == []


def test_missing_languages_file_is_an_error(tmp_path):
    write(tmp_path, {"translations/p.phrases.txt": CLEAN})
    issues = run(
        tmp_path,
        FileSet(["translations/*.phrases.txt"], "phrases"),
        languages="translations/nope.txt",
    )
    assert summary(issues) == [("translations/nope.txt", 0, "error", "unreadable-file")]


def test_glob_matching_nothing_is_an_error(tmp_path):
    write(tmp_path, {"cfg/a.cfg": '"Root"\n{\n\t"k"\t"v"\n}\n'})
    issues = run(tmp_path, FileSet(["cfg/*.cfg", "cfg/*.txt"]))
    assert summary(issues) == [("cfg/*.txt", 0, "error", "no-matching-files")]


def test_cli_output_removed_on_clean_run(tmp_path, monkeypatch):
    write(tmp_path, {"cfg/a.cfg": '"Root"\n{\n\t"k"\t"v"\n}\n', "out.txt": "old"})
    monkeypatch.chdir(tmp_path)
    assert main(["cfg/a.cfg", "--output", "out.txt"]) == 0
    assert not (tmp_path / "out.txt").exists()


    write(
        tmp_path,
        {"translations/config.txt": LANGUAGES, "translations/p.phrases.txt": CLEAN},
    )
    monkeypatch.chdir(tmp_path)
    args = [
        "--phrases",
        "translations/*.phrases.txt",
        "--languages",
        "translations/config.txt",
        "--report",
        "missing/dir/r.txt",
    ]
    assert main(args) == 2


def test_relative_name_falls_back_across_drives(monkeypatch):
    def relpath(path, start=None):
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", relpath)
    assert relative_name(Path("/x/a.txt")) == "/x/a.txt"


def test_unclosed_section_ends_on_last_line():
    nodes, _ = kv.parse('"Root"\n{\n\t"a"\t"1"\n')
    assert nodes[0].end_line == 3

