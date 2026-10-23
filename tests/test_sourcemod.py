import json

import pytest

from kv_lint.cli import main
from kv_lint.config import FileSet
from test_linter import run, summary, write

LANGUAGES_CFG = (
    '"Languages"\n{\n\t"en"\t\t"English"\t// English\n\t"de"\t\t"German"\n}\n'
)
BASE = """\
"Phrases"
{
	"Votes"
	{
		"#format"	"{1:d},{2:d}"
		"en"		"{1} of {2} votes"
	}
	"Yes"
	{
		"en"		"Yes"
	}
}
"""


def sourcemod(root, name, german, base=BASE):
    write(
        root,
        {
            "configs/languages.cfg": LANGUAGES_CFG,
            f"translations/{name}": base,
            f"translations/de/{name}": german,
        },
    )
    return run(
        root,
        FileSet(["translations/**/*.txt"], "phrases"),
        languages="configs/languages.cfg",
    )


# a.* sorts before translations/de/, z.* after, so both merge orders are covered
@pytest.mark.parametrize("name", ["a.phrases.txt", "z.phrases.txt"])
def test_language_folder_merges_into_base_file(tmp_path, name):
    german = '"Phrases"\n{\n\t"Votes"\n\t{\n\t\t"de"\t"{1} Stimmen"\n\t}\n\t"Yes"\n\t{\n\t\t"de"\t"Ja"\n\t}\n\t"Gone"\n\t{\n\t\t"de"\t"Weg"\n\t}\n}\n'
    assert summary(sourcemod(tmp_path, name, german)) == [
        (f"translations/de/{name}", 5, "error", "missing-placeholder"),
        (f"translations/de/{name}", 11, "error", "missing-english"),
    ]


def test_language_folder_coverage_and_duplicates(tmp_path, monkeypatch):
    german = (
        '"Phrases"\n{\n\t"Yes"\n\t{\n\t\t"de"\t"Ja"\n\t\t"en"\t"Yes again"\n\t}\n}\n'
    )
    issues = sourcemod(tmp_path, "p.phrases.txt", german)
    assert summary(issues) == [
        ("translations/de/p.phrases.txt", 6, "error", "duplicate-key")
    ]

    monkeypatch.chdir(tmp_path)
    args = [
        "--phrases",
        "translations/**/*.txt",
        "--languages",
        "configs/languages.cfg",
        "--json",
        "out.json",
    ]
    main(args)
    report = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert list(report["files"]) == ["translations/p.phrases.txt"]
    assert report["languages"]["de"]["translated"] == 1


def test_sourcemod_languages_cfg_maps_codes_to_names(tmp_path, monkeypatch):
    write(
        tmp_path,
        {
            "configs/languages.cfg": LANGUAGES_CFG,
            "translations/p.phrases.txt": '"Phrases"\n{\n\t"Yes"\n\t{\n\t\t"en"\t"Yes"\n\t\t"de"\t"Ja"\n\t\t"xx"\t"?"\n\t}\n}\n',
        },
    )
    issues = run(
        tmp_path,
        FileSet(["translations/*.txt"], "phrases"),
        languages="configs/languages.cfg",
    )
    assert [(i.check, i.message) for i in issues] == [
        (
            "unknown-language",
            "Unknown language code 'xx' in phrase 'Yes' (not in languages.cfg)",
        )
    ]

    monkeypatch.chdir(tmp_path)
    main(
        [
            "--phrases",
            "translations/*.txt",
            "--languages",
            "configs/languages.cfg",
            "--report",
            "r.txt",
        ]
    )
    assert "  de (german)" in (tmp_path / "r.txt").read_text(encoding="utf-8")


def test_unmatched_brace(tmp_path):
    german = '"Phrases"\n{\n\t"Votes"\n\t{\n\t\t"de"\t"{1ç von {2} Stimmen"\n\t}\n}\n'
    assert summary(sourcemod(tmp_path, "p.phrases.txt", german)) == [
        ("translations/de/p.phrases.txt", 5, "error", "missing-placeholder"),
        ("translations/de/p.phrases.txt", 5, "warning", "unmatched-brace"),
    ]


def test_folder_named_like_a_language_needs_a_base_file(tmp_path):
    write(
        tmp_path,
        {
            "configs/languages.cfg": LANGUAGES_CFG,
            "translations/de/only.phrases.txt": '"Phrases"\n{\n\t"Yes"\n\t{\n\t\t"en"\t"Yes"\n\t}\n}\n',
        },
    )
    assert (
        run(
            tmp_path,
            FileSet(["translations/**/*.txt"], "phrases"),
            languages="configs/languages.cfg",
        )
        == []
    )


@pytest.mark.parametrize("name", ["a.phrases.txt", "z.phrases.txt"])
def test_language_folder_copy_wins_over_base(tmp_path, name):
    base = BASE.replace(
        '"{1} of {2} votes"', '"{1} of {2} votes"\n\t\t"de"\t"{1} von {2}"'
    )
    german = '"Phrases"\n{\n\t"Votes"\n\t{\n\t\t"de"\t"{1} Stimmen"\n\t}\n}\n'
    assert summary(sourcemod(tmp_path, name, german, base)) == [
        (f"translations/de/{name}", 5, "error", "duplicate-key"),
        (f"translations/de/{name}", 5, "error", "missing-placeholder"),
    ]
