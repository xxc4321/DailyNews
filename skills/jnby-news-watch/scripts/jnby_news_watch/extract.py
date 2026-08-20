from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import re
from typing import Iterable


INSTRUCTION_PATTERN = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous|system\s*prompt|print\s+(?:every|all)\s+secret|send\s*credentials|change\s+the\s+ranking\s+rules)",
    re.IGNORECASE,
)
CONTROL_PATTERN = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def read_bounded(chunks: Iterable[bytes], *, max_bytes: int) -> bytes:
    buffer = bytearray()
    for chunk in chunks:
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise ValueError("response exceeds configured byte limit")
    return bytes(buffer)


@dataclass(frozen=True)
class ExtractedArticle:
    title: str
    text: str
    canonical_url: str
    language: str
    published_at: datetime | None
    author: str
    security_flags: tuple[str, ...]


class _ArticleParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.visible_parts: list[str] = []
        self.in_title = False
        self.skip_tags: list[str] = []
        self.canonical = ""
        self.language = ""
        self.published = ""
        self.author = ""
        self.flags: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_name = tag.lower()
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag_name == "html":
            self.language = values.get("lang", "").split("-", 1)[0].lower()
        hidden = (
            tag_name in {"script", "style", "noscript", "template", "svg"}
            or "hidden" in values
            or values.get("aria-hidden", "").lower() == "true"
            or "display:none" in values.get("style", "").replace(" ", "").lower()
            or "visibility:hidden" in values.get("style", "").replace(" ", "").lower()
        )
        if (hidden or self.skip_tags) and tag_name not in {
            "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"
        }:
            self.skip_tags.append(tag_name)
        if tag_name == "title" and not self.skip_tags:
            self.in_title = True
        if tag_name == "link" and values.get("rel", "").lower() == "canonical":
            self.canonical = values.get("href", "").strip()
        if tag_name == "meta":
            key = (values.get("property") or values.get("name")).lower()
            content = values.get("content", "").strip()
            if key in {"article:published_time", "date", "datepublished"}:
                self.published = content
            elif key in {"author", "article:author"}:
                self.author = content

    def handle_endtag(self, tag: str) -> None:
        tag_name = tag.lower()
        if tag_name == "title":
            self.in_title = False
        if self.skip_tags and tag_name in self.skip_tags:
            reverse_index = self.skip_tags[::-1].index(tag_name)
            del self.skip_tags[len(self.skip_tags) - reverse_index - 1 :]

    def handle_data(self, data: str) -> None:
        if self.skip_tags:
            if INSTRUCTION_PATTERN.search(data):
                self.flags.add("hidden_instruction")
            return
        cleaned = CONTROL_PATTERN.sub("", data)
        if self.in_title:
            self.title_parts.append(cleaned)
        if cleaned.strip():
            self.visible_parts.append(cleaned)

    def handle_comment(self, data: str) -> None:
        if INSTRUCTION_PATTERN.search(data):
            self.flags.add("hidden_instruction")


def _charset(headers: dict[str, str]) -> str:
    content_type = headers.get("content-type", "")
    match = re.search(r"charset=([\w-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else "utf-8"


def _collapse(parts: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def extract_article(
    payload: bytes, headers: dict[str, str], source_url: str
) -> ExtractedArticle:
    content_type = headers.get("content-type", "").lower()
    if "html" not in content_type and content_type:
        raise ValueError("unsupported article content type")
    text = payload.decode(_charset(headers), errors="replace")
    parser = _ArticleParser()
    parser.feed(text)
    return ExtractedArticle(
        title=_collapse(parser.title_parts),
        text=_collapse(parser.visible_parts),
        canonical_url=parser.canonical or source_url,
        language=parser.language or "und",
        published_at=_parse_datetime(parser.published),
        author=parser.author,
        security_flags=tuple(sorted(parser.flags)),
    )
