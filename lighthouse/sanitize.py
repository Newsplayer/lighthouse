"""Allow-list HTML sanitizer (stdlib only) for storing a safe copy of each posting."""

from __future__ import annotations

from html import escape
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
