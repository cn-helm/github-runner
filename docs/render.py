"""Render only the two public READMEs, preserving existing Helm artifacts."""
import argparse
import html
from pathlib import Path
import shutil

import markdown

ROOT = Path(__file__).resolve().parents[1]
PAGES = (("readme.md", "index.html", "en"),
         ("readme.zh.md", "readme.zh.html", "zh-CN"))
STYLE = """
:root { color-scheme: light dark; }
body { margin: 0 auto; max-width: 1040px; padding: 24px;
       font: 16px/1.65 system-ui, sans-serif; overflow-wrap: anywhere; }
a { color: light-dark(#075bb5, #8dc6ff); }
nav { display: flex; flex-wrap: wrap; gap: 20px; border-bottom: 1px solid #8886;
      padding-bottom: 16px; margin-bottom: 24px; }
h1, h2, h3 { line-height: 1.25; margin-top: 1.6em; }
pre { overflow-x: auto; padding: 16px; background: #8882; border-radius: 6px; }
code { font-family: ui-monospace, monospace; font-size: .9em; }
table { display: block; overflow-x: auto; border-collapse: collapse; }
th, td { border: 1px solid #8886; padding: 8px 12px; text-align: left; }
@media (max-width: 600px) { body { padding: 16px; } }
"""


def render(output: Path) -> None:
    output = output.resolve()
    if output == ROOT:
        raise ValueError("Use a separate output directory, not the chart root.")
    output.mkdir(parents=True, exist_ok=True)
    for source_name, target_name, language in PAGES:
        source = ROOT / source_name
        text = source.read_text(encoding="utf-8")
        # Keep Markdown links useful on GitHub and convert the language links for Pages.
        text = text.replace("(readme.md)", "(index.html)")
        text = text.replace("(readme.zh.md)", "(readme.zh.html)")
        body = markdown.markdown(text, extensions=["fenced_code", "tables", "toc"])
        title = "GitHub persistent runner" + (" · 简体中文" if language == "zh-CN" else "")
        page = f'''<!doctype html>
<html lang="{language}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{STYLE}</style>
</head>
<body>
<nav aria-label="Documentation">
<a href="index.html" lang="en">English</a>
<a href="readme.zh.html" lang="zh-CN">简体中文</a>
<a href="index.yaml">Helm index</a>
<a href="{source_name}">Markdown</a>
</nav>
<main>{body}</main>
</body>
</html>
'''
        (output / target_name).write_text(page, encoding="utf-8")
        shutil.copyfile(source, output / source_name)
    (output / ".nojekyll").touch()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".helm-repository"))
    render(parser.parse_args().output)
