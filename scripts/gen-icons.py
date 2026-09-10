#!/usr/bin/env python3
"""Regenerate the vendored Lucide icon set used by the web console.

Usage:
  scripts/gen-icons.py                      # download lucide-static, verify, rewrite
  scripts/gen-icons.py --from-tarball FILE  # airgap: use a local lucide-static .tgz
  scripts/gen-icons.py --from-dir DIR       # airgap: use an unpacked package/ dir
  scripts/gen-icons.py --check              # exit 1 if the generated blocks are stale

The console never loads icons at runtime from the network (CSP is
`default-src 'self'`; the airgap tarball must be self-contained). Instead
this script extracts the SVG bodies of the icons listed in MANIFEST from
the pinned lucide-static release and writes them into two marker-delimited
blocks:

  web/static/js/icons.js      the PATHS table read by Icons.svg()
  web/static/css/style.css    --icon-* mask data-URIs for CSS pseudo-elements

Icons are addressed by *semantic* name (``ok``, ``destroy``, ``node``) rather
than by Lucide file name, so call sites read by meaning and a future swap of
the underlying glyph is a one-line change here. Keys must stay valid JS
identifiers (tests grep for ``name:``).

Adding an icon: add a row to MANIFEST, run this script, commit both the
manifest and the regenerated blocks. Licence: web/static/vendor/lucide/LICENSE.
"""

import argparse
import base64
import hashlib
import io
import re
import sys
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

LUCIDE_VERSION = "1.44.0"
LUCIDE_INTEGRITY = (
    "sha512-u1PAHVq1Ka06FDcXFY8r8fLtS5efVHaawXEETW5tmfnMbd9NU6sPK3GAvZgrbJzY5"
    "JmbjHoTTQDcoQOBmW1RKg=="
)
TARBALL_URL = (
    "https://registry.npmjs.org/lucide-static/-/lucide-static-"
    f"{LUCIDE_VERSION}.tgz"
)

ROOT = Path(__file__).resolve().parent.parent
ICONS_JS = ROOT / "web" / "static" / "js" / "icons.js"
STYLE_CSS = ROOT / "web" / "static" / "css" / "style.css"
CACHE_DIR = ROOT / "dist"

# Semantic name -> lucide-static icon file (without .svg), grouped for the
# generated block's comments. Order is preserved in the output.
MANIFEST = [
    ("Statut", {
        "ok": "check",
        "fail": "x",
        "warn": "triangle-alert",
        "info": "info",
        "pending": "hourglass",
        "running": "zap",
        "dotOn": "circle-dot",
        "dotOff": "circle",
        "error": "circle-x",
        "paused": "circle-pause",
        "timer": "timer",
        "cancelled": "ban",
    }),
    ("Alimentation", {
        "play": "play",
        "stop": "square",
        "restart": "rotate-cw",
        "power": "power",
        "refresh": "refresh-cw",
        "restore": "refresh-ccw",
    }),
    ("Objets", {
        "node": "server",
        "vm": "monitor",
        "metrics": "chart-column",
        "network": "network",
        "storage": "database",
        "volume": "cylinder",
        "disk": "hard-drive",
        "cdrom": "disc",
        "bundle": "package",
        "install": "package",
        "switch": "arrow-right-left",
        "console": "square-terminal",
        "snapshot": "camera",
        "migrate": "arrow-left-right",
        "settings": "sliders-horizontal",
        "notes": "notebook-pen",
        "doc": "file-text",
        "activity": "scroll-text",
        "docs": "book-open",
        "help": "circle-help",
    }),
    ("Fonctionnalités", {
        "capi": "rocket",
        "terraform": "layers",
        "baremetal": "satellite",
        "robot": "bot",
        "tools": "wrench",
        "firmware": "cpu",
        "compute": "cpu",
        "cloud": "cloud",
        "placement": "map-pin",
        "lifecycle": "repeat",
        "general": "clipboard-list",
        "wizard": "wand-sparkles",
        "magnet": "magnet",
        "gamepad": "gamepad-2",
        "shield": "shield",
        "user": "user",
        "construction": "construction",
        "key": "key-round",
        "code": "braces",
        "plug": "plug",
        "languages": "languages",
    }),
    ("Actions", {
        "edit": "pencil",
        "delete": "trash-2",
        "trash": "trash-2",
        "destroy": "bomb",
        "save": "save",
        "download": "download",
        "upload": "upload",
        "preview": "eye",
        "build": "hammer",
        "clean": "brush-cleaning",
        "search": "search",
        "fit": "maximize",
        "unlock": "lock-open",
        "lock": "lock",
        "add": "plus",
        "close": "x",
        "undo": "undo-2",
        "redo": "redo-2",
        "tag": "tag",
        "pin": "pin",
        "star": "star",
        "news": "newspaper",
        "test": "flask-conical",
        "brand": "hexagon",
    }),
    ("Interface", {
        "chevronDown": "chevron-down",
        "chevronRight": "chevron-right",
        "chevronLeft": "chevron-left",
        "chevronUp": "chevron-up",
        "sort": "chevrons-up-down",
        "arrowUp": "arrow-up",
        "arrowDown": "arrow-down",
        "arrowLeft": "arrow-left",
        "arrowRight": "arrow-right",
        "moveVertical": "move-vertical",
        "grip": "grip-vertical",
    }),
]

ICONS = {name: lucide for _, group in MANIFEST for name, lucide in group.items()}

# CSS custom properties written into style.css, each a mask data-URI of the
# named semantic icon. Masks only use the alpha channel, so the stroke colour
# is irrelevant; the rule supplies the colour via background-color.
CSS_MASKS = {
    "icon-check": "ok",
    "icon-x": "fail",
    "icon-sort": "sort",
    "icon-sort-asc": "chevronUp",
    "icon-sort-desc": "chevronDown",
}

JS_BEGIN = "  // BEGIN GENERATED"
JS_END = "  // END GENERATED"
CSS_BEGIN = "/* BEGIN GENERATED"
CSS_END = "/* END GENERATED"

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def fetch_tarball(cache_dir: Path) -> bytes:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"lucide-static-{LUCIDE_VERSION}.tgz"
    if cached.exists():
        data = cached.read_bytes()
    else:
        print(f"downloading {TARBALL_URL}", file=sys.stderr)
        with urllib.request.urlopen(TARBALL_URL, timeout=60) as resp:
            data = resp.read()
        cached.write_bytes(data)
    verify_integrity(data)
    return data


def verify_integrity(data: bytes) -> None:
    algo, _, expected = LUCIDE_INTEGRITY.partition("-")
    digest = base64.b64encode(hashlib.new(algo, data).digest()).decode()
    if digest != expected:
        sys.exit(
            f"integrity mismatch for lucide-static {LUCIDE_VERSION}: "
            f"expected {LUCIDE_INTEGRITY}, got {algo}-{digest}"
        )


def load_svgs_from_tarball(data: bytes) -> dict:
    svgs = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name.startswith("package/icons/") and member.name.endswith(".svg"):
                name = Path(member.name).stem
                svgs[name] = tar.extractfile(member).read().decode("utf-8")
    return svgs


def load_svgs_from_dir(package_dir: Path) -> dict:
    icons_dir = package_dir / "icons"
    if not icons_dir.is_dir():
        sys.exit(f"{icons_dir} not found — pass the unpacked package/ directory")
    return {p.stem: p.read_text("utf-8") for p in icons_dir.glob("*.svg")}


def svg_body(svg_text: str) -> str:
    """Return the child elements of a Lucide SVG as one compact line."""
    text = re.sub(r"<!--.*?-->", "", svg_text, flags=re.S)
    m = re.search(r"<svg\b[^>]*>(.*)</svg>", text, flags=re.S)
    if not m:
        raise ValueError("no <svg> element")
    body = m.group(1)
    body = re.sub(r"\s+", " ", body).strip()
    body = body.replace(" />", "/>").replace("> <", "><")
    if "'" in body:
        raise ValueError("unexpected quote in SVG body")
    return body


def build_paths(svgs: dict) -> list:
    missing = sorted({v for v in ICONS.values() if v not in svgs})
    if missing:
        sys.exit("icons missing from lucide-static: " + ", ".join(missing))
    bad = [k for k in ICONS if not _IDENT_RE.match(k)]
    if bad:
        sys.exit("manifest keys must be JS identifiers: " + ", ".join(bad))
    out = [f"{JS_BEGIN} — lucide-static {LUCIDE_VERSION} (scripts/gen-icons.py, ne pas éditer)"]
    for label, group in MANIFEST:
        out.append(f"    // {label}")
        for name, lucide in group.items():
            out.append(f"    {name}: '{svg_body(svgs[lucide])}',  // {lucide}")
    out.append(JS_END)
    return out


def wrap_svg(body: str, stroke: str = "black") -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
        f'stroke="{stroke}" stroke-width="2" stroke-linecap="round" '
        f'stroke-linejoin="round">{body}</svg>'
    )


def build_css(svgs: dict) -> list:
    out = [f"{CSS_BEGIN} — lucide-static {LUCIDE_VERSION} masks (scripts/gen-icons.py) */", ":root {"]
    for prop, semantic in CSS_MASKS.items():
        svg = wrap_svg(svg_body(svgs[ICONS[semantic]]))
        uri = "data:image/svg+xml," + urllib.parse.quote(svg, safe="=/:; ,")
        out.append(f'  --{prop}: url("{uri}");')
    out.append("}")
    out.append(f"{CSS_END} */")
    return out


def replace_block(text: str, begin: str, end: str, lines: list, path: Path) -> str:
    start = text.find(begin)
    stop = text.find(end, start + 1) if start >= 0 else -1
    if start < 0 or stop < 0:
        sys.exit(f"markers '{begin}' / '{end}' not found in {path}")
    stop = text.index("\n", stop)
    return text[:start] + "\n".join(lines) + text[stop:]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--from-tarball", type=Path, help="local lucide-static .tgz")
    src.add_argument("--from-dir", type=Path, help="unpacked lucide-static package/ dir")
    ap.add_argument("--check", action="store_true", help="fail if generated blocks are stale")
    args = ap.parse_args(argv)

    if args.from_dir:
        svgs = load_svgs_from_dir(args.from_dir)
    else:
        data = args.from_tarball.read_bytes() if args.from_tarball else fetch_tarball(CACHE_DIR)
        verify_integrity(data)
        svgs = load_svgs_from_tarball(data)

    targets = [
        (ICONS_JS, JS_BEGIN, JS_END, build_paths(svgs)),
        (STYLE_CSS, CSS_BEGIN, CSS_END, build_css(svgs)),
    ]
    stale = []
    for path, begin, end, lines in targets:
        before = path.read_text("utf-8")
        after = replace_block(before, begin, end, lines, path)
        if after != before:
            stale.append(path)
            if not args.check:
                path.write_text(after, "utf-8")
    if args.check:
        if stale:
            print("stale generated blocks: " + ", ".join(str(p.relative_to(ROOT)) for p in stale))
            return 1
        print("generated blocks are up to date")
        return 0
    print(f"wrote {len(ICONS)} icons from lucide-static {LUCIDE_VERSION}"
          + (" (" + ", ".join(p.name for p in stale) + " updated)" if stale else " (no change)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
