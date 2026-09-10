# Lucide icons (vendored)

- Package: `lucide-static`
- Version: 1.44.0
- Upstream: https://lucide.dev — https://github.com/lucide-icons/lucide
- Licence: ISC (see `LICENSE` in this directory; a subset of icons is
  MIT-licensed from the Feather project, listed in the same file)
- Tarball integrity (npm registry):
  `sha512-u1PAHVq1Ka06FDcXFY8r8fLtS5efVHaawXEETW5tmfnMbd9NU6sPK3GAvZgrbJzY5JmbjHoTTQDcoQOBmW1RKg==`

No SVG files are shipped from this directory. The icons the console uses are
selected by name in `scripts/gen-icons.py` and their bodies are written into
`web/static/js/icons.js` (and a few CSS mask data-URIs in
`web/static/css/style.css`) so the airgap tarball stays self-contained and the
page never fetches anything at runtime.

Regenerate after changing the manifest or bumping the version:

```sh
python3 scripts/gen-icons.py                       # online, verifies the hash
python3 scripts/gen-icons.py --from-tarball FILE   # airgap
python3 scripts/gen-icons.py --check               # CI-style staleness check
```
