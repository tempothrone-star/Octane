#!/usr/bin/env python3
"""Build public documentation/source from the committed Git tree, never local state."""
from __future__ import annotations

import argparse
import html
import json
import posixpath
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import markdown
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).resolve().parent / 'assets'
REPO = 'https://github.com/tempothrone-star/Octane'


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def public_files() -> dict[str, str]:
    files = {}
    for path in git('ls-tree', '-rz', 'HEAD').split(b'\0'):
        if not path:
            continue
        metadata, raw_name = path.split(b'\t', 1)
        mode, kind, object_id = metadata.split()
        name = raw_name.decode('utf-8')
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            continue
        # Defense in depth: only the public Git tree is read, and private state
        # filenames are excluded even if accidentally added in a future commit.
        if any(part in ('.git', '.octane', '.octance', 'node_modules') for part in name.split('/')):
            continue
        if name.endswith(('.env', '.sqlite3', '.db', '.pem', '.key')):
            continue
        data = git('cat-file', 'blob', object_id.decode())
        if len(data) > 300_000 or b'\0' in data:
            continue
        try:
            files[name] = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
    return files


def source_url(name: str) -> str:
    return '/source/' + quote(name, safe='/') + '.html'


def doc_url(name: str) -> str:
    return '/docs/' + quote(name.removeprefix('docs/').removesuffix('.md'), safe='/') + '.html'


def layout(title: str, body: str, *, section: str = '', side: str = '') -> str:
    links = []
    for label, url in [('Overview', '/'), ('Documentation', '/docs/README.html'), ('Source', '/source/')]:
        current = 'aria-current="page"' if section == label else ''
        links.append(f'<a href="{url}" {current}>{label}</a>')
    nav = ''.join(links)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Octane for OpenClaw: project intelligence, function tracing, and evidence-driven bug fixes.">
<title>{html.escape(title)} · Octane</title><link rel="icon" href="/assets/icon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/assets/site.css"><script defer src="/assets/site.js"></script></head>
<body><a class="skip" href="#main">Skip to content</a>
<header><a class="brand" href="/" aria-label="Octane home"><span class="mark">O</span> OCTANE<span class="version">{VERSION}</span></a>
<nav aria-label="Main navigation">{nav}<a href="{REPO}">GitHub</a></nav></header>
<div class="shell {"with-sidebar" if side else ""}">{side}<main id="main">{body}</main></div>
<footer><a class="brand" href="/">OCTANE</a><p>Project intelligence. Evidence before edits.</p>
<div><a href="/docs/CONTRIBUTING.html">Contribute</a><a href="/docs/SECURITY.html">Security</a><a href="{REPO}">MIT · Source on GitHub</a></div></footer>
</body></html>'''


def render_doc(name: str, text: str, files: dict[str, str]) -> tuple[str, str]:
    md = markdown.Markdown(extensions=['fenced_code', 'tables', 'toc', 'codehilite'],
                           extension_configs={'toc': {'permalink': False}, 'codehilite': {'guess_lang': False}})
    body = md.convert(text)

    def rewrite(match: re.Match) -> str:
        attr, raw = match.groups()
        link = html.unescape(raw)
        parts = urlsplit(link)
        if parts.scheme or parts.netloc or not parts.path:
            return match.group(0)
        target = posixpath.normpath(posixpath.join(posixpath.dirname(name), unquote(parts.path)))
        if target in files:
            path = doc_url(target) if target.endswith('.md') else source_url(target)
            # Markdown line links are translated to source browser line IDs.
            fragment = re.sub(r'^L(\d+)$', r'L-\1', parts.fragment) if not target.endswith('.md') else parts.fragment
        else:
            path = REPO + '/blob/main/' + quote(target, safe='/')
            fragment = parts.fragment
        return f'{attr}="{html.escape(urlunsplit(("", "", path, parts.query, fragment)), quote=True)}"'

    body = re.sub(r'(href|src)="([^"]+)"', rewrite, body)
    return body, md.toc


def build(destination: Path) -> None:
    global VERSION
    files = public_files()
    VERSION = json.loads(files['package.json'])['version']
    revision = git('rev-parse', 'HEAD').decode().strip()
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'assets').mkdir(exist_ok=True)
    for path in ASSETS.iterdir():
        if path.is_file():
            (destination / 'assets' / path.name).write_bytes(path.read_bytes())
    (destination / 'assets' / 'highlight.css').write_text(HtmlFormatter().get_style_defs('.highlight'))
    # One stylesheet keeps the restrictive script policy independent of the renderer.
    css = destination / 'assets' / 'site.css'
    css.write_text(css.read_text() + '\n' + (destination / 'assets' / 'highlight.css').read_text())
    search = []
    docs = sorted(name for name in files if name.endswith('.md'))
    doc_nav = '<aside><p class="eyebrow">PROJECT HANDBOOK</p><label for="doc-filter">Find a document</label><input id="doc-filter" type="search" placeholder="Search titles…"><nav aria-label="Documentation">'
    for name in docs:
        title = next((line.lstrip('# ').strip() for line in files[name].splitlines() if line.startswith('# ')), name)
        doc_nav += f'<a href="{doc_url(name)}" data-filter-item>{html.escape(title)}</a>'
    doc_nav += '</nav></aside>'
    for name in docs:
        body, _ = render_doc(name, files[name], files)
        page = f'<div class="breadcrumb">Documentation / {html.escape(name)}</div><article class="prose">{body}</article><div class="page-links"><a href="{source_url(name)}">View document source</a><a href="{REPO}/blob/main/{quote(name, safe="/")}">View on GitHub</a></div>'
        out = destination / doc_url(name).lstrip('/')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(layout(name, page, section='Documentation', side=doc_nav), encoding='utf-8')
        search.append({'title': name, 'url': doc_url(name), 'type': 'Documentation'})
    rows = []
    for name, text in sorted(files.items()):
        try:
            lexer = get_lexer_for_filename(name)
        except ClassNotFound:
            lexer = TextLexer()
        code = highlight(text, lexer, HtmlFormatter(linenos='table', lineanchors='L', anchorlinenos=True))
        page = f'<div class="breadcrumb"><a href="/source/">Source browser</a> / {html.escape(name)}</div><h1 class="file-title">{html.escape(name)}</h1><p class="muted">Committed public source · <a href="{REPO}/blob/{revision}/{quote(name, safe="/")}">{revision[:7]}</a></p><div class="source-code">{code}</div>'
        out = destination / source_url(name).lstrip('/')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(layout(name, page, section='Source'), encoding='utf-8')
        rows.append(f'<a class="file-row" href="{source_url(name)}" data-filter-item><span>{html.escape(name)}</span><span>{len(text.splitlines())} lines</span></a>')
        search.append({'title': name, 'url': source_url(name), 'type': 'Source'})
    source_page = f'<p class="eyebrow">THE PUBLIC CODEBASE</p><h1>Explore the source.</h1><p class="lead">Browse {len(files)} committed text files. Follow an implementation from its tool schema to the Python engine and its tests.</p><label for="file-filter">Find a file</label><input id="file-filter" type="search" placeholder="Filter by path, module, or language…"><p id="filter-count" class="muted" aria-live="polite">{len(rows)} files</p><div class="file-list">{"".join(rows)}</div>'
    (destination / 'source' / 'index.html').write_text(layout('Source browser', source_page, section='Source'))
    disclosure = files['README.md'].split('### Development and AI Assistance\n\n', 1)[1].split('\n\nAsk ', 1)[0]
    disclosure_html = markdown.markdown(disclosure)
    home = (ASSETS.parent / 'home.html').read_text()
    home = home.replace('{{VERSION}}', VERSION).replace('{{REVISION}}', revision[:7]).replace('{{DISCLOSURE}}', disclosure_html)
    (destination / 'index.html').write_text(layout('Understand the project. Fix the right bug.', home, section='Overview'))
    (destination / 'search.json').write_text(json.dumps(search, ensure_ascii=False))
    (destination / '404.html').write_text(layout('Page not found', '<h1>Page not found.</h1><p>Explore the <a href="/docs/README.html">documentation</a> or <a href="/source/">source browser</a>.</p>'))
    (destination / 'health.txt').write_text(f'Octane {VERSION}\n{revision}\n')
    # Archive the same public, filtered text files as the source browser. Never
    # archive the working directory, Git state, symlinks, credentials or databases.
    import zipfile
    with zipfile.ZipFile(destination / f'octane-{VERSION}-source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(f'Octane-{VERSION}/{name}', text)
    print(f'Built {len(docs)} documents and {len(files)} source files at {destination}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.output.resolve())
