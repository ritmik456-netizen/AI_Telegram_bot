import html
import re

TELEGRAM_LIMIT = 4000


def split_text(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _inline_to_html(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    return text


def markdown_to_html(text: str) -> str:
    """Переводит простую Markdown-разметку нейросети в HTML, который понимает Telegram."""
    parts = re.split(r"```(?:[\w+-]*\n)?(.*?)```", text, flags=re.S)
    result = []
    for index, part in enumerate(parts):
        if index % 2:
            result.append(f"<pre>{html.escape(part.strip(), quote=False)}</pre>")
        else:
            result.append(_inline_to_html(part))
    return "".join(result)
