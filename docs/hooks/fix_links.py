"""MkDocs hook: inject README.md as index page with fixed links.

GitHub README uses repo-relative paths (``docs/quick_start.md``,
``examples/results/tb2.md``).  MkDocs serves from ``docs/``, so
``docs/...`` links must be rewritten and non-docs paths (``examples/``,
top-level files) must be pointed at GitHub so they resolve on the
published site.
"""

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_URL = "https://github.com/opensage-agent/nitrobox/blob/main"


def on_page_markdown(markdown: str, page, **kwargs) -> str:
    if page.file.src_path != "index.md":
        return markdown
    readme = (_ROOT / "README.md").read_text()
    # docs/foo.md → foo.md  (served by mkdocs)
    readme = re.sub(r'\]\(docs/', '](', readme)
    # examples/foo.md → absolute GitHub URL  (not in mkdocs tree)
    readme = re.sub(r'\]\(examples/', f']({_REPO_URL}/examples/', readme)
    return readme
