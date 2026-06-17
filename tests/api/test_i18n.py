"""i18n coverage tests.

Walk every HTML template + every JS file under web/ for `data-i18n*` attribute
references, parse the per-language dicts from `web/static/js/i18n.js`, and
flag any key that:
  - is referenced in the UI but absent from the English dict (hard failure —
    English is the source-of-truth fallback)
  - is in the English dict but absent from any other lang (soft warning,
    listed but not a test failure — those rows fall through to the EN
    string at runtime)

Catches the case the user saw with v1.3.9: I added the CAPHV sub-tabs and
forgot to register `capi.tab.install`, `capi.tab.clusters`, `capi.install.title`
in any dict, so they rendered as raw keys on screen.
"""

import re
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
WEB  = ROOT / "web"
I18N_JS = WEB / "static" / "js" / "i18n.js"
TEMPLATES_DIR = WEB / "templates"
JS_DIR = WEB / "static" / "js"

# Match `data-i18n[-title|-placeholder]="key"` in HTML — must use double or
# single quotes. We also catch the JS `data-i18n="..."` snippets that some
# components emit dynamically (e.g. capi.js).
ATTR_RE = re.compile(r"""data-i18n(?:-title|-placeholder)?\s*=\s*['"]([\w.-]+)['"]""")
# Match `data-tip-i18n="key"` — used by the tooltip system to look up the
# tip text via the i18n dict (capi.js, app.js emit these dynamically).
TIP_RE = re.compile(r"""data-tip-i18n\s*=\s*['"]([\w.-]+)['"]""")
# Match programmatic lookups `i18n.t('key')` / `i18n.t("key")` in JS.
# Ignores template-literal calls — those are usually computed at runtime
# (e.g. `i18n.t(`vm.state.${vm.runStrategy}`)`) and would otherwise
# explode the false-positive count.
JS_T_RE = re.compile(r"""\bi18n\.t\(\s*['"]([\w.-]+)['"]""")
# Lang block boundaries in i18n.js — `en: {`, `fr: {`, …
LANG_HEADER_RE = re.compile(r"^\s*(en|fr|it|es|de):\s*\{")
# Key extraction inside a lang block — `'key.name':` or `"key.name":`.
KEY_RE = re.compile(r"""['"]([\w.-]+)['"]\s*:\s*['"]""")


def _all_referenced_keys():
    """Scan all .html and .js files for i18n references — data-i18n,
    data-tip-i18n, and `i18n.t('key')`. Skip i18n.js itself (its docstring
    contains literal `data-i18n="key"` examples)."""
    keys = set()
    for f in list(TEMPLATES_DIR.rglob("*.html")) + list(JS_DIR.rglob("*.js")):
        if f.name == "i18n.js":
            continue
        try:
            text = f.read_text()
        except OSError:
            continue
        keys.update(ATTR_RE.findall(text))
        keys.update(TIP_RE.findall(text))
        # JS_T_RE catches `i18n.t('foo.bar')` calls, but also matches the
        # literal prefix of concatenated calls like `i18n.t('foo.' + x)`
        # where the captured group is `foo.` (trailing dot, partial key).
        # Strip those — a real key never ends in `.`.
        for k in JS_T_RE.findall(text):
            if not k.endswith("."):
                keys.add(k)
    return keys


def _lang_dicts():
    """Parse i18n.js to return {lang_code: set(keys)}."""
    text = I18N_JS.read_text()
    out = {}
    cur_lang = None
    depth = 0
    for line in text.splitlines():
        m = LANG_HEADER_RE.match(line)
        if m:
            cur_lang = m.group(1)
            out[cur_lang] = set()
            depth = 1
            continue
        if cur_lang is None:
            continue
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            cur_lang = None
            continue
        for k in KEY_RE.findall(line):
            out.setdefault(cur_lang, set()).add(k)
    return out


def test_i18n_no_missing_keys_in_english():
    """Every `data-i18n="..."` reference must exist in the English dict.

    English is the canonical fallback — if a key is missing there, the
    runtime renders the raw key string on screen (the bug the user spotted
    with `capi.tab.install`).
    """
    referenced = _all_referenced_keys()
    dicts = _lang_dicts()
    en = dicts.get("en") or set()
    missing = sorted(referenced - en)
    assert not missing, (
        "Missing English i18n entries (would render as raw keys):\n  - "
        + "\n  - ".join(missing)
    )


def test_i18n_dict_parity_across_languages():
    """Soft check: every other language should ideally cover the same keys
    as English. Reported as a single failure with the diff per lang so the
    author can address them all at once. NOT a blocker — at runtime missing
    locale keys fall through to English.
    """
    dicts = _lang_dicts()
    en = dicts.get("en") or set()
    holes = {}
    for lang in ("fr", "it", "es", "de"):
        l = dicts.get(lang) or set()
        delta = sorted(en - l)
        if delta:
            holes[lang] = delta
    if holes:
        # Baseline tech debt: ~390 holes today (IT/ES/DE never had the
        # automation/clusters/vms/snapshots/migrate/vm-edit/console keys).
        # Block only on regressions ABOVE the baseline so CI doesn't stall
        # while still catching "I added an EN key and forgot all 4 others".
        BASELINE = 632
        total = sum(len(v) for v in holes.values())
        if total > BASELINE:
            lines = []
            for lang, keys in holes.items():
                lines.append(f"  {lang}: {len(keys)} missing — first 5: {keys[:5]}")
            pytest.fail(
                f"Translations parity regressed: {total} holes > baseline {BASELINE}.\n"
                + "\n".join(lines)
                + "\n\n(Bump BASELINE if you added new EN keys and intentionally "
                "delay translating to IT/ES/DE; ideally translate them.)"
            )


def test_i18n_resolves_data_tip_i18n_attribute():
    """REGRESSION (v1.4.15): applyTranslations() must resolve
    data-tip-i18n="key" → data-tip="<translated string>". The CSS
    tooltip rule .tip[data-tip]::after reads attr(data-tip); without
    this resolution step the bubble never renders, even though the
    markup looks correct.

    We can't easily run the JS, so we check the i18n source string-level:
    the loop body must exist.
    """
    src = I18N_JS.read_text()
    assert "data-tip-i18n" in src, (
        "i18n.js must resolve data-tip-i18n into data-tip on every "
        "translation pass (see CSS .tip[data-tip]::after rule)."
    )
    # And the resolution must use setAttribute('data-tip', …) — anything
    # else would not feed the CSS attr() lookup.
    assert "setAttribute('data-tip'" in src or 'setAttribute("data-tip"' in src, (
        "i18n.js's data-tip-i18n handler must call setAttribute('data-tip', …)"
    )


def test_i18n_no_orphan_keys_in_english():
    """Keys defined in EN but never referenced — usually placeholders for
    features that didn't land. We grep both .html and .js, so keys used via
    `i18n.t('foo.bar')` calls are NOT detected (no quotes/dashes match).
    Baseline accepts the current tech debt; trip on regression."""
    referenced = _all_referenced_keys()
    en = _lang_dicts().get("en") or set()
    orphans = sorted(en - referenced)
    BASELINE = 132   # v1.4.19: +topology.* keys used via i18n.t() in template literals
    if len(orphans) > BASELINE:
        pytest.fail(
            f"{len(orphans)} unused English i18n entries (baseline {BASELINE}):\n  - "
            + "\n  - ".join(orphans[:20]) + "\n  …"
        )
