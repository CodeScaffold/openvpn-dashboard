#!/usr/bin/env bash
# Keeps docs/demo/ (the GitHub Pages live demo) in sync with static/ (the real
# app). docs/demo/app.js and docs/demo/styles.css must be byte-identical to
# their static/ counterparts — the demo runs the real, unmodified frontend
# against docs/demo/demo-data.js's mocked fetch() instead of a real backend.
# docs/demo/index.html is static/index.html with four known differences
# (title, stylesheet path, the demo banner, and the script tags) — this
# script reapplies that same transform every time rather than hand-editing
# two copies.
#
# Usage:
#   scripts/sync-demo.sh          # sync docs/demo/ from static/
#   scripts/sync-demo.sh --check  # exit 1 if docs/demo/ has drifted (CI)

set -euo pipefail
cd "$(dirname "$0")/.."

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

cp static/app.js "$TMP/app.js"
cp static/styles.css "$TMP/styles.css"

python3 - "$TMP/index.html" << 'PYEOF'
import sys
content = open("static/index.html").read()

content = content.replace(
    "<title>OpenVPN Admin</title>",
    "<title>OpenVPN Admin — Live Demo</title>",
)
content = content.replace(
    '<link rel="stylesheet" href="/static/styles.css">',
    '''<link rel="stylesheet" href="styles.css">
<style>
  .demo-banner {
    display: flex; align-items: center; justify-content: center; gap: 8px;
    background: var(--surface-card); color: var(--text-muted); font-size: 0.8rem;
    text-align: center; padding: 7px 16px; border-bottom: 1px solid var(--border);
    font-family: var(--font);
  }
  .demo-banner::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--ov-orange); }
  .demo-banner a { color: var(--text); font-weight: 600; text-decoration: none; }
  .demo-banner a:hover { color: var(--ov-orange); }
  @media (min-width: 821px) { .sidebar { height: calc(100vh - 34px); } }
</style>''',
)
content = content.replace(
    "<body>\n",
    '''<body>

<div class="demo-banner">
  Live demo &middot; fake data, nothing here is real &middot;
  <a href="https://github.com/CodeScaffold/openvpn-dashboard" target="_blank" rel="noopener">get the code &amp; run your own &rarr;</a>
</div>
''',
    1,
)
content = content.replace(
    '<script src="/static/app.js"></script>',
    '<script src="demo-data.js"></script>\n<script src="app.js"></script>',
)

open(sys.argv[1], "w").write(content)
PYEOF

if [[ "$CHECK" == "1" ]]; then
  status=0
  for f in app.js styles.css index.html; do
    if ! diff -q "$TMP/$f" "docs/demo/$f" > /dev/null 2>&1; then
      echo "DRIFT: docs/demo/$f is out of sync with static/$f — run scripts/sync-demo.sh" >&2
      status=1
    fi
  done
  exit $status
else
  cp "$TMP/app.js" docs/demo/app.js
  cp "$TMP/styles.css" docs/demo/styles.css
  cp "$TMP/index.html" docs/demo/index.html
  echo "docs/demo/ synced from static/"
fi
