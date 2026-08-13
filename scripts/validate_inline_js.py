"""Parse every inline JavaScript block with Node's production parser."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_FILES = (
    "dashboard.html",
    "deepdive.html",
    "gameside_deepdive.html",
    "props.html",
    "tracker.html",
)
STATIC_JS_FILES = (\n    "static/global-nav.js",\n    "static/mobile-nav.js",\n    "static/product-hub.js",\n)


def main() -> int:
    failures = []
    count = 0
    for name in HTML_FILES:
        source = (ROOT / name).read_text(encoding="utf-8")
        scripts = re.findall(
            r"<script(?:\s[^>]*)?>(.*?)</script>", source, flags=re.IGNORECASE | re.DOTALL
        )
        for index, script in enumerate(scripts, start=1):
            count += 1
            result = subprocess.run(
                ["node", "--check", "-"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                failures.append(f"{name} script {index}: {result.stderr.strip()}")
    for name in STATIC_JS_FILES:
        count += 1
        result = subprocess.run(
            ["node", "--check", str(ROOT / name)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            failures.append(f"{name}: {result.stderr.strip()}")
    if failures:
        raise SystemExit("\n\n".join(failures))
    print(f"Validated {count} inline JavaScript blocks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
