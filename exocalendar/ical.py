"""RFC 5545 iCalendar parsing and serialization.

Losslessness contract: anything parsed serializes back byte-equivalent modulo
line folding and CRLF normalization — unknown properties, parameters, and
components are preserved verbatim and in order. Parameter values keep their
original quoting (stored raw, unquoted on access).
"""

from __future__ import annotations

from dataclasses import dataclass, field

FOLD_LIMIT = 75


# --- folding -----------------------------------------------------------------

def unfold(text: str) -> list[str]:
    """Split into logical lines, joining folded continuations."""
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")):
            if lines:
                lines[-1] += raw[1:]
            elif raw[1:]:
                lines.append(raw[1:])
        elif raw:
            lines.append(raw)
    return lines


def fold_line(line: str) -> str:
    """Fold a logical line at FOLD_LIMIT octets, never splitting a UTF-8 sequence."""
    encoded = line.encode()
    if len(encoded) <= FOLD_LIMIT:
        return line
    parts: list[str] = []
    limit = FOLD_LIMIT
    rest = line
    while rest:
        chunk = rest
        while len(chunk.encode()) > limit:
            chunk = chunk[:-1]
        parts.append(chunk)
        rest = rest[len(chunk):]
        limit = FOLD_LIMIT - 1  # continuation lines lose one octet to the leading space
    return "\r\n ".join(parts)


# --- text escaping -----------------------------------------------------------

def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt in "nN":
                out.append("\n")
            else:
                out.append(nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# --- content lines -----------------------------------------------------------

@dataclass
class ContentLine:
    """One logical line. `params` values are stored raw (quotes kept)."""

    name: str
    params: list[tuple[str, list[str]]] = field(default_factory=list)
    value: str = ""

    @classmethod
    def parse(cls, line: str) -> "ContentLine":
        name, params, rest = _parse_name_params(line)
        return cls(name=name.upper(), params=params, value=rest)

    def param(self, name: str) -> str | None:
        name = name.upper()
        for pname, values in self.params:
            if pname.upper() == name and values:
                return _unquote(values[0])
        return None

    def param_values(self, name: str) -> list[str]:
        name = name.upper()
        for pname, values in self.params:
            if pname.upper() == name:
                return [_unquote(v) for v in values]
        return []

    def set_param(self, name: str, value: str) -> None:
        quoted = _quote_if_needed(value)
        for i, (pname, _values) in enumerate(self.params):
            if pname.upper() == name.upper():
                self.params[i] = (pname, [quoted])
                return
        self.params.append((name.upper(), [quoted]))

    def serialize(self) -> str:
        parts = [self.name]
        for pname, values in self.params:
            parts.append(";" + pname + "=" + ",".join(values))
        return "".join(parts) + ":" + self.value


def _parse_name_params(line: str) -> tuple[str, list[tuple[str, list[str]]], str]:
    params: list[tuple[str, list[str]]] = []
    i = 0
    n = len(line)
    # property name
    while i < n and line[i] not in ";:":
        i += 1
    if i == n:
        raise ValueError(f"content line has no ':': {line[:80]!r}")
    name = line[:i]
    if not name:
        raise ValueError(f"content line has empty name: {line[:80]!r}")
    # parameters
    while i < n and line[i] == ";":
        i += 1
        j = i
        while j < n and line[j] != "=":
            if line[j] in ";:":
                raise ValueError(f"malformed parameter in: {line[:80]!r}")
            j += 1
        if j == n:
            raise ValueError(f"parameter without '=' in: {line[:80]!r}")
        pname = line[i:j]
        i = j + 1
        values: list[str] = []
        while True:
            if i < n and line[i] == '"':
                j = line.find('"', i + 1)
                if j == -1:
                    raise ValueError(f"unterminated quote in: {line[:80]!r}")
                values.append(line[i : j + 1])
                i = j + 1
            else:
                j = i
                while j < n and line[j] not in ",;:":
                    j += 1
                values.append(line[i:j])
                i = j
            if i < n and line[i] == ",":
                i += 1
                continue
            break
        params.append((pname, values))
    if i == n or line[i] != ":":
        raise ValueError(f"content line has no ':': {line[:80]!r}")
    return name, params, line[i + 1 :]


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def _quote_if_needed(value: str) -> str:
    if any(c in value for c in ':;,"'):
        return '"' + value.replace('"', "") + '"'
    return value


# --- components --------------------------------------------------------------

@dataclass
class Component:
    name: str
    lines: list[ContentLine] = field(default_factory=list)
    children: list["Component"] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> "Component":
        comps = parse_all(text)
        if len(comps) != 1:
            raise ValueError(f"expected one top-level component, got {len(comps)}")
        return comps[0]

    def get(self, name: str) -> ContentLine | None:
        name = name.upper()
        for line in self.lines:
            if line.name == name:
                return line
        return None

    def get_all(self, name: str) -> list[ContentLine]:
        name = name.upper()
        return [l for l in self.lines if l.name == name]

    def set(self, name: str, value: str, params: list[tuple[str, list[str]]] | None = None) -> ContentLine:
        existing = self.get(name)
        if existing is not None:
            existing.value = value
            if params is not None:
                existing.params = params
            return existing
        line = ContentLine(name=name.upper(), params=params or [], value=value)
        self.lines.append(line)
        return line

    def remove(self, name: str) -> None:
        name = name.upper()
        self.lines = [l for l in self.lines if l.name != name]

    def find_children(self, name: str) -> list["Component"]:
        name = name.upper()
        return [c for c in self.children if c.name == name]

    def serialize(self) -> str:
        return "".join(fold_line(l) + "\r\n" for l in self._logical_lines())

    def _logical_lines(self) -> list[str]:
        out = [f"BEGIN:{self.name}"]
        for line in self.lines:
            out.append(line.serialize())
        for child in self.children:
            out.extend(child._logical_lines())
        out.append(f"END:{self.name}")
        return out


def parse_all(text: str) -> list[Component]:
    """Parse text into a list of top-level components."""
    top: list[Component] = []
    stack: list[Component] = []
    for logical in unfold(text):
        cl = ContentLine.parse(logical)
        if cl.name == "BEGIN":
            comp = Component(name=cl.value.upper())
            if stack:
                stack[-1].children.append(comp)
            else:
                top.append(comp)
            stack.append(comp)
        elif cl.name == "END":
            if not stack:
                raise ValueError(f"END:{cl.value} without matching BEGIN")
            comp = stack.pop()
            if comp.name != cl.value.upper():
                raise ValueError(f"END:{cl.value} does not match BEGIN:{comp.name}")
        else:
            if not stack:
                raise ValueError(f"property outside any component: {logical[:80]!r}")
            stack[-1].lines.append(cl)
    if stack:
        raise ValueError(f"unterminated component: {stack[-1].name}")
    return top
