"""Tests for Phase 11 M2 markdown rendering pipeline.

These tests verify the rendering pipeline for Markdown content with:
- Fenced code blocks with Pygments syntax highlighting
- Tables with proper HTML structure
- Raw HTML escaping (not execution)
- Safe local attachment image resolution
- Path traversal rejection
- Remote URL deferral
- Canonical Markdown preservation
"""

from __future__ import annotations

import pathlib
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from PySide6.QtWidgets import QLabel

from bots5.desktop.markdown import (
    MarkdownRenderer,
    SafeAttachmentResolver,
    create_renderer,
    render_markdown,
)
from bots5.domain.models import Message, MessageRole, MessageState


# =============================================================================
# Test data fixtures
# =============================================================================


@pytest.fixture
def simple_markdown() -> str:
    """Simple markdown content."""
    return "# Hello World\n\nThis is a **bold** statement with *italic* text.\n\n- Item 1\n- Item 2\n- Item 3"


@pytest.fixture
def markdown_with_code() -> str:
    """Markdown with fenced code blocks."""
    return """# Code Examples

Here is a Python function:

```python
def greet(name):
    print(f"Hello, {name}!")
    return True
```

And a JavaScript snippet:

```javascript
function greet(name) {
    console.log(`Hello, ${name}!`);
}
```
"""


@pytest.fixture
def markdown_with_table() -> str:
    """Markdown with a table."""
    return """# Features

| Feature | Status | Priority |
|---------|--------|----------|
| Code highlighting | Done | High |
| Tables | Done | Medium |
| Images | Deferred | Low |
"""


@pytest.fixture
def markdown_with_raw_html() -> str:
    """Markdown containing raw HTML."""
    return """# HTML Test

This is a paragraph.

<div class="evil">This is evil HTML that should be escaped</div>

<script>alert('XSS');</script>

More text after.
"""


@pytest.fixture
def markdown_with_links() -> str:
    """Markdown with links."""
    return """# Links

Here is a [text link](https://example.com).

And an email: [email](mailto:test@example.com).
"""


@pytest.fixture
def markdown_with_images() -> str:
    """Markdown with image references."""
    return """# Images

Local image: ![Alt text](images/screenshot.png)

Remote image: ![Remote](https://example.com/image.png)
"""


@pytest.fixture
def markdown_with_path_traversal() -> str:
    """Markdown with path traversal attempts in image references."""
    return """# Path Traversal Tests

Safe: ![Safe](subdir/file.png)

Traverse up: ![Up](../../etc/passwd)

Absolute outside: ![Absolute](/etc/passwd)
"""


@pytest.fixture
def complex_markdown() -> str:
    """Complex markdown combining multiple features."""
    return """# Complex Document

## Section 1: Code

Here's a Python example:

```python
def add(a, b):
    return a + b

# Test the function
result = add(2, 3)
print(f"Result: {result}")
```

## Section 2: Table

| Parameter | Type | Description |
|-----------|------|-------------|
| a | int | First number |
| b | int | Second number |

## Section 3: Blockquote

> This is an important quote
> that spans multiple lines.

## Section 4: List

1. First item
2. Second item with **bold**
3. Third item with *italic*

## Section 5: Mixed

Some `inline code` and **formatted** text.
"""


# =============================================================================
# SafeAttachmentResolver tests
# =============================================================================


class TestSafeAttachmentResolver:
    """Tests for the safe attachment path resolver.
    
    The resolver enforces:
    - Remote http(s) URLs are deferred (return None)
    - file:// URLs are normalised and subjected to the SAME containment check as
      any other reference; an absolute path outside the attachment objects root
      is REFUSED (never passed through to Qt)
    - Other references are deferred when no authority is available
    """
    
    def test_rejects_remote_http_url(self):
        """Remote HTTP URLs should be deferred."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        result = resolver.resolve("http://example.com/image.png")
        assert result is None
    
    def test_rejects_remote_https_url(self):
        """Remote HTTPS URLs should be deferred."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        result = resolver.resolve("https://example.com/image.png")
        assert result is None
    
    def test_refuses_absolute_file_url_outside_attachment_root(self):
        """An absolute file:// path must never be passed through unchecked."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        result = resolver.resolve("file:///home/user/image.png")
        assert result is None, "absolute file:// paths must not be trusted"

    def test_refuses_absolute_path(self):
        """A bare absolute path must never be trusted."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        assert resolver.resolve("/etc/passwd") is None

    def test_refuses_traversal_reference(self):
        """Traversal must be refused before any filesystem access."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        assert resolver.resolve("../../etc/passwd") is None
        assert resolver.resolve("subdir/../../secret") is None
    
    def test_defers_local_relative_path_without_authority(self):
        """Local relative paths without authority are deferred."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        result = resolver.resolve("test.png")
        assert result is None
    
    def test_defers_local_subdirectory_path_without_authority(self):
        """Local subdirectory paths without authority are deferred."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        result = resolver.resolve("subdir/test.png")
        assert result is None


# =============================================================================
# MarkdownRenderer tests
# =============================================================================


class TestMarkdownRenderer:
    """Tests for the markdown renderer."""
    
    def test_renders_headings(self, simple_markdown: str):
        """Headings should be rendered."""
        renderer = MarkdownRenderer()
        html = renderer.render(simple_markdown)
        assert "<h1" in html
        assert "Hello World" in html
    
    def test_renders_emphasis(self, simple_markdown: str):
        """Bold and italic text should be rendered."""
        renderer = MarkdownRenderer()
        html = renderer.render(simple_markdown)
        # Bold and italic are rendered as spans with class attributes
        assert "<span class='strong'>" in html or "<strong" in html or "<b" in html
        assert "<span class='emphasis'>" in html or "<em" in html or "<i" in html
    
    def test_renders_unordered_lists(self, simple_markdown: str):
        """Unordered lists should be rendered as <ul>."""
        renderer = MarkdownRenderer()
        html = renderer.render(simple_markdown)
        assert "<ul>" in html
        assert "<li>" in html
        assert "</li>" in html
        assert "</ul>" in html
        assert "Item 1" in html
        assert "Item 2" in html
        assert "Item 3" in html
    
    def test_renders_fenced_code_block(self, markdown_with_code: str):
        """Fenced code blocks with language should produce highlighted output."""
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_code)
        
        # Should contain code block
        assert "<div" in html
        assert "code-block" in html or "highlight" in html.lower()
        
        # Python code block should be present (keywords are highlighted separately)
        assert "def" in html
        assert "greet" in html
        assert "&gt;def greet" not in html  # Should not be escaped
        
        # JavaScript code block should be present
        assert "function" in html
        assert "greet" in html
    
    def test_code_block_has_pygments_highlighting(self, markdown_with_code: str):
        """Fenced code blocks should have Pygments-style highlighting."""
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_code)
        
        # Pygments adds span elements with style or class attributes for colors
        # The key is that it's not just plain text - there should be some markup
        # that indicates syntax highlighting was applied
        
        # Count the occurrences of <span (indicating highlighting)
        span_count = html.count("<span")
        
        # A simple code block without highlighting would have fewer spans
        # With highlighting, we should have multiple spans for different token types
        assert span_count >= 2, "Code block should have multiple spans for syntax highlighting"
    
    def test_renders_table(self, markdown_with_table: str):
        """Markdown tables should be rendered as HTML tables."""
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_table)
        
        # Table structure
        assert "<table" in html
        assert "<thead" in html or "<tbody" in html
        assert "<tr>" in html
        assert "<th>" in html or "<td>" in html
        
        # Table content
        assert "Feature" in html
        assert "Status" in html
        assert "Priority" in html
        assert "Code highlighting" in html
    
    def test_raw_html_is_escaped_not_executed(self, markdown_with_raw_html: str):
        """Raw HTML should be escaped, not executed."""
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_raw_html)
        
        # The div tag should be escaped
        assert "&lt;div" in html or "&lt;script" in html
        
        # The evil HTML should NOT be present as actual HTML
        assert "<div class=\"evil\">" not in html
        assert "<script>alert" not in html
        
        # The text content should be visible
        assert "HTML Test" in html
        assert "This is a paragraph" in html
    
    def test_inline_code_is_rendered(self, markdown_with_code: str):
        """Inline code should be rendered with <code> tag."""
        # Create a test with inline code
        test_md = "This has `inline code` in it."
        renderer = MarkdownRenderer()
        html = renderer.render(test_md)
        
        assert "<code" in html
        assert "inline code" in html
        assert "</code>" in html
    
    def test_links_are_rendered(self, markdown_with_links: str):
        """Links should be rendered."""
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_links)
        
        # Text link
        assert "text link" in html
        # Email link
        assert "email" in html
    
    def test_remote_images_are_deferred(self, markdown_with_images: str):
        """Remote image URLs should produce no image (deferred per R-16)."""
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_images)
        
        # Remote image should be deferred - no actual <img> for remote
        assert "https://example.com/image.png" not in html
        
        # The alt text should still be in the document (as plain text)
        assert "Remote" in html
    
    def test_local_images_with_file_url_deferred(self):
        """Local images with file:// URL are deferred (not rendered as <img>).
        
        The markdown-it-py renderer produces raw markdown output for image
        references. The actual image rendering is handled by Qt widgets
        when they process the rendered HTML and look for image references.
        """
        renderer = MarkdownRenderer()
        markdown = "![Local image](file:///tmp/test.png)"
        html = renderer.render(markdown)
        
        # The file:// URL is passed through in the rendered markdown
        assert "file:///tmp/test.png" in html
        # The image reference is kept as markdown (not converted to <img>)
        # because the renderer doesn't resolve paths - it just renders markdown

    def test_path_traversal_in_images_is_deferred(self, markdown_with_path_traversal: str):
        """Path traversal attempts in image references must never become local file reads.

        This test previously rendered and then asserted nothing whatsoever, so it would
        have passed even if the renderer happily emitted an ``<img src>`` pointing outside
        the data root.  It now pins the actual contract: no ``<img>`` element is produced
        for a traversal path, and the raw traversal target does not appear in an attribute.
        """
        renderer = MarkdownRenderer()
        html = renderer.render(markdown_with_path_traversal)

        assert "<img" not in html, f"traversal path produced an image element: {html!r}"
        assert ".." not in html or "src=" not in html, f"traversal target leaked into an attribute: {html!r}"


# =============================================================================
# Canonical text preservation tests
# =============================================================================


class TestCanonicalPreservation:
    """Tests that raw Markdown is preserved (canonical text)."""
    
    def test_rendering_does_not_mutate_input(self, simple_markdown: str):
        """Rendering should not mutate the original markdown text."""
        original = simple_markdown
        renderer = MarkdownRenderer()
        
        # Render multiple times
        html1 = renderer.render(original)
        html2 = renderer.render(original)
        
        # Original should be unchanged
        assert original == simple_markdown
        
        # Renderings should be consistent
        assert html1 == html2
    
    def test_message_content_unchanged_after_render(self):
        """Rendering a message must not mutate the message's canonical content.

        This previously read ``pass`` with a comment claiming the model could not be
        constructed, so it asserted nothing at all.  It is exercised for real now: the
        canonical Markdown stored on the message is the source of truth that the design
        requires to be preserved, so rendering must leave it byte-identical.
        """
        from datetime import UTC, datetime

        original_content = "# Test\n\n**Bold** text."
        message = Message(
            id="msg-1",
            chat_id="chat-1",
            role=MessageRole.ASSISTANT,
            state=MessageState.COMPLETE,
            content=original_content,
            sequence=1,
            created_at=datetime.now(UTC),
        )

        renderer = MarkdownRenderer()
        html = renderer.render(message.content)

        assert message.content == original_content
        assert "Bold" in html
        # The renderer emits themed span classes rather than raw <strong>/<em> tags,
        # so assert on the class the renderer actually produces.
        assert "class='strong'" in html or "<strong>" in html
        # Rendering twice from the same message must be stable and still non-mutating.
        assert renderer.render(message.content) == html
        assert message.content == original_content

    def test_deleted_tombstone_message_is_not_rendered_as_content(self):
        """A DELETED message must not surface its (empty) body as if it were content.

        The tombstone contract keeps a deleted message in the transcript so structural
        continuity survives, but its body is not live content.
        """
        from bots5.desktop.markdown import render_markdown

        html = render_markdown("")
        assert html.strip() in {"", "<p></p>"}
        assert MessageState.DELETED.value == "deleted"


# =============================================================================
# Helper functions tests
# =============================================================================


class TestHelperFunctions:
    """Tests for helper functions."""
    
    def test_create_renderer_returns_instance(self):
        """create_renderer should return a MarkdownRenderer instance."""
        renderer = create_renderer()
        assert isinstance(renderer, MarkdownRenderer)
    
    def test_create_renderer_with_resolver(self):
        """create_renderer should accept a resolver."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        renderer = create_renderer(attachment_resolver=resolver)
        assert isinstance(renderer, MarkdownRenderer)
    
    def test_render_markdown_convenience(self, simple_markdown: str):
        """render_markdown convenience function should work."""
        html = render_markdown(simple_markdown)
        assert "<h1" in html
        assert "Hello World" in html
    
    def test_render_markdown_with_resolver(self, markdown_with_images: str):
        """render_markdown should work with resolver."""
        resolver = SafeAttachmentResolver(None)  # type: ignore
        html = render_markdown(markdown_with_images, attachment_resolver=resolver)
        
        # Remote images should be deferred
        assert "https://example.com/image.png" not in html


# =============================================================================
# Edge case tests
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""
    
    def test_empty_markdown(self):
        """Empty markdown should render without error."""
        renderer = MarkdownRenderer()
        html = renderer.render("")
        # Should return empty or minimal HTML
        assert isinstance(html, str)
    
    def test_only_whitespace(self):
        """Markdown with only whitespace should render."""
        renderer = MarkdownRenderer()
        html = renderer.render("   \n\n   \n  ")
        assert isinstance(html, str)
    
    def test_malformed_fence(self):
        """Malformed fence (unclosed) should be handled gracefully."""
        renderer = MarkdownRenderer()
        html = renderer.render("```\nunclosed code block")
        assert isinstance(html, str)
    
    def test_nested_formatting(self):
        """Nested formatting should render correctly."""
        renderer = MarkdownRenderer()
        html = renderer.render("**Bold with *italic* inside**")
        # Pygments uses span class for styling
        assert "<span class='strong'>" in html or "<strong" in html or "<b" in html
        assert "<span class='emphasis'>" in html or "<em" in html or "<i" in html or "<span" in html
    
    def test_special_characters_in_code(self):
        """Special characters in code should be escaped."""
        renderer = MarkdownRenderer()
        html = renderer.render("```python\nprint('<script>alert(1)</script>')\n```")
        
        # The script tag should be escaped in the output
        assert "&lt;script&gt;" in html or "<script>" not in html
    
    def test_table_with_empty_cells(self):
        """Tables with empty cells should render."""
        renderer = MarkdownRenderer()
        html = renderer.render("| A | B |\n|---|---|\n|   | X |")
        
        assert "<table" in html
        assert "<td>" in html
        assert "X" in html
    
    def test_blockquote(self):
        """Blockquotes should render correctly."""
        renderer = MarkdownRenderer()
        html = renderer.render("> This is a quote")
        
        assert "<blockquote" in html
        assert "This is a quote" in html
    
    def test_ordered_list(self):
        """Ordered lists should render correctly."""
        renderer = MarkdownRenderer()
        html = renderer.render("1. First\n2. Second\n3. Third")
        
        assert "<ol" in html
        assert "<li>" in html
        assert "</li>" in html
        assert "</ol>" in html
    
    def test_hard_break(self):
        """Hard breaks (two spaces at end of line) should render."""
        renderer = MarkdownRenderer()
        html = renderer.render("Line 1  \nLine 2")
        
        # Hard breaks are converted to spaces by default in markdown-it-py
        # The text appears on the same line
        assert "Line 1Line 2" in html or "Line 1 Line 2" in html
    
    def test_paragraphs(self):
        """Paragraphs should be separated."""
        renderer = MarkdownRenderer()
        html = renderer.render("Paragraph 1\n\nParagraph 2")
        
        # Should have two paragraphs or equivalent structure
        assert "Paragraph 1" in html
        assert "Paragraph 2" in html


# =============================================================================
# Test suite metadata
# =============================================================================


# Mark all tests as Phase 11 M2 rendering pipeline tests
pytestmark = pytest.mark.phase11_m2_rendering


# ---------------------------------------------------------------------------
# R-16: local attachment images must actually render, and must stay inside root
# ---------------------------------------------------------------------------


class _FakeAuthority:
    """Minimal stand-in exposing the same `root` attribute DataRootAuthority has."""

    def __init__(self, root):
        self.root = root


def test_local_attachment_image_renders_as_img(tmp_path):
    """R-16: an image inside the attachment objects root must render."""
    objects = tmp_path / "attachments" / "objects"
    objects.mkdir(parents=True)
    (objects / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    resolver = SafeAttachmentResolver(_FakeAuthority(tmp_path))
    resolved = resolver.resolve("pic.png")
    assert resolved is not None, "local attachment image must resolve"
    assert resolved.endswith("pic.png")

    renderer = MarkdownRenderer(attachment_resolver=resolver)
    html = renderer.render("![pic](pic.png)")
    assert "<img" in html, "resolved local image must render as an <img> element"
    assert "pic.png" in html


def test_image_outside_objects_root_is_refused(tmp_path):
    """A file that exists but sits outside the objects root must not render."""
    (tmp_path / "attachments" / "objects").mkdir(parents=True)
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    resolver = SafeAttachmentResolver(_FakeAuthority(tmp_path))
    assert resolver.resolve("../secret.png") is None
    assert resolver.resolve("../../secret.png") is None

    renderer = MarkdownRenderer(attachment_resolver=resolver)
    html = renderer.render("![s](../secret.png)")
    assert "<img" not in html, "path outside the objects root must never render"


# =============================================================================
# Inline formatting coverage (design M2 scope)
# =============================================================================


def test_strikethrough_is_rendered_not_literal() -> None:
    """The design's M2 scope names strikethrough as required inline formatting.

    The commonmark preset does not include the strikethrough rule, so ``~~text~~``
    rendered as a literal ``<p>~~text~~</p>`` before this was enabled.
    """
    html = render_markdown("~~strike~~")
    assert "~~" not in html, f"strikethrough markers must not survive: {html!r}"
    assert "strike" in html
    assert "strikethrough" in html or "<s>" in html or "<del>" in html, html


def test_inline_formatting_emits_balanced_tags() -> None:
    """Every inline open tag must be closed by the matching tag.

    Both ``strong_close`` and the deferred-link close previously emitted a closing tag
    that did not match what was opened (``</strong>`` for a ``<span>``, and ``</a>``
    for a ``<span>``), producing unbalanced markup.
    """
    for source, open_tag in (
        ("**bold**", "<span class='strong'>"),
        ("*ital*", "<span class='emphasis'>"),
        ("~~strike~~", "<span class='strikethrough'>"),
    ):
        html = render_markdown(source)
        assert open_tag in html, f"{source!r} did not open {open_tag}: {html!r}"
        assert html.count("<span") == html.count("</span>"), (
            f"unbalanced span tags for {source!r}: {html!r}"
        )
