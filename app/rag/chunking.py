"""Markdown → section chunks.

Each `##` section becomes one chunk (prefixed with the document title so the
embedding carries document context). Sections longer than MAX_CHARS are split on
paragraph boundaries into part 1/2/… chunks. The chunk id is stable across
rebuilds (doc slug + section slug + part), which is what test golden sets key on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from pathlib import Path

MAX_CHARS = 1800
DOC_ID_RE = re.compile(r"\*\*Document ID:\*\*\s*([A-Z0-9-]+)")


@dataclass
class Chunk:
    id: str
    doc_id: str          # e.g. NDPL-HR-001
    doc_title: str       # e.g. Nimbus Dynamics — Leave Policy (NDPL-HR-001)
    source_file: str     # e.g. 01-leave-policy.md
    section: str         # e.g. 3. Casual Leave (CL)
    part: int
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:60]


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_CHARS:
        return [text]
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        candidate = (buf + "\n\n" + para).strip() if buf else para
        if len(candidate) > MAX_CHARS and buf:
            parts.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        parts.append(buf)
    return parts


def chunk_markdown(path: Path) -> list[Chunk]:
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), path.stem)
    m = DOC_ID_RE.search(raw)
    doc_id = m.group(1) if m else path.stem.upper()

    sections: list[tuple[str, list[str]]] = []
    current_name, current_lines = "Preamble", []
    for line in lines:
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            if current_lines and "".join(current_lines).strip():
                sections.append((current_name, current_lines))
            current_name, current_lines = line[3:].strip(), []
        else:
            current_lines.append(line)
    if current_lines and "".join(current_lines).strip():
        sections.append((current_name, current_lines))

    chunks: list[Chunk] = []
    for name, body_lines in sections:
        body = "\n".join(body_lines).strip()
        for i, part in enumerate(_split_long(body), start=1):
            header = f"{title}\nSection: {name}\n\n"
            chunks.append(Chunk(
                id=f"{path.stem}#{_slug(name)}" + (f"#p{i}" if i > 1 else ""),
                doc_id=doc_id,
                doc_title=title,
                source_file=path.name,
                section=name,
                part=i,
                text=header + part,
            ))
    return chunks


def chunk_directory(directory: Path) -> list[Chunk]:
    out: list[Chunk] = []
    for md in sorted(directory.glob("*.md")):
        out.extend(chunk_markdown(md))
    return out
