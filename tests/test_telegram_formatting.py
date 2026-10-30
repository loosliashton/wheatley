"""Tests for Markdown to Telegram HTML conversion and formatting utilities."""

import pytest
from wheatley.chat.formatter import (
    markdown_to_telegram_html,
    strip_html_tags,
    split_telegram_message,
)


def test_headers_conversion():
    md = "# Main Header\n## Sub Header\n### Section"
    html = markdown_to_telegram_html(md)
    assert "<b>Main Header</b>" in html
    assert "<b>Sub Header</b>" in html
    assert "<b>Section</b>" in html


def test_bold_and_italic():
    md = "This is **bold text** and this is __also bold__."
    html = markdown_to_telegram_html(md)
    assert "<b>bold text</b>" in html
    assert "<b>also bold</b>" in html

    # Single asterisks (Telegram-style emphasis)
    md2 = "Welcome to *Wheatley* homelab."
    assert "<b>Wheatley</b>" in markdown_to_telegram_html(md2)

    # Underscores for italic when separate word
    md3 = "This is _italicized_ text."
    assert "<i>italicized</i>" in markdown_to_telegram_html(md3)


def test_underscores_in_identifiers_preserved():
    # Crucial test: variable names with underscores MUST NOT become broken italics
    md = "Checking `guest_state` and node_metrics_total for check_engine_instance."
    html = markdown_to_telegram_html(md)
    # The backticked identifier should be code
    assert "<code>guest_state</code>" in html
    # The unbackticked identifiers should NOT have <i> tags inside them
    assert "node_metrics_total" in html
    assert "check_engine_instance" in html
    assert "<i>metrics</i>" not in html
    assert "<i>engine</i>" not in html


def test_inline_code_escaping():
    md = "Run `curl -s http://node:8006/api2/json?status=<pending>&all=1`."
    html = markdown_to_telegram_html(md)
    assert "<code>curl -s http://node:8006/api2/json?status=&lt;pending&gt;&amp;all=1</code>" in html


def test_code_blocks():
    md = """Here is the command:
```bash
systemctl status wheatley
if [ $? -ne 0 ]; then
    echo "<error> & exit"
fi
```
"""
    html = markdown_to_telegram_html(md)
    assert '<pre><code class="language-bash">' in html
    assert "&lt;error&gt; &amp; exit" in html
    assert "</code></pre>" in html


def test_bullet_points():
    md = "- First item\n- Second item\n* Third item"
    html = markdown_to_telegram_html(md)
    assert "• First item" in html
    assert "• Second item" in html
    assert "• Third item" in html


def test_links():
    md = "Visit [Proxmox](https://proxmox.com) for updates."
    html = markdown_to_telegram_html(md)
    assert '<a href="https://proxmox.com">Proxmox</a>' in html


def test_tables_converted_to_pre():
    md = """Summary table:
| Node | Status | RAM |
| --- | --- | --- |
| pve-node-01 | OK | 16GB |
| pve-node-02 | OK | 8GB |
"""
    html = markdown_to_telegram_html(md)
    assert "<pre>" in html
    assert "pve-node-01" in html
    assert "pve-node-02" in html


def test_strip_html_tags():
    html_sample = "<b>Wheatley</b> is <i>online</i> &amp; healthy. <code>pid=123</code>"
    clean = strip_html_tags(html_sample)
    assert clean == "Wheatley is online & healthy. pid=123"


def test_split_telegram_message():
    short = "Short message"
    assert split_telegram_message(short, max_length=100) == ["Short message"]

    long_msg = ("Paragraph 1: " + "A" * 60 + "\n\n" +
                "Paragraph 2: " + "B" * 60 + "\n\n" +
                "Paragraph 3: " + "C" * 60)
    chunks = split_telegram_message(long_msg, max_length=100)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk) <= 100


@pytest.mark.asyncio
async def test_typing_indicator_repeats():
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from wheatley.chat.telegram import TypingIndicator

    mock_chat = MagicMock()
    mock_chat.send_action = AsyncMock()

    async with TypingIndicator(mock_chat, interval=0.05):
        await asyncio.sleep(0.12)

    assert mock_chat.send_action.call_count >= 2
    mock_chat.send_action.assert_called_with("typing")


@pytest.mark.asyncio
async def test_typing_indicator_handles_none_chat():
    import asyncio
    from wheatley.chat.telegram import TypingIndicator

    async with TypingIndicator(None):
        await asyncio.sleep(0.01)
