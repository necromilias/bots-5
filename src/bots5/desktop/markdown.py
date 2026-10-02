"""Markdown rendering pipeline for B.O.T.S. Phase 11 M2.

This module provides a rich rendering pipeline that converts Markdown
to Qt-compatible HTML with syntax highlighting via Pygments.

Key properties:
- Raw Markdown stays the CANONICAL stored text; rendered presentation is DERIVED state only.
- Never mutate or re-serialise stored message text.
- Raw HTML is DISABLED (no raw HTML execution) - it is escaped.
- Links must not auto-execute anything dangerous; opening external links must be explicit.
- Images whose source resolves to a LOCAL attachment are rendered via SAFE path resolver.
- Remote image URLs are DEFERRED (never fetched).
"""

from __future__ import annotations

import re
import pathlib
from typing import Callable

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererProtocol
from markdown_it.token import Token

from pygments import highlight
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.formatters import HtmlFormatter

from bots5.infrastructure.data_root_authority import DataRootAuthority


# =============================================================================
# Safe attachment path resolver
# =============================================================================


class SafeAttachmentResolver:
    """Resolves attachment references safely, blocking path traversal and remote URLs.
    
    This resolver enforces strict security:
    - Rejects remote http(s) URLs - produces no image (deferred per R-16)
    - Only allows file:// URLs for local attachments
    - Rejects absolute paths that might be unsafe
    """
    
    def __init__(self, authority: DataRootAuthority | None = None) -> None:
        self._authority = authority
    
    def resolve(self, reference: str) -> str | None:
        """Resolve an image reference to a local path or None if unsafe/remote.
        
        Args:
            reference: The image reference (file path or http(s) URL)
            
        Returns:
            A path string suitable for Qt's image loading, or None if the
            reference should be deferred (remote URL) or is unsafe.
        """
        # Remote URLs are deferred per R-16: never fetch the network.
        if _is_remote_url(reference):
            return None

        # Normalise a file:// reference into a filesystem path so that it is
        # subjected to exactly the same containment check as any other
        # reference.  A file:// URL must NEVER be passed through to Qt
        # unchecked: that would allow arbitrary local file reads.
        candidate_text = reference
        if candidate_text.startswith("file://"):
            candidate_text = candidate_text[len("file://") :]

        # Absolute paths are never trusted; attachments are addressed relative
        # to the attachment objects root.
        if candidate_text.startswith("/"):
            return None

        # Reject traversal before touching the filesystem.
        if ".." in pathlib.PurePosixPath(candidate_text).parts:
            return None

        root = self._attachment_objects_root()
        if root is None:
            return None
        try:
            candidate = (root / candidate_text).resolve()
            # Must remain inside the attachment objects root after resolution
            # (this also defeats symlink escapes).
            candidate.relative_to(root.resolve())
        except (ValueError, OSError):
            return None
        if not candidate.is_file():
            return None
        return str(candidate)

    def _attachment_objects_root(self) -> pathlib.Path | None:
        """Return the attachment objects root, or None when unknown.

        The renderer must never guess at authority internals: if no root can be
        established, image rendering is deferred rather than risking an
        unbounded local read.
        """
        authority = self._authority
        if authority is None:
            return None
        # DataRootAuthority exposes `root` (its logical/diagnostic root).  The
        # renderer reads it but always re-validates containment, so it never
        # grants access outside the attachment objects tree.
        root = getattr(authority, "root", None)
        if root is None:
            root = getattr(authority, "_data_root", None)
        if root is None:
            return None
        return pathlib.Path(root) / "attachments" / "objects"


def _is_remote_url(reference: str) -> bool:
    """Check if a reference is a remote http(s) URL."""
    return reference.startswith(("http://", "https://"))


# =============================================================================
# Custom renderer with Pygments highlighting for code blocks
# =============================================================================


class PygmentsRendererMixin:
    """Mixin that adds Pygments syntax highlighting to fenced code blocks."""
    
    def fence(self, tokens: list[Token], idx: int, options: dict, env: dict) -> str:
        """Render a fenced code block with syntax highlighting."""
        token = tokens[idx]
        lang = token.info.strip() if token.info else ""
        code = token.content
        
        # Try to get the lexer for the specified language
        lexer = None
        if lang:
            try:
                lexer = get_lexer_by_name(lang, stripall=True)
            except Exception:
                # Language not recognized, try to guess
                pass
        
        if lexer is None:
            try:
                lexer = guess_lexer(code)
            except Exception:
                # Fallback to plain text
                lexer = None
        
        if lexer is None:
            # Plain text, just escape and wrap
            formatted_code = self._escape_html(code)
            formatter = HtmlFormatter(noclasses=True, wrapline=True)
        else:
            # Use Pygments for syntax highlighting
            formatter = HtmlFormatter(noclasses=True, wrapline=True)
            formatted_code = highlight(code, lexer, formatter)
        
        # Extract just the code part from the highlighting
        # Pygments adds <div class="highlight"><pre>...</pre></div>
        # We want just the <span> colored content
        # Remove the wrapper div and pre tags
        formatted_code = re.sub(r'^<div class="highlight"><pre>', '', formatted_code)
        formatted_code = re.sub(r'</pre></div>$', '', formatted_code)
        
        return (
            f"<div class='code-block'>"
            f"{formatted_code}"
            f"</div>"
        )
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )


class CustomRenderer(PygmentsRendererMixin, RendererProtocol):
    """Custom renderer that uses Pygments for code highlighting."""
    
    def __init__(self, attachment_resolver: SafeAttachmentResolver | None = None):
        self._attachment_resolver = attachment_resolver
    
    def render(self, tokens: list[Token], options: dict, env: dict) -> str:
        """Render a list of tokens to HTML."""
        output = []
        i = 0
        while i < len(tokens):
            token = tokens[i]
            
            if token.type == "inline":
                output.append(self.render_inline(tokens, i, options, env))
            elif token.type == "fence":
                output.append(self.fence(tokens, i, options, env))
            elif token.type == "table_open":
                output.append("<table class='markdown-table'>")
                # Render thead/tbody content
                while i < len(tokens):
                    t = tokens[i]
                    if t.type == "thead_open":
                        output.append("<thead>")
                    elif t.type == "thead_close":
                        output.append("</thead>")
                    elif t.type == "tbody_open":
                        output.append("<tbody>")
                    elif t.type == "tbody_close":
                        output.append("</tbody>")
                    elif t.type == "tr_open":
                        output.append("<tr>")
                    elif t.type == "tr_close":
                        output.append("</tr>")
                    elif t.type == "th_open":
                        output.append("<th>")
                    elif t.type == "th_close":
                        output.append("</th>")
                    elif t.type == "td_open":
                        output.append("<td>")
                    elif t.type == "td_close":
                        output.append("</td>")
                    elif t.type == "inline":
                        # Render the inline content
                        content = self._render_inline_children(t.children or [], options, env)
                        output.append(content)
                    elif t.type == "table_close":
                        output.append("</table>")
                        i += 1
                        break
                    i += 1
            elif token.type == "blockquote_open":
                output.append("<blockquote>")
            elif token.type == "blockquote_close":
                output.append("</blockquote>")
            elif token.type == "bullet_list_open":
                output.append("<ul>")
            elif token.type == "bullet_list_close":
                output.append("</ul>")
            elif token.type == "ordered_list_open":
                output.append("<ol>")
            elif token.type == "ordered_list_close":
                output.append("</ol>")
            elif token.type == "list_item_open":
                output.append("<li>")
            elif token.type == "list_item_close":
                output.append("</li>")
            elif token.type == "paragraph_open":
                output.append("<p>")
            elif token.type == "paragraph_close":
                output.append("</p>")
            elif token.type == "hardbreak":
                output.append("<br/>")
            elif token.type == "softbreak":
                output.append(" ")
            elif token.type == "text":
                output.append(self._escape_html(token.content))
            elif token.type == "em_open":
                output.append(f"<span class='emphasis'>")
            elif token.type == "em_close":
                output.append("</span>")
            elif token.type == "strong_open":
                output.append(f"<span class='strong'>")
            elif token.type == "strong_close":
                # Was "</strong>", which closed the <span> above with a different tag and
                # produced unbalanced markup.
                output.append("</span>")
            elif token.type == "s_open":
                output.append("<span class='strikethrough'>")
            elif token.type == "s_close":
                output.append("</span>")
            elif token.type == "code_inline":
                escaped = self._escape_html(token.content)
                output.append(f"<code class='inline-code'>{escaped}</code>")
            elif token.type == "link_open":
                output.append(self.render_link_open(tokens, i, options, env))
            elif token.type == "link_close":
                output.append("</a>")
            elif token.type == "image":
                output.append(self.render_image(tokens, i, options, env))
            elif token.type == "html_inline" or token.type == "html_block":
                # Raw HTML - escape it, don't execute
                output.append(self._escape_html(token.content))
            elif token.type == "heading_open":
                # Open heading with the correct level from token.tag
                level = token.tag[1:]  # '1', '2', etc.
                output.append(f"<h{level}>")
            elif token.type == "heading_close":
                # Close with the correct level
                level = token.tag[1:]  # '1', '2', etc.
                output.append(f"</h{level}>")
            
            i += 1
        
        return "".join(output)
    
    def render_inline(self, tokens: list[Token], idx: int, options: dict, env: dict) -> str:
        """Render inline content."""
        # This is a simplified inline renderer
        # The actual rendering is done by the inner tokens
        if idx < len(tokens) and tokens[idx].type == "inline":
            inner = tokens[idx]
            output = []
            i = 0
            while i < len(inner.children or []):
                child = inner.children[i]
                if child.type == "text":
                    output.append(self._escape_html(child.content))
                elif child.type == "code_inline":
                    escaped = self._escape_html(child.content)
                    output.append(f"<code class='inline-code'>{escaped}</code>")
                elif child.type == "em_open":
                    output.append(f"<span class='emphasis'>")
                elif child.type == "em_close":
                    output.append("</span>")
                elif child.type == "strong_open":
                    output.append(f"<span class='strong'>")
                elif child.type == "strong_close":
                    output.append("</span>")
                elif child.type == "s_open":
                    output.append("<span class='strikethrough'>")
                elif child.type == "s_close":
                    output.append("</span>")
                elif child.type == "link_open":
                    output.append(self.render_link_open(inner.children, i, options, env))
                elif child.type == "link_close":
                    output.append("</a>")
                elif child.type == "image":
                    output.append(self.render_image(inner.children, i, options, env))
                elif child.type == "html_inline":
                    output.append(self._escape_html(child.content))
                i += 1
            return "".join(output)
        return ""
    
    def render_table(self, tokens: list[Token], idx: int, options: dict, env: dict) -> str:
        """Render a Markdown table as HTML table."""
        token = tokens[idx]
        result = ["<table class='markdown-table'>"]
        
        # Find the thead and tbody sections
        thead_content = []
        tbody_rows = []
        current_row = []
        is_header_row = True
        
        if token.children:
            for child in token.children:
                if child.type == "thead_open":
                    is_header_row = True
                elif child.type == "thead_close":
                    if current_row:
                        thead_content.append(current_row)
                        current_row = []
                    is_header_row = False
                elif child.type == "tbody_open":
                    pass
                elif child.type == "tbody_close":
                    if current_row:
                        tbody_rows.append(current_row)
                        current_row = []
                elif child.type == "tr_open":
                    current_row = []
                elif child.type == "tr_close":
                    if current_row:
                        if is_header_row:
                            thead_content.append(current_row)
                        else:
                            tbody_rows.append(current_row)
                        current_row = []
                elif child.type == "th_open":
                    pass
                elif child.type == "th_close":
                    pass
                elif child.type == "td_open":
                    pass
                elif child.type == "td_close":
                    pass
                elif child.type == "inline" and child.children:
                    # This is the cell content
                    cell_content = self._render_inline_children(child.children, options, env)
                    current_row.append(cell_content)
        
        # Render header
        if thead_content:
            result.append("<thead>")
            for row in thead_content:
                result.append("<tr>")
                for cell_content in row:
                    result.append(f"<th>{cell_content}</th>")
                result.append("</tr>")
            result.append("</thead>")
        
        # Render body
        if tbody_rows:
            result.append("<tbody>")
            for row in tbody_rows:
                result.append("<tr>")
                for cell_content in row:
                    result.append(f"<td>{cell_content}</td>")
                result.append("</tr>")
            result.append("</tbody>")
        
        result.append("</table>")
        return "".join(result)
    
    def _render_inline_children(self, children: list[Token], options: dict, env: dict) -> str:
        """Render inline token children to HTML."""
        output = []
        for child in children:
            if child.type == "text":
                output.append(self._escape_html(child.content))
            elif child.type == "code_inline":
                escaped = self._escape_html(child.content)
                output.append(f"<code class='inline-code'>{escaped}</code>")
            elif child.type == "em_open":
                output.append("<span class='emphasis'>")
            elif child.type == "em_close":
                output.append("</span>")
            elif child.type == "strong_open":
                output.append("<span class='strong'>")
            elif child.type == "strong_close":
                output.append("</span>")
            elif child.type == "s_open":
                output.append("<span class='strikethrough'>")
            elif child.type == "s_close":
                output.append("</span>")
            elif child.type == "html_inline":
                output.append(self._escape_html(child.content))
            elif child.type == "link_open":
                output.append(self._render_link_open_inline(child))
            elif child.type == "link_close":
                output.append("</a>")
            elif child.type == "image":
                output.append(self._render_image_inline(child))
        return "".join(output)
    
    def _render_link_open_inline(self, token: Token) -> str:
        """Render link open for inline content."""
        href = token.attrs.get("href", "") if token.attrs else ""
        if href.startswith("mailto:"):
            escaped_href = self._escape_html(href)
            return f'<a href="{escaped_href}" class="link">'
        # NOTE: this must be an <a> element, not a <span>.  Every link_close path emits
        # "</a>", so a <span> here produced unbalanced markup (opened <span>, closed </a>).
        # An <a> with no href is not clickable, so the deferral semantics are unchanged.
        return '<a class="link" style="color: #3b9ddd; text-decoration: underline;">'
    
    def _render_image_inline(self, token: Token) -> str:
        """Render image for inline content."""
        src = token.attrs.get("src", "") if token.attrs else ""
        if self._attachment_resolver:
            resolved = self._attachment_resolver.resolve(src)
            if resolved:
                escaped_src = self._escape_html(resolved)
                alt = token.attrs.get("alt", "") if token.attrs else ""
                escaped_alt = self._escape_html(alt)
                return f'<img src="{escaped_src}" alt="{escaped_alt}"/>'
        return ""
    
    def render_link_open(self, tokens: list[Token], idx: int, options: dict, env: dict) -> str:
        """Render an opening link tag with safe URL handling."""
        token = tokens[idx]
        href = token.attrs.get("href", "") if token.attrs else ""
        
        # For safety, we don't auto-open links
        # Just render as plain text with a span class for later handling
        if href.startswith("mailto:"):
            # Mailto is relatively safe
            escaped_href = self._escape_html(href)
            return f'<a href="{escaped_href}" class="link">'
        
        # For http(s) links, render without href to prevent auto-execution
        # The actual URL is visible in the text
        # NOTE: this must be an <a> element, not a <span>.  Every link_close path emits
        # "</a>", so a <span> here produced unbalanced markup (opened <span>, closed </a>).
        # An <a> with no href is not clickable, so the deferral semantics are unchanged.
        return '<a class="link" style="color: #3b9ddd; text-decoration: underline;">'
    
    def render_image(self, tokens: list[Token], idx: int, options: dict, env: dict) -> str:
        """Render an image, resolving through safe path resolver."""
        token = tokens[idx]
        src = token.attrs.get("src", "") if token.attrs else ""
        
        if self._attachment_resolver:
            resolved = self._attachment_resolver.resolve(src)
            if resolved:
                # Local attachment - render with safe path
                escaped_src = self._escape_html(resolved)
                alt = token.attrs.get("alt", "") if token.attrs else ""
                escaped_alt = self._escape_html(alt)
                return f'<img src="{escaped_src}" alt="{escaped_alt}"/>'
            # else: remote URL or unsafe - defer (render nothing or placeholder)
        
        # Defer remote/unresolvable images - render nothing
        return ""
    
    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )


# =============================================================================
# Markdown renderer with custom Pygments renderer
# =============================================================================


class MarkdownRenderer:
    """Renders Markdown to Qt-compatible HTML with syntax highlighting.
    
    Features:
    - Fenced code blocks with Pygments syntax highlighting
    - Tables with proper HTML table structure
    - Blockquotes, lists, emphasis, etc.
    - Raw HTML is escaped (not executed)
    - Images are resolved through the safe attachment resolver
    - Links are rendered but don't auto-execute
    """
    
    def __init__(self, attachment_resolver: SafeAttachmentResolver | None = None) -> None:
        """Initialize the renderer.
        
        Args:
            attachment_resolver: Optional resolver for local attachment images.
                               If provided, image references are resolved safely.
                               If None, all images are deferred.
        """
        self._attachment_resolver = attachment_resolver
        
        # Create markdown-it parser with commonmark preset
        self._parser = MarkdownIt("commonmark")
        
        # Enable fence (code blocks) - this is enabled by default in commonmark
        self._parser.enable("fence")
        
        # Enable table support
        self._parser.enable("table")
        
        # Enable strikethrough.  The design's M2 scope names inline formatting as
        # "bold/italic/strikethrough/inline code/links", but the commonmark preset does
        # not include the strikethrough rule, so "~~text~~" was rendering literally.
        self._parser.enable("strikethrough")
        
        # Enable linkify for auto-linking URLs
        self._parser.enable("linkify")
        
        # Use custom renderer with Pygments highlighting
        self._parser.renderer = CustomRenderer(attachment_resolver=attachment_resolver)
    
    def render(self, markdown: str) -> str:
        """Render Markdown to HTML.
        
        Args:
            markdown: The raw Markdown text (canonical form)
            
        Returns:
            HTML string suitable for Qt rich text display.
            The raw Markdown is preserved unchanged.
        """
        # Parse the markdown
        tokens = self._parser.parse(markdown)
        
        # Render to HTML using the custom renderer attached to the parser
        return self._parser.renderer.render(tokens, self._parser.options, {})
    
    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters to prevent XSS."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )


# =============================================================================
# Module-level convenience functions
# =============================================================================


def create_renderer(attachment_resolver: SafeAttachmentResolver | None = None) -> MarkdownRenderer:
    """Create a new Markdown renderer.
    
    Args:
        attachment_resolver: Optional resolver for local attachment images.
                           If provided, image references are resolved safely.
                           If None, all images are deferred.
    
    Returns:
        A configured MarkdownRenderer instance.
    """
    return MarkdownRenderer(attachment_resolver=attachment_resolver)


def render_markdown(markdown: str, attachment_resolver: SafeAttachmentResolver | None = None) -> str:
    """Convenience function to render Markdown to HTML.
    
    Args:
        markdown: The raw Markdown text (canonical form)
        attachment_resolver: Optional resolver for local attachment images.
    
    Returns:
        HTML string suitable for Qt rich text display.
    """
    renderer = create_renderer(attachment_resolver=attachment_resolver)
    return renderer.render(markdown)
