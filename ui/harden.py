"""
Removes third-party requests from Chainlit's bundled page. Out of the box, index.html loads
Inter from Google Fonts and a KaTeX stylesheet from jsDelivr on every page load, so opening a
self-hosted data tool contacts both. KaTeX is only used for LaTeX, which is off here.

Idempotent, keeps a backup (index.html.orig) and is re-applied by run.sh, because reinstalling
Chainlit restores the original file.   python ui/harden.py [--check]
"""
import re
import shutil
import sys
from pathlib import Path

import importlib.util

# find the package without importing it: importing Chainlit creates a .chainlit/ config folder
# in whatever directory the import happens from
_spec = importlib.util.find_spec("chainlit")
INDEX = Path(_spec.origin).parent / "frontend" / "dist" / "index.html"
THIRD_PARTY = ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net")
LINK = re.compile(r"<link\b[^>]*>", re.I)


def external_hosts(html: str) -> list[str]:
    return sorted({h for h in THIRD_PARTY for tag in LINK.findall(html) if h in tag})


def strip(html: str) -> str:
    return LINK.sub(lambda m: "" if any(h in m.group(0) for h in THIRD_PARTY) else m.group(0), html)


def main() -> int:
    html = INDEX.read_text()
    found = external_hosts(html)
    if "--check" in sys.argv:
        print("third-party hosts in index.html:", found or "none")
        return 1 if found else 0
    if not found:
        return 0
    backup = INDEX.with_name("index.html.orig")
    if not backup.exists():
        shutil.copy(INDEX, backup)
    INDEX.write_text(strip(html))
    print(f"removed third-party requests from Chainlit's page: {', '.join(found)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
