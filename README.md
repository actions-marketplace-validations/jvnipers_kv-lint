# KV-Lint

Linter for Valve KeyValues (KV1) files. Every file gets syntax, duplicate key and whitespace checks, file sets can add:

- `phrases`: Sourcemod style `*.phrases.txt` checked against `en` (placeholders, printf specifiers, language codes)
- `radiopanel`: per-language RadioPanel menus checked against `english.txt`

## Config

`kv-lint.toml`, globs relative to it:

```toml
languages = "translations/config.txt"
report = "missing-translations.txt"   # optional
json = "missing-translations.json"    # optional

[[files]]
glob = "translations/*.phrases.txt"
rules = "phrases"

[[files]]
glob = "menu/*.txt"
rules = "radiopanel"

[[files]]
glob = ["configs/*.cfg", "gamedata/*.txt"]
```

## Usage

```yaml
- uses: actions/checkout@v7
- uses: jvnipers/kv-lint@v1
```

```bash
uvx --from git+https://github.com/jvnipers/kv-lint kv-lint              # reads kv-lint.toml
uvx --from git+https://github.com/jvnipers/kv-lint kv-lint "cfg/*.txt"  # generic checks only
```

## Ignoring

```json
// lint-ignore-start
...
"en"    "ignored"
...
// lint-ignore-end

// lint-ignore-next
"en"    "ignored"

"en"    "ignored"  // lint-ignore
```
