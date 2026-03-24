"""MkDocs hook: inject README.md as index page with fixed links.

GitHub README uses `docs/quick_start.md` (relative to repo root).
MkDocs expects `quick_start.md` (relative to docs/).
This hook reads README.md, strips `docs/` from link targets, and
returns it as the index page markdown.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def on_page_markdown(markdown: str, page, **kwargs) -> str:
    if page.file.src_path != "index.md":
        return markdown
    readme = (_ROOT / "README.md").read_text()
    return re.sub(r'\]\(docs/', '](', readme)
