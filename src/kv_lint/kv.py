from dataclasses import dataclass
from typing import Optional


@dataclass
class Node:
    key: str
    line: int
    value: Optional[str] = None
    children: Optional[list["Node"]] = None
    end_line: int = 0
    condition: Optional[str] = None

    @property
    def is_section(self) -> bool:
        return self.children is not None


@dataclass
class _Token:
    kind: str  # "str", "{", "}" or "cond"
    text: str
    line: int


def _tokenize(text: str, escapes: bool, errors: list[tuple[int, str]]) -> list[_Token]:
    tokens = []
    i, line, n = 0, 1, len(text)

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            # Not Valve KV1, but SourceMod-style configs use them
            end = text.find("*/", i + 2)
            if end < 0:
                errors.append((line, "Unclosed block comment"))
                end = n
            else:
                end += 2
            line += text.count("\n", i, end)
            i = end
        elif c in "{}":
            tokens.append(_Token(c, c, line))
            i += 1
        elif c == '"':
            # Strings may not span lines; an unclosed one ends at the newline
            j = i + 1
            while j < n and text[j] not in '"\n':
                j += (
                    2
                    if escapes and text[j] == "\\" and j + 1 < n and text[j + 1] != "\n"
                    else 1
                )
            tokens.append(_Token("str", text[i + 1 : j], line))
            if j < n and text[j] == '"':
                i = j + 1
            else:
                errors.append((line, "Unclosed string"))
                i = j
        elif c == "[":
            j = i
            while j < n and text[j] not in "]\n":
                j += 1
            if j < n and text[j] == "]":
                j += 1
            tokens.append(_Token("cond", text[i:j], line))
            i = j
        else:
            j = i
            while (
                j < n
                and not text[j].isspace()
                and text[j] not in '"{}'
                and not text.startswith("//", j)
            ):
                j += 1
            tokens.append(_Token("str", text[i:j], line))
            i = j

    return tokens


def parse(text: str, escapes: bool = False) -> tuple[list[Node], list[tuple[int, str]]]:
    # Escapes are off by default: config values can be paths ending in a backslash
    errors: list[tuple[int, str]] = []
    tokens = _tokenize(text, escapes, errors)
    last_line = text.removesuffix("\n").count("\n") + 1

    root: list[Node] = []
    siblings = root
    # Open section, its children, line of its '{'
    stack: list[tuple[Node, list[Node], int]] = []

    def peek(index: int) -> Optional[_Token]:
        return tokens[index] if index < len(tokens) else None

    def open_section(node: Node, brace_line: int) -> list[Node]:
        children: list[Node] = []
        node.children = children
        siblings.append(node)
        stack.append((node, children, brace_line))
        return children

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1

        if tok.kind == "}":
            if not stack:
                errors.append((tok.line, "Unmatched closing brace '}'"))
                continue
            node, _, _ = stack.pop()
            node.end_line = tok.line
            siblings = stack[-1][1] if stack else root
            continue

        if tok.kind == "cond":
            errors.append((tok.line, f"Conditional '{tok.text}' without a key"))
            continue

        if tok.kind == "{":
            errors.append((tok.line, "Opening brace '{' without a key"))
            siblings = open_section(Node("", tok.line), tok.line)
            continue

        node = Node(tok.text, tok.line, end_line=tok.line)
        nxt = peek(i)
        if nxt and nxt.kind == "cond":
            node.condition = nxt.text
            i += 1
            nxt = peek(i)

        if nxt is None or nxt.kind in ("}", "cond"):
            errors.append((tok.line, f"Key '{tok.text}' has no value"))
            continue

        i += 1
        if nxt.kind == "{":
            siblings = open_section(node, nxt.line)
            continue

        node.value = nxt.text
        node.end_line = nxt.line
        after = peek(i)
        if after and after.kind == "cond":
            node.condition = after.text
            i += 1
        siblings.append(node)

    for node, _, brace_line in stack:
        errors.append((brace_line, "Unclosed opening brace '{'"))
        node.end_line = last_line

    errors.sort()
    return root, errors
