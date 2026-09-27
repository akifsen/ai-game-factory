"""Static offline review page. No script, remote assets, or live database."""

from __future__ import annotations

import html
from typing import Any


def _text(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_review_page(report: dict[str, Any]) -> str:
    """Render one snapshot page. Image sources must already be relative and safe."""
    images = report.get("images")
    if not isinstance(images, list):
        raise ValueError("review report images must be a list")
    cards: list[str] = []
    for item in images:
        if not isinstance(item, dict):
            raise ValueError("review image entry must be an object")
        src = item.get("src")
        if not isinstance(src, str) or not _safe_relative(src):
            raise ValueError("review image path must be a relative file name")
        portrait = int(item.get("height", 0)) > int(item.get("width", 0))
        css = "capture portrait" if portrait else "capture"
        raw_state = item.get("state")
        state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}
        cards.append(
            "<figure>"
            f'<img class="{css}" src="{_text(src)}" alt="{_text(item.get("id"))} tick {_text(item.get("tick"))}" width="{_text(item.get("width"))}" height="{_text(item.get("height"))}">'
            "<figcaption>"
            f"<strong>{_text(item.get('id'))}</strong> tick {_text(item.get('tick'))}<br>"
            f"HP {_text(state.get('player_hp'))} · enemies {_text(state.get('enemies_remaining'))} · score {_text(state.get('score'))}<br>"
            f"SHA-256 {_text(item.get('sha256'))}"
            "</figcaption></figure>"
        )
    checks = report.get("checks")
    check_rows = ""
    if isinstance(checks, list):
        for check in checks:
            if isinstance(check, dict):
                check_rows += (
                    "<li>"
                    f"{_text(check.get('id'))}: {_text(check.get('status'))} — {_text(check.get('detail', ''))}"
                    "</li>"
                )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_text(report.get("title", "Game Factory review"))}</title>
<style>
body {{ margin: 0; font-family: "Segoe UI", sans-serif; background: #f4f1ea; color: #1d1c19; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 28px 20px 48px; }}
h1 {{ font-size: 1.6rem; margin-bottom: 0.2rem; }}
h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
.meta, .status {{ line-height: 1.45; }}
.pill {{ display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px; background: #e7e1d6; margin-right: 0.4rem; }}
.pass {{ background: #d9ead3; }}
.fail {{ background: #f4cccc; }}
.pending {{ background: #fff2cc; }}
figure {{ margin: 0 0 28px; }}
img.capture {{ max-width: min(100%, 1280px); height: auto; background: #111; }}
img.portrait {{ max-width: min(100%, 420px); }}
figcaption {{ margin-top: 8px; }}
code, pre {{ font-family: Consolas, monospace; }}
pre {{ white-space: pre-wrap; background: #fff; padding: 12px; }}
</style>
</head>
<body>
<main>
<h1>{_text(report.get("title", "Rendered capture review"))}</h1>
<p class="meta">{_text(report.get("project_id"))} · {_text(report.get("scenario_id"))}</p>
<h2>Technical result</h2>
<p class="status"><span class="pill {_text(str(report.get("technical_status", "")).lower())}">{_text(report.get("technical_status"))}</span></p>
<h2>Human review</h2>
<p class="status"><span class="pill pending">{_text(report.get("human_status", "PENDING"))}</span> This page does not record a decision.</p>
<h2>Environment</h2>
<p class="meta">{_text(report.get("environment"))}</p>
<h2>Captures</h2>
{"".join(cards)}
<h2>Checks</h2>
<ul>{check_rows}</ul>
<h2>Commands</h2>
<pre>{_text(report.get("commands", ""))}</pre>
</main>
</body>
</html>
"""


def _safe_relative(path: str) -> bool:
    if path.startswith(("/", "\\")) or ".." in path.split("/") or ":" in path or "\\" in path:
        return False
    if path.lower().startswith(("http:", "https:", "file:", "javascript:")):
        return False
    return path.startswith("images/") and path.count("/") == 1
