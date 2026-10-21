# KV-Lint

Linter for Valve KeyValues (KV1) files. Every file gets syntax, duplicate key and whitespace checks, file sets can add:

- `phrases`: `*.phrases.txt` (placeholders, printf specifiers, color tags, language codes). `style`:
  - `sourcemod` (default): checked against `en`
  - `mm-utils`: checked against the phrase name, as mm-utils does at load
- `radiopanel`: per-language RadioPanel menus checked against `english.txt`

## Config

`kv-lint.toml`, globs relative to it:

```toml
languages = "translations/config.txt"
report = "missing-translations.txt"   # optional
json = "missing-translations.json"    # optional
markdown = "translation-summary.md"   # optional

[[files]]
glob = "translations/*.phrases.txt"
rules = "phrases"
style = "mm-utils"

[[files]]
glob = "menu/*.txt"
rules = "radiopanel"

[[files]]
glob = ["configs/*.cfg", "gamedata/*.txt"]

[checks]                              # optional severity overrides
trailing-whitespace = "off"
missing-menu-file = "warning"
```

## Checks

Issues carry a check id (`[WARNING] trailing-whitespace: ...`).
Severity (`error`, `warning`, `info`, `off`) comes from `[checks]`/`--check ID=SEVERITY`, else the rule set, else [checks.py](src/kv_lint/checks.py). Only errors fail; `info` isn't annotated on GitHub.

## Usage

```yaml
- uses: actions/checkout@v7
- uses: jvnipers/kv-lint@v1
  with:
    output: kv-lint-results.txt # optional, only written when there are issues
```

Without a `kv-lint.toml`, pass the file sets as inputs (space-separated globs):

```yaml
- uses: jvnipers/kv-lint@v1
  with:
    phrases: translations/*.phrases.txt
    phrases-style: sourcemod
    languages: translations/config.txt
    files: translations/config.txt configs/*.cfg
```

```bash
uvx --from git+https://github.com/jvnipers/kv-lint kv-lint              # reads kv-lint.toml
uvx --from git+https://github.com/jvnipers/kv-lint kv-lint "cfg/*.txt"  # generic checks only
uvx --from git+https://github.com/jvnipers/kv-lint kv-lint --phrases "translations/*.phrases.txt" \
    --languages translations/config.txt "configs/*.cfg"                 # no kv-lint.toml
```

Inline options (`--phrases`, `--phrases-style`, `--radiopanel`, `--languages`, `--report`, `--json`, `--markdown`) can't be combined with `--config`. `--check` (action: `checks`) works with both.

## Ignoring

Check ids are optional (none ignores all), `--` starts a reason.

```json
// lint-ignore-start
...
"en"    "ignored"
...
// lint-ignore-end

// lint-ignore-next duplicate-key
"en"    "only duplicate-key ignored"

"en"    "ignored"  // lint-ignore unknown-tag, trailing-whitespace -- legacy tags
```
