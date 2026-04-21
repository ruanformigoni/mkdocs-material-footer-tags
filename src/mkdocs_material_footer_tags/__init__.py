"""mkdocs-material-footer-tags — inject a 'Related pages' footer into tagged pages.

Reads the same ``tags:`` YAML frontmatter that Material for MkDocs' built-in
tags plugin parses, ranks other pages by Jaccard similarity of their tag
sets, and injects the top N as a markdown list at the bottom of each tagged
page during the MkDocs build. Source ``.md`` files on disk are never
modified — the list is appended to the in-memory markdown stream that
mkdocs then renders to HTML.

Each entry shows:
  - the page's nav breadcrumb (plain ancestor-section names) followed by the
    bold linked page title,
  - a short summary truncated to a configurable character limit,
  - the page's tags rendered as Material-styled pills; tags shared with the
    current page get an extra ``md-tag--matched`` class for visual emphasis.

An inline ``<style>`` block is injected into every page's ``<head>`` so the
matched-tag highlight renders without the user having to add CSS manually.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set

import yaml
from mkdocs.config import config_options as c
from mkdocs.plugins import BasePlugin
from mkdocs.structure.files import Files
from mkdocs.utils import get_relative_url

log = logging.getLogger("mkdocs.plugins.mkdocs-material-footer-tags")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_H1_RE = re.compile(r"^# (.+?)\s*$", re.MULTILINE)
_TAG_INDEX_MARKER = "<!-- material/tags -->"

_MATCHED_TAG_CSS = (
    "<style>"
    ".md-typeset .md-tag.md-tag--matched{"
    "background-color:var(--md-accent-fg-color,#448aff);"
    "color:#fff}"
    "</style>"
)


class _PageEntry:
    """Per-page record captured during the ``on_files`` scan.

    Holds the pieces needed to render a related-page list item: where the
    page lives on disk (``src_path``) and in the docs tree (``src_uri``),
    its rendered URL, the title to display, the summary text, and the tag
    list.
    """

    __slots__ = ("src_path", "src_uri", "url", "title", "summary", "tags")

    def __init__(
        self,
        src_path: str,
        src_uri: str,
        url: str,
        title: str,
        summary: Optional[str],
        tags: List[str],
    ) -> None:
        self.src_path = src_path
        self.src_uri = src_uri
        self.url = url
        self.title = title
        self.summary = summary
        self.tags = tags


class MaterialFooterTagsConfig(c.Config):
    """Plugin configuration schema.

    Defaults are chosen to produce a readable footer without further tuning.
    All options are optional in the user's ``mkdocs.yml``.
    """

    # Maximum number of entries emitted in the Related pages list.
    max_pages = c.Type(int, default=5)
    # Lower bound on |A ∩ B|; pages sharing fewer tags with the current
    # page are excluded before Jaccard scoring runs.
    min_shared_tags = c.Type(int, default=1)
    # Heading text for the injected section.
    heading = c.Type(str, default="Related pages")
    # Markdown heading level (1-6).
    heading_level = c.Type(int, default=2)
    # Whether each entry carries a description after the title.
    show_summary = c.Type(bool, default=True)
    # Whether each entry carries the tag-pill row.
    show_tags = c.Type(bool, default=True)
    # Whether each entry carries the nav-path prefix before the title.
    show_breadcrumb = c.Type(bool, default=True)
    # Truncation threshold for summaries, in characters.
    summary_max_chars = c.Type(int, default=180)
    # String appended when a summary is truncated.
    summary_ellipsis = c.Type(str, default="...")


class MaterialFooterTagsPlugin(BasePlugin[MaterialFooterTagsConfig]):
    """Related-pages footer plugin.

    Hook sequence used during a MkDocs build:

    - ``on_files``: scan each ``.md`` for frontmatter tags and the
      tags-index marker; build the per-tag and per-page indexes.
    - ``on_nav``: walk the rendered nav tree to record each page's
      breadcrumb (parent section titles).
    - ``on_page_markdown``: for each tagged page, rank the other tagged
      pages by Jaccard similarity of tag sets and inject a
      ``Related pages`` section at the bottom of its markdown.
    - ``on_post_page``: inject the matched-tag CSS into the page ``<head>``.
    """

    def __init__(self) -> None:
        # Populated by on_files, cleared per build.
        self._pages_by_tag: Dict[str, List[_PageEntry]] = defaultdict(list)
        self._pages_by_src: Dict[str, _PageEntry] = {}
        # Rendered URL of the page containing the ``<!-- material/tags -->``
        # marker, if any. Used to build anchor URLs for tag pills.
        self._tag_index_url: Optional[str] = None
        # Populated by on_nav: src_path -> ancestor section titles.
        self._breadcrumbs: Dict[str, List[str]] = {}

    def on_files(self, files: Files, config) -> Files:
        """Scan every documentation page for frontmatter tags.

        Builds two indexes in one pass:

        - ``_pages_by_tag``: tag name -> list of pages carrying that tag.
        - ``_pages_by_src``: absolute source path -> page record.

        Also detects the first page containing the
        ``<!-- material/tags -->`` marker so tag pills can link to its
        rendered URL. Returns ``files`` unchanged; this hook is purely
        for side-effect indexing.
        """
        self._pages_by_tag.clear()
        self._pages_by_src.clear()
        self._tag_index_url = None
        for file in files.documentation_pages():
            src_path = file.abs_src_path
            if not src_path:
                continue
            try:
                text = Path(src_path).read_text(encoding="utf-8")
            except OSError as exc:
                log.warning("failed to read %s: %s", src_path, exc)
                continue
            if self._tag_index_url is None and _TAG_INDEX_MARKER in text:
                self._tag_index_url = file.url
            meta = self._parse_frontmatter(text)
            tags = meta.get("tags") or []
            if not isinstance(tags, list) or not tags:
                continue
            tags = [str(t) for t in tags]
            title = str(meta.get("title") or self._extract_h1(text) or file.src_uri)
            summary = self._extract_description(text, meta)
            entry = _PageEntry(
                src_path=src_path,
                src_uri=file.src_uri,
                url=file.url,
                title=title,
                summary=summary,
                tags=tags,
            )
            self._pages_by_src[src_path] = entry
            for tag in tags:
                self._pages_by_tag[tag].append(entry)
        log.info(
            "indexed %d tagged page(s) across %d tag(s); tag index %s",
            len(self._pages_by_src),
            len(self._pages_by_tag),
            self._tag_index_url if self._tag_index_url else "not found",
        )
        return files

    def on_nav(self, nav, config, files):
        """Capture the section hierarchy above each page.

        Walks the rendered nav tree and records, for each page, the list
        of ancestor section titles. That list is used as the breadcrumb
        prefix on related-page entries. Pages absent from nav get no
        breadcrumb.
        """
        self._breadcrumbs.clear()
        self._walk_nav(nav.items, [])
        return nav

    def _walk_nav(self, items, ancestors: List[str]) -> None:
        """Recursive helper for ``on_nav``.

        For each nav item that has children (a Section), recurse with its
        title appended to ``ancestors``. For each terminal page, record
        the current ``ancestors`` as the page's breadcrumb.
        """
        for item in items:
            if getattr(item, "children", None):
                title = getattr(item, "title", None)
                next_ancestors = ancestors + [title] if title else list(ancestors)
                self._walk_nav(item.children, next_ancestors)
                continue
            file = getattr(item, "file", None)
            if file and file.abs_src_path:
                self._breadcrumbs[file.abs_src_path] = list(ancestors)

    def on_page_markdown(self, markdown: str, page, config, files) -> str:
        """Inject the Related pages section into the current page's markdown.

        If the current page is tagged, scores every other tagged page by
        Jaccard similarity of tag sets, filters by ``min_shared_tags``,
        sorts by score descending then title ascending (deterministic),
        takes the top ``max_pages``, and appends a formatted markdown
        list under the configured heading.

        Pages without tags are untouched. The returned string becomes the
        input to the markdown-to-HTML renderer; the on-disk ``.md`` file
        is never modified.
        """
        src_path = page.file.abs_src_path
        current = self._pages_by_src.get(src_path)
        if current is None:
            return markdown

        current_tags: Set[str] = set(current.tags)
        scores: Dict[str, float] = {}
        for tag in current.tags:
            for other in self._pages_by_tag.get(tag, ()):
                if other.src_path == src_path or other.src_path in scores:
                    continue
                other_tags = set(other.tags)
                overlap = current_tags & other_tags
                if len(overlap) < self.config.min_shared_tags:
                    continue
                union = current_tags | other_tags
                if not union:
                    continue
                scores[other.src_path] = len(overlap) / len(union)

        if not scores:
            return markdown

        eligible = [(self._pages_by_src[p], s) for p, s in scores.items()]
        eligible.sort(key=lambda pair: (-pair[1], pair[0].title.lower()))
        eligible = eligible[: self.config.max_pages]

        hashes = "#" * max(1, self.config.heading_level)
        lines = ["", "", f"{hashes} {self.config.heading}", ""]
        from_dir = posixpath.dirname(page.file.src_uri) or "."
        for entry, _score in eligible:
            lines.append(self._render_entry(entry, from_dir, page.url, current_tags))
        return markdown + "\n".join(lines)

    def on_post_page(self, output: str, page, config) -> str:
        """Inject the matched-tag CSS into the page ``<head>``.

        Replaces the first ``</head>`` in the rendered HTML with the CSS
        block followed by ``</head>``. Runs on every page — the CSS is
        tiny and there is no benefit to conditionally injecting it only
        on pages that actually contain matched tags.
        """
        return output.replace("</head>", f"{_MATCHED_TAG_CSS}</head>", 1)

    def _render_entry(
        self,
        entry: _PageEntry,
        from_dir: str,
        page_url: str,
        current_tags: Set[str],
    ) -> str:
        """Render one related-page entry as a markdown list item.

        Emits, left-to-right: breadcrumb sections (plain text) +
        `` / `` + bold linked title; optional em-dash + truncated summary;
        ``<br>`` + tag pills. Matched tags appear first within the pill
        row.
        """
        rel = posixpath.relpath(entry.src_uri, from_dir)
        title_link = f"**[{entry.title}]({rel})**"
        breadcrumb = self._breadcrumbs.get(entry.src_path) if self.config.show_breadcrumb else None
        if breadcrumb:
            heading = " / ".join(breadcrumb) + " / " + title_link
        else:
            heading = title_link
        parts = [f"- {heading}"]
        if self.config.show_summary and entry.summary:
            summary = self._truncate(entry.summary)
            if summary:
                parts.append(f" — {summary}")
        if self.config.show_tags and entry.tags:
            parts.append(f"<br>{self._render_pills(entry.tags, page_url, current_tags)}")
        return "".join(parts)

    def _render_pills(
        self,
        tags: List[str],
        page_url: str,
        current_tags: Set[str],
    ) -> str:
        """Render the tag-pill row for one entry.

        Tags that overlap ``current_tags`` are emitted first (preserving
        their frontmatter declaration order within the group), followed
        by non-overlapping tags in their declaration order.

        Each pill is an ``<a>`` linking to the detected tags-index anchor
        when one is available, or a ``<span>`` otherwise. Shared tags
        carry the extra ``md-tag--matched`` class for CSS styling.
        """
        matched = [t for t in tags if t in current_tags]
        unmatched = [t for t in tags if t not in current_tags]
        chunks: List[str] = []
        for tag in matched + unmatched:
            classes = "md-tag md-tag--matched" if tag in current_tags else "md-tag"
            if self._tag_index_url:
                rel = get_relative_url(self._tag_index_url, page_url)
                chunks.append(f'<a href="{rel}#tag:{tag}" class="{classes}">{tag}</a>')
            else:
                chunks.append(f'<span class="{classes}">{tag}</span>')
        return " ".join(chunks)

    def _truncate(self, text: str) -> str:
        """Normalize and truncate a summary to the configured length.

        Whitespace is collapsed to single spaces; markdown link,
        inline-code, and bold/italic markers are stripped to their inner
        text. If the result exceeds ``summary_max_chars``, it is cut at
        the nearest space within the last half of the allowed range and
        the configured ellipsis is appended. Returns the
        possibly-truncated plain text.
        """
        text = re.sub(r"\s+", " ", text).strip()
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        text = re.sub(r"(\*\*|__|\*|_)", "", text)
        if not text:
            return ""
        max_chars = self.config.summary_max_chars
        if len(text) <= max_chars:
            return text
        ellipsis = self.config.summary_ellipsis
        cutoff = max_chars - len(ellipsis)
        if cutoff <= 0:
            return ellipsis
        snip = text[:cutoff].rstrip()
        last_space = snip.rfind(" ")
        if last_space > cutoff // 2:
            snip = snip[:last_space]
        return snip.rstrip() + ellipsis

    def _extract_description(self, text: str, meta: dict) -> Optional[str]:
        """Pick the best description for a page.

        A non-empty frontmatter ``description:`` field wins over
        heuristic body extraction. Falls back to the first body paragraph
        via ``_extract_first_paragraph``. Returns ``None`` if nothing
        usable is found.
        """
        desc = meta.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc
        return self._extract_first_paragraph(text)

    @staticmethod
    def _extract_first_paragraph(text: str) -> Optional[str]:
        """Best-effort first-paragraph extraction.

        Strips the YAML frontmatter and the leading H1, then walks the
        body line-by-line collecting a run of non-empty text lines
        terminated by a blank line. Lines beginning with admonition
        (``!!!``), code fence (backticks), blockquote (``>``), another
        heading (``#``), list bullet (``-``, ``*``, ``+``), HTML (``<``),
        or table (``|``) markers reset the collector — those structural
        lines are not plain prose. Returns ``None`` when the page has no
        prose paragraph in this sense.
        """
        body = _FRONTMATTER_RE.sub("", text, count=1)
        body = _H1_RE.sub("", body, count=1)
        collected: List[str] = []
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped:
                if collected:
                    break
                continue
            if stripped.startswith(("!!!", "```", ">", "#", "-", "*", "+", "<", "|")):
                collected = []
                continue
            collected.append(stripped)
        if not collected:
            return None
        return " ".join(collected)

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        """Parse the leading ``---``-delimited YAML block into a dict.

        A missing or malformed block yields an empty dict rather than an
        error — the caller treats the absence of frontmatter as "no
        tags, no description".
        """
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return {}
        try:
            data = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            return {}
        return data or {}

    @staticmethod
    def _extract_h1(text: str) -> Optional[str]:
        """Return the text of the first ``# `` heading in the document, if any."""
        m = _H1_RE.search(text)
        return m.group(1).strip() if m else None
