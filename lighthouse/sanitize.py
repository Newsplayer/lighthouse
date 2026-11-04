"""Allow-list HTML sanitizer (stdlib only) for storing a safe copy of each posting."""

from __future__ import annotations

from functools import lru_cache
import re
from html import escape, unescape
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "u",
    "h1", "h2", "h3", "h4", "h5", "h6", "a", "blockquote", "div", "span",
    "table", "thead", "tbody", "tr", "th", "td", "hr",
}
VOID_TAGS = {"br", "hr"}
DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT:
            self._skip += 1
            return
        if self._skip or tag not in ALLOWED_TAGS:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().startswith(("http://", "https://", "mailto:")):
                self.out.append(
                    f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">'
                )
            else:
                self.out.append("<a>")
            return
        self.out.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        if not self._skip and tag in VOID_TAGS:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT:
            self._skip = max(0, self._skip - 1)
            return
        if not self._skip and tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(escape(data, quote=False))


def sanitize_html(raw: str) -> str:
    parser = _Sanitizer()
    parser.feed(raw or "")
    parser.close()
    return "".join(parser.out)


_BLOCK = {"p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "blockquote", "ul", "ol"}


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK:
            self.out.append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


_TAG = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)[^>]*>")


def _tag(m: re.Match) -> str:
    name = m.group(2).lower()
    if name not in _BLOCK:
        return ""
    return "\n- " if (name == "li" and not m.group(1)) else "\n"


@lru_cache(maxsize=2048)
def html_to_text(html: str) -> str:
    """Plain text from (already sanitized) HTML, for search, similarity and extraction.

    Our stored HTML has been through sanitize_html, so every '<' is a real, simple tag
    (text '<' is escaped as &lt;). That lets one regex pass replace a full HTML parse:
    same output, about 10x faster. Cached: clustering and extraction convert the same
    descriptions back to back."""
    raw = unescape(_TAG.sub(_tag, html or ""))
    lines = (" ".join(line.split()) for line in raw.splitlines())
    text, blank = [], False
    for line in lines:
        if line:
            text.append(line)
            blank = False
        elif not blank and text:
            text.append("")
            blank = True
    return "\n".join(text).strip()


def html_to_text_slow(html: str) -> str:
    """Reference implementation (HTML parser); kept for tests."""
    parser = _Text()
    parser.feed(html or "")
    parser.close()
    lines = (" ".join(line.split()) for line in "".join(parser.out).splitlines())
    text, blank = [], False
    for line in lines:
        if line:
            text.append(line)
            blank = False
        elif not blank and text:
            text.append("")
            blank = True
    return "\n".join(text).strip()


# Exactly the tags sanitize_html emits. After removing them, a clean document has no '<' or '>' left
# (text is escaped), so one regex pass proves a description is safe without re-parsing it.
_SAFE_TAG = re.compile(
    r"</?(?:" + "|".join(sorted(ALLOWED_TAGS - {"a"})) + r")>"
    r'|<a href="(?:https?://|mailto:)[^"<>]*" target="_blank" rel="noopener noreferrer">|<a>|</a>')


def ensure_safe(html: str) -> str:
    """For descriptions that come from someone else's database (the shared job index): pass
    through if already in sanitized form, otherwise sanitize again."""
    html = html or ""
    rest = _SAFE_TAG.sub("", html)
    return html if "<" not in rest and ">" not in rest else sanitize_html(html)
