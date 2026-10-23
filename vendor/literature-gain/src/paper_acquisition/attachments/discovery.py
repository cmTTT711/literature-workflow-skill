"""Extract explicit supplementary links, never guess publisher download URLs."""

import re
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlsplit

MARKER = re.compile(
    r"supplement|supporting[\s_-]*(?:information|material|data)|additional[\s_-]*(?:file|material)"
    r"|electronic[\s_-]*annex|\bmmc\d+|moesm|\besm[_\d-]|\bsuppl\b",
    re.IGNORECASE,
)
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "wbr"}


class SupplementLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.anchor = None
        self.heading = None
        self.section_level = None
        self.section_depth = None
        self.links = []
        self.anchors = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = bool(MARKER.search(" ".join(attrs.get(k, "") for k in ("id", "class", "rel"))))
        if tag not in VOID:
            self.stack.append((tag, marker))
        if re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            if self.section_level is not None and level <= self.section_level:
                self.section_level = None
                self.section_depth = None
            self.heading = [level, ""]
        if tag == "a" and attrs.get("href"):
            self.anchor = {
                "url": attrs["href"],
                "label": attrs.get("title", ""),
                "marked": any(m for _, m in self.stack) or self.section_level is not None,
            }
        if tag == "meta" and MARKER.search(attrs.get("name", "")) and attrs.get("content"):
            value = attrs["content"]
            if value.startswith(("http://", "https://", "/")):
                self.links.append({"url": value, "label": attrs["name"]})
        if tag == "link" and marker and attrs.get("href"):
            self.links.append({"url": attrs["href"], "label": attrs.get("rel", "")})

    def handle_data(self, data):
        if self.anchor is not None:
            self.anchor["label"] += data
        if self.heading is not None:
            self.heading[1] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.anchor is not None:
            link = self.anchor
            self.anchors.append({"url": link["url"], "label": link["label"].strip()})
            if link["marked"] or MARKER.search(link["label"] + " " + link["url"]):
                self.links.append({"url": link["url"], "label": link["label"].strip()})
            self.anchor = None
        if re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            if MARKER.search(self.heading[1]):
                self.section_level = self.heading[0]
                self.section_depth = len(self.stack) - 1
            self.heading = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                if self.section_depth is not None and len(self.stack) < self.section_depth:
                    self.section_level = None
                    self.section_depth = None
                break


def extract_links(body, page_url):
    parser = SupplementLinks()
    parser.feed(body.decode("utf-8", errors="replace"))
    page = urlsplit(page_url)
    article = re.fullmatch(r"/article/(rs-\d+)/(v\d+)/?", page.path)
    if page.hostname in {"www.researchsquare.com", "researchsquare.com"} and article:
        # Research Square nests the heading inside a button. Accept observed asset links
        # only when their article id AND version match this page, never site-wide links.
        for link in parser.anchors:
            asset = urlsplit(urljoin(page_url, link["url"]))
            if asset.hostname in {
                "assets.researchsquare.com",
                "assets-eu.researchsquare.com",
            } and asset.path.startswith(f"/files/{article[1]}/{article[2]}/"):
                parser.links.append(link)
    links = {}
    for link in parser.links:
        if link["url"].startswith("#"):
            continue
        url = urldefrag(urljoin(page_url, link["url"]))[0]
        parsed = urlsplit(url)
        if re.search(r"/(?:rss|atom|feed)(?:\.xml)?/?$", parsed.path, re.IGNORECASE):
            continue
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            continue
        if url == urldefrag(page_url)[0]:
            continue
        links.setdefault(url, {**link, "url": url, "page_url": page_url})
    return list(links.values())
