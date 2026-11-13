"""Bundle the interface with this week's data.

    web/index.html      full page (open locally or host on GitHub Pages)
    dist/scout.html     body-only fragment for publishing as a claude.ai artifact
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    app = open(os.path.join(ROOT, "web", "app.html"), encoding="utf-8").read()
    data = open(os.path.join(ROOT, "web", "data", "report.json"), encoding="utf-8").read()
    data = data.replace("</", "<\\/")  # keep the JSON from closing the script tag
    inject = f"<script>window.REPORT = {data};</script>\n"
    marker = "<script>\n(function () {"
    assert marker in app, "app.html script marker missing"
    fragment = app.replace(marker, inject + marker, 1)
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    with open(os.path.join(ROOT, "dist", "scout.html"), "w", encoding="utf-8") as f:
        f.write(fragment)
    head, _, body = fragment.partition('<div class="wrap">')
    full = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
            "<style>body{margin:0}[hidden]{display:none!important}img{max-width:100%}</style>\n"
            f"{head}</head>\n<body>\n<div class=\"wrap\">{body}</body>\n</html>\n")
    with open(os.path.join(ROOT, "web", "index.html"), "w", encoding="utf-8") as f:
        f.write(full)
    print("built web/index.html and dist/scout.html")


if __name__ == "__main__":
    main()
