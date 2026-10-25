"""Formatting utilities for converting Markdown to Telegram-compliant HTML."""

from __future__ import annotations

import html
import re
from typing import List


def format_tables_to_pre(text: str) -> str:
    """Detects Markdown tables and wraps them in <pre> blocks so columns stay aligned."""
    lines = text.split("\n")
    new_lines: List[str] = []
    table_lines: List[str] = []
    in_table = False

    for line in lines:
        # A markdown table row typically starts and ends with | or contains |
        if re.match(r"^\s*\|.*\|\s*$", line):
            in_table = True
            table_lines.append(line)
        else:
            if in_table:
                new_lines.append("```\n" + "\n".join(table_lines) + "\n```")
                table_lines = []
                in_table = False
            new_lines.append(line)

    if in_table:
        new_lines.append("```\n" + "\n".join(table_lines) + "\n```")

    return "\n".join(new_lines)


def markdown_to_telegram_html(text: str) -> str:
    """Converts GitHub-Flavored Markdown into Telegram-compliant HTML.

    Telegram's HTML parser supports:
      <b>bold</b>, <strong>bold</strong>
      <i>italic</i>, <em>italic</em>
      <u>underline</u>, <ins>underline</ins>
      <s>strikethrough</s>, <strike>strikethrough</strike>, <del>strikethrough</del>
      <code>inline code</code>
      <pre>preformatted code</pre>
      <pre><code class="language-python">preformatted with language</code></pre>
      <a href="https://...">hyperlink</a>

    Unlike Telegram's legacy Markdown mode, HTML mode does not fail on underscores
    inside variable names (e.g. `guest_state`), asterisks, or unescaped periods.
    """
    if not text:
        return ""

    # 1. Format tables into code blocks so they keep monospaced alignment
    text = format_tables_to_pre(text)

    # 2. Extract and protect code blocks: ```lang\ncode\n```
    code_blocks = []

    def save_code_block(m: re.Match) -> str:
        code_blocks.append((m.group(1) or "", m.group(2)))
        return f"\x00CODEBLOCK_{len(code_blocks)-1}\x00"

    text = re.sub(r"```([a-zA-Z0-9_\-\+]*)\n?([\s\S]*?)```", save_code_block, text)

    # 3. Extract and protect inline code: `code`
    inline_codes = []

    def save_inline_code(m: re.Match) -> str:
        inline_codes.append(m.group(1))
        return f"\x00INLINECODE_{len(inline_codes)-1}\x00"

    text = re.sub(r"`([^`\n]+)`", save_inline_code, text)

    # 4. HTML escape remaining plain text (<, >, &)
    text = html.escape(text, quote=False)

    # 5. Markdown headers (# Header -> <b>Header</b>)
    text = re.sub(r"(?m)^#{1,6}\s+(.+)$", r"<b>\1</b>", text)

    # 6. Bold: **text** or __text__
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)

    # 7. Legacy / single asterisk bold or italic: *text* (word-boundary safe)
    # E.g. *Wheatley* or *Warning* becomes <b>...</b>
    text = re.sub(r"(?<!\w)\*([^*\n]+?)\*(?!\w)", r"<b>\1</b>", text)

    # 8. Italic: _text_ (only when bordered by whitespace or punctuation, not inside foo_bar_baz)
    text = re.sub(r"(?<!\w)_([^\n_]+?)_(?!\w)", r"<i>\1</i>", text)

    # 9. Strikethrough: ~~text~~
    text = re.sub(r"~~([^~]+?)~~", r"<s>\1</s>", text)

    # 10. Links: [title](url)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s\)]+)\)", r'<a href="\2">\1</a>', text)

    # 11. Bullet lists: lines starting with - or * or +
    text = re.sub(r"(?m)^(\s*)[-*+]\s+", r"\1• ", text)

    # 12. Restore inline code (HTML-escaped)
    for i, code in enumerate(inline_codes):
        escaped_inline = html.escape(code, quote=False)
        text = text.replace(f"\x00INLINECODE_{i}\x00", f"<code>{escaped_inline}</code>")

    # 13. Restore code blocks (HTML-escaped)
    for i, (lang, code) in enumerate(code_blocks):
        escaped_block = html.escape(code.rstrip("\n"), quote=False)
        if lang:
            block_html = f'<pre><code class="language-{lang}">{escaped_block}</code></pre>'
        else:
            block_html = f"<pre>{escaped_block}</pre>"
        text = text.replace(f"\x00CODEBLOCK_{i}\x00", block_html)

    return text


def strip_html_tags(text: str) -> str:
    """Strips all HTML tags and unescapes entities for clean plain-text fallback."""
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", "", text)
    return html.unescape(no_tags)


def split_telegram_message(text: str, max_length: int = 3800) -> List[str]:
    """Splits a long Markdown or plain-text message into chunks that fit within Telegram limits."""
    if len(text) <= max_length:
        return [text]

    chunks = []
    remaining = text

    while len(remaining) > max_length:
        # Try to split at double newline (paragraph boundary)
        split_idx = remaining.rfind("\n\n", 0, max_length)
        if split_idx == -1:
            # Try to split at single newline
            split_idx = remaining.rfind("\n", 0, max_length)
        if split_idx == -1:
            # Try to split at whitespace
            split_idx = remaining.rfind(" ", 0, max_length)
        if split_idx == -1:
            # Hard split
            split_idx = max_length

        chunks.append(remaining[:split_idx].rstrip())
        remaining = remaining[split_idx:].lstrip()

    if remaining:
        chunks.append(remaining)

    return chunks
