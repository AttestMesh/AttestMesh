"""Matrix message formatting helpers."""

from __future__ import annotations

import html
import re

_LIST_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def matrix_text_content(body: str) -> dict[str, str]:
    return {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": markdown_to_matrix_html(body),
    }


def markdown_to_matrix_html(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []
    i = 0

    def flush_para() -> None:
        if not para:
            return
        out.append("<p>" + "<br />".join(_inline(line) for line in para) + "</p>")
        para.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_para()
            i += 1
            continue
        if stripped.startswith("```"):
            flush_para()
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(block)) + "</code></pre>")
            continue
        if _is_table_start(lines, i):
            flush_para()
            rows: list[list[str]] = [_split_table_row(lines[i])]
            i += 2
            while i < len(lines) and _looks_like_table_row(lines[i]):
                rows.append(_split_table_row(lines[i]))
                i += 1
            out.append(_table(rows))
            continue
        list_match = _LIST_RE.match(line)
        if list_match:
            flush_para()
            items: list[str] = []
            while i < len(lines):
                match = _LIST_RE.match(lines[i])
                if not match:
                    break
                items.append(match.group(1))
                i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in items) + "</ul>")
            continue
        para.append(line)
        i += 1

    flush_para()
    return "\n".join(out)


def _inline(text: str) -> str:
    parts = re.split(r"(`[^`]*`)", text)
    rendered: list[str] = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            rendered.append("<code>" + html.escape(part[1:-1]) + "</code>")
            continue
        escaped = html.escape(part)
        escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
        rendered.append(escaped)
    return "".join(rendered)


def _is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and _looks_like_table_row(lines[index])
        and bool(_TABLE_SEP_RE.match(lines[index + 1]))
    )


def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and len(_split_table_row(stripped)) >= 2


def _split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _table(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = rows[0]
    body = rows[1:]
    html_rows = ["<table><thead><tr>"]
    html_rows.extend(f"<th>{_inline(cell)}</th>" for cell in header)
    html_rows.append("</tr></thead>")
    if body:
        html_rows.append("<tbody>")
        for row in body:
            padded = row + [""] * max(0, len(header) - len(row))
            html_rows.append("<tr>")
            html_rows.extend(f"<td>{_inline(cell)}</td>" for cell in padded[: len(header)])
            html_rows.append("</tr>")
        html_rows.append("</tbody>")
    html_rows.append("</table>")
    return "".join(html_rows)
