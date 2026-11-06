#!/usr/bin/env python3
"""Paquets d'artefacts signés : construire, signer, vérifier, importer.

    harvester-artefacts pack    DESCRIPTION [--out DIR] [--sign-key FICHIER] [--creds FICHIER]
                                [--platform os/arch] [--plain-http HÔTE]...
    harvester-artefacts sign    PAQUET --key FICHIER
    harvester-artefacts verify  PAQUET [--signers FICHIER]
    harvester-artefacts inspect PAQUET [--signers FICHIER]
    harvester-artefacts import  PAQUET [--accept-unsigned | --accept-unknown-key] [--person NOM]
    harvester-artefacts list
    harvester-artefacts remove  NOM VERSION

`pack` tourne sur une machine connectée, en bibliothèque standard seulement
(décision 122) : la description (JSON ; YAML si PyYAML est là) nomme des
charts (dépôt HTTP ou `oci://`), des images (une plateforme, `linux/amd64`
par défaut) et des manifestes ; le paquet `<nom>-<version>.tar.gz` porte
SHA256SUMS en premier membre, l'index `artefacts.yaml`, `charts/`,
`images/` (disposition OCI, couches partagées une fois) et `manifests/`.
Les identifiants des registres sources viennent d'un fichier privé
(`--creds`, JSON `{hôte: {username, password}}`), jamais d'un argument.
`--plain-http` sert aux registres de test en clair.

`sign` et `verify` emploient `ssh-keygen -Y` (espace de noms
`harvester-artefacts`) ; `verify` rend 0 seulement pour une signature valide
d'une clé de confiance. `import` (dans la console) applique la décision du
plan (point 184) : 0 accepté, 2 refusé, 3 acquittement requis.

Progression sur stderr au format STEP_EVENT|<étape>|<statut>|<message> ;
résultat en JSON sur stdout. Codes : 0 succès, 1 échec, 2 refus, 3
acquittement requis.

Voir docs/design/2026-11-05-artefacts-registre-260-plan.md (tâche 4).
"""

import argparse
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import artefacts as art  # noqa: E402
import oci_layout as ol  # noqa: E402
import oci_push as op  # noqa: E402

EXIT_OK, EXIT_FAIL, EXIT_REFUSED, EXIT_ACK = 0, 1, 2, 3
INDEX_MAX = 64 << 20                # index.yaml d'un dépôt Helm (certains font des dizaines de Mo)
CHART_MAX = 64 << 20
TOP_KEYS = {"name", "version", "description", "items"}
ITEM_KEYS = {"name", "kind", "version", "description", "chart", "values", "images", "files"}
CHART_KEYS = {"repo", "name", "version"}
FILE_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")


def step(sid, status, msg=""):
    clean = " ".join(str(msg).split())
    sys.stderr.write(f"STEP_EVENT|{sid}|{status}|{clean}\n")
    sys.stderr.flush()


def out(obj):
    print(json.dumps(obj, indent=2))


def state_dir():
    import cluster_decl
    return cluster_decl.state_dir()


def console_config():
    """config.yaml de la console (verrou du mode strict, point 184)."""
    from kube import _read_yaml
    cfg = Path(os.environ.get("HARVESTER_OPS_CONFIG", "/etc/harvester-ops/config.yaml"))
    return (_read_yaml(cfg) if cfg.is_file() else None) or {}


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------
def read_description(path):
    """La description : JSON, ou YAML si PyYAML est présent (point 185)."""
    text = Path(path).read_text()
    try:
        return json.loads(text)
    except ValueError:
        pass
    try:
        import yaml
    except ImportError:
        raise ValueError(f"{Path(path).name} is not JSON, and PyYAML is not installed to read YAML: "
                         "write the description in JSON") from None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"{Path(path).name}: neither JSON nor YAML ({str(e).splitlines()[0]})") from None


def stored_name(ref, as_=None):
    """Nom d'une image dans le paquet : `as` s'il est donné, sinon la
    référence ; tag gardé et empreinte retirée quand les deux sont là (une
    image épinglée par empreinte reste nommée par son tag)."""
    host, path, tag, digest = ol.normalize_ref(as_ or ref)
    return f"{host}/{path}:{tag}" if tag else f"{host}/{path}@{digest}"


def check_description(doc):
    """(description normalisée, erreurs). Images : {"ref", "as", "name"}."""
    errors = []
    if not isinstance(doc, dict):
        return None, ["the description is not an object"]
    for k in sorted(set(doc) - TOP_KEYS):
        errors.append(f"unknown key: {k}")
    if not art.NAME_RE.match(str(doc.get("name") or "")):
        errors.append("name: RFC 1123, a letter first, 40 characters at most")
    if not art.VERSION_RE.match(str(doc.get("version") or "")):
        errors.append("version: x.y.z or YYYY.MM[.DD][-n]")
    items = doc.get("items")
    if not isinstance(items, list) or not items:
        errors.append("items: a list, one item at least")
        items = []
    out_items, names = [], {}
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(it, dict):
            errors.append(f"{where}: not an object")
            continue
        for k in sorted(set(it) - ITEM_KEYS):
            errors.append(f"{where}: unknown key: {k}")
        kind = it.get("kind")
        if kind not in art.KINDS + art.LATER_KINDS:
            errors.append(f"{where}.kind: one of {', '.join(art.KINDS + art.LATER_KINDS)}")
        item = {k: it[k] for k in ("name", "kind", "description", "values") if k in it}
        if kind == "chart":
            c = it.get("chart")
            if not isinstance(c, dict) or set(c) != CHART_KEYS or not all(isinstance(c[k], str) and c[k]
                                                                         for k in CHART_KEYS):
                errors.append(f"{where}.chart: {{repo, name, version}}")
            elif not re.match(r"^(oci|https?)://[^\s]+$", c["repo"]):
                errors.append(f"{where}.chart.repo: oci://, https:// or http://")
            elif not re.match(r"^[A-Za-z0-9._-]+$", c["name"]) or not re.match(r"^[A-Za-z0-9._+-]+$", c["version"]):
                errors.append(f"{where}.chart: name and version without spaces or slashes")
            else:
                item["chart"] = dict(c)
        elif "chart" in it:
            errors.append(f"{where}: chart is for a chart")
        imgs = []
        for j, x in enumerate(it.get("images") or []):
            ref, as_ = (x, None) if isinstance(x, str) else (x.get("ref"), x.get("as")) if isinstance(x, dict) \
                and set(x) <= {"ref", "as"} else (None, None)
            try:
                if not isinstance(ref, str):
                    raise ValueError("a reference, or {ref, as}")
                if as_ is not None and (not isinstance(as_, str) or ol.normalize_ref(as_)[3]):
                    raise ValueError("as: a name with a tag, no digest")
                name = stored_name(ref, as_)
            except ValueError as e:
                errors.append(f"{where}.images[{j}]: {e}")
                continue
            if name in names and names[name] != ol.full_name(ref):
                errors.append(f"{where}.images[{j}]: {name} names two different images")
            names[name] = ol.full_name(ref)
            imgs.append({"ref": ref, "as": as_, "name": name})
        if kind == "images" and not imgs and not any(e.startswith(f"{where}.images") for e in errors):
            errors.append(f"{where}.images: one image at least")
        if imgs:
            item["images"] = imgs
        files = it.get("files")
        if kind == "manifest" or (kind in art.LATER_KINDS and files is not None):
            if not isinstance(files, list) or not files or not all(isinstance(f, str) and FILE_RE.match(Path(f).name)
                                                                  for f in files):
                errors.append(f"{where}.files: .yaml files, one at least")
            elif len({Path(f).name for f in files}) != len(files):
                errors.append(f"{where}.files: two files with the same name")
            else:
                item["files"] = list(files)
        elif files is not None:
            errors.append(f"{where}: files are for a manifest")
        out_items.append(item)
    if errors:
        return None, errors
    return {"name": doc["name"], "version": doc["version"], "description": doc.get("description", ""),
            "items": out_items}, []


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def read_creds(path):
    """{hôte: {username, password}} d'un fichier privé (0600)."""
    if not path:
        return {}
    p = Path(path)
    if p.stat().st_mode & 0o077:
        raise ValueError(f"{p.name}: the credentials file must be private (chmod 600)")
    data = json.loads(p.read_text())
    if not isinstance(data, dict) or not all(isinstance(v, dict) and set(v) <= {"username", "password"}
                                             for v in data.values()):
        raise ValueError(f"{p.name}: expected {{host: {{username, password}}}}")
    return data


class Sources:
    """Un client de registre par hôte source, identifiants du fichier."""

    def __init__(self, creds, plain_http=()):
        self.creds, self.plain, self.clients = creds, set(plain_http or ()), {}

    def registry(self, host):
        api = "registry-1.docker.io" if host == "docker.io" else host
        if api not in self.clients:
            c = self.creds.get(host) or self.creds.get(api)
            self.clients[api] = op.Registry(api, c, plain_http=host in self.plain or api in self.plain)
        return self.clients[api]

    def basic(self, url):
        c = self.creds.get(urllib.parse.urlparse(url).netloc)
        return op.Registry("x", c)._basic() if c else None


def _unquote(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def helm_index_entries(text, chart):
    """Les versions de `chart` d'un index.yaml de dépôt Helm : [{"version",
    "digest", "urls", ...}]. Lecture limitée à la forme qu'écrit `helm repo
    index` (blocs, listes en tirets), sans PyYAML ; JSON accepté."""
    if text.lstrip().startswith("{"):
        return list((json.loads(text).get("entries") or {}).get(chart) or [])
    found, in_entries, chart_indent, cur = [], False, None, None
    item, key_indent, block = None, None, None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        ind, s = len(raw) - len(raw.lstrip(" ")), raw.strip()
        if ind == 0:
            in_entries, cur = s == "entries:", None
            continue
        if not in_entries:
            continue
        if not s.startswith("-") and s.endswith(":") and (chart_indent is None or ind == chart_indent):
            chart_indent, cur, item, key_indent = ind, _unquote(s[:-1]), None, None
            continue
        if cur != chart:
            continue
        if s.startswith("- ") and (ind == key_indent - 2 if key_indent is not None else ind >= chart_indent):
            item, block = {}, None
            found.append(item)
            key_indent, s, ind = ind + 2, s[2:].strip(), ind + 2
        if item is None:
            continue
        if s.startswith("- ") and block:
            item.setdefault(block, []).append(_unquote(s[2:]))
            continue
        if ind == key_indent and ":" in s:
            k, _, v = s.partition(":")
            v = v.strip()
            if not v:
                block = k.strip()
            elif v.startswith("[") and v.endswith("]"):
                item[k.strip()] = [_unquote(x) for x in v[1:-1].split(",") if x.strip()]
                block = None
            else:
                item[k.strip()] = _unquote(v)
                block = None
    return found


def _fetch(url, dest, auth, cap, timeout=120):
    """GET en flux vers `dest` (ou en mémoire si `dest` est None), borné à
    `cap` octets ; rend (octets ou None, empreinte sha256)."""
    if urllib.parse.urlparse(url).scheme not in ("https", "http"):
        raise ValueError(f"{url}: https:// or http:// only")
    req = urllib.request.Request(url, headers={"Authorization": auth} if auth else {})
    h, n, buf = hashlib.sha256(), 0, bytearray()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            f = open(dest, "wb") if dest else None
            try:
                for b in iter(lambda: r.read(1 << 20), b""):
                    n += len(b)
                    if n > cap:
                        raise ValueError(f"{urllib.parse.urlparse(url).netloc}: reply larger than {cap} bytes")
                    h.update(b)
                    (f.write(b) if f else buf.extend(b))
            finally:
                if f:
                    f.close()
    except urllib.error.HTTPError as e:
        e.close()
        raise ValueError(f"GET {urllib.parse.urlparse(url).netloc}{urllib.parse.urlparse(url).path}: "
                         f"HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise ValueError(f"GET {urllib.parse.urlparse(url).netloc}: {e.reason}") from None
    return (None if dest else bytes(buf)), h.hexdigest()


def fetch_chart(sources, c, dest):
    """Le chart `c` ({repo, name, version}) écrit dans `dest` (.tgz) :
    artefact OCI (empreinte de la couche contrôlée) ou dépôt HTTP (empreinte
    de index.yaml contrôlée)."""
    repo = c["repo"].rstrip("/")
    if repo.startswith("oci://"):
        host, _, path = repo[len("oci://"):].partition("/")
        reg = sources.registry(host)
        rrepo, tag = f"{path}/{c['name']}" if path else c["name"], c["version"].replace("+", "_")
        try:
            body, _ = reg.manifest(rrepo, tag)
        except op.RegistryError as e:
            raise ValueError(f"chart {c['name']} {c['version']}: {e}") from None
        layer = next((x for x in json.loads(body).get("layers") or []
                      if x.get("mediaType") in (ol.M_HELM_CHART, "application/tar+gzip")), None)
        if not layer:
            raise ValueError(f"chart {c['name']} {c['version']}: {repo} holds no Helm chart under this name")
        reg.blob_to_file(rrepo, layer["digest"], dest, size=layer.get("size"))
        return
    index_url = repo + "/index.yaml"
    text, _ = _fetch(index_url, None, sources.basic(index_url), INDEX_MAX)
    entry = next((e for e in helm_index_entries(text.decode("utf-8", "replace"), c["name"])
                  if str(e.get("version")) == c["version"]), None)
    if entry is None:
        raise ValueError(f"chart {c['name']} {c['version']}: not in {index_url}")
    digest, urls = str(entry.get("digest") or ""), entry.get("urls") or []
    if not re.match(r"^[0-9a-f]{64}$", digest) or not urls:
        raise ValueError(f"chart {c['name']} {c['version']}: the repository index gives no sha256 digest or URL")
    url = urllib.parse.urljoin(index_url, urls[0])
    if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(index_url).netloc:
        auth = None                      # identifiants seulement pour l'hôte du dépôt
    else:
        auth = sources.basic(url)
    try:
        _, got = _fetch(url, dest, auth, CHART_MAX)
        if got != digest:
            raise ValueError(f"chart {c['name']} {c['version']}: the download does not match the repository index")
    except BaseException:
        Path(dest).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Construction du paquet
# ---------------------------------------------------------------------------
def write_package(root, out_path):
    """Archive de `root` (répertoire `<nom>-<version>`) : SHA256SUMS calculé
    puis écrit en premier membre (l'import vérifie en flux), le reste trié."""
    root = Path(root)
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != art.SUMS)
    sums = "".join(f"{ol.sha256_file(p)}  {p.relative_to(root).as_posix()}\n" for p in files)
    (root / art.SUMS).write_text(sums)

    def norm(ti):
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        ti.mode = 0o755 if ti.isdir() else 0o644
        return ti
    part = Path(str(out_path) + ".part")
    with tarfile.open(part, "w:gz") as t:
        t.add(root, arcname=root.name, recursive=False, filter=norm)
        t.add(root / art.SUMS, arcname=f"{root.name}/{art.SUMS}", filter=norm)
        for p in files:
            t.add(p, arcname=f"{root.name}/{p.relative_to(root).as_posix()}", filter=norm)
    os.replace(part, out_path)
    return {"files": len(files) + 1, "size": Path(out_path).stat().st_size}


def pack(desc, base_dir, out_dir, sources, platform=ol.DEFAULT_PLATFORM):
    """Construit le paquet de `desc` (description vérifiée) ; rend son
    chemin et son résumé."""
    top = f"{desc['name']}-{desc['version']}"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    build = Path(tempfile.mkdtemp(prefix=f".{top}.", dir=out_dir))
    try:
        root = build / top
        root.mkdir()
        index = {"name": desc["name"], "version": desc["version"], "description": desc["description"], "items": []}
        layout = ol.Layout(root / "images").init()
        wanted, copied, done = {}, {}, 0
        all_images = [im for it in desc["items"] for im in it.get("images") or []]
        for im in all_images:
            wanted[im["name"]] = im
        for n, (name, im) in enumerate(wanted.items(), 1):
            host = ol.normalize_ref(im["ref"])[0]
            step("images", "running", f"images {n}/{len(wanted)}: {name}")
            key = ol.full_name(im["ref"])
            if key not in copied:
                try:
                    copied[key] = ol.copy_image(sources.registry(host), im["ref"], layout, platform=platform)
                except op.RegistryError as e:
                    raise ValueError(f"image {im['ref']}: {e}") from None
            res = copied[key]
            if name != res["name"]:
                e = layout.find(res["name"])
                layout.add_entry({k: v for k, v in e.items() if k != "annotations"},
                                 ol.normalize_ref(name)[2] or "", name)
            done += res["size"]
        layout.index["manifests"] = [e for e in layout.entries()
                                     if (e.get("annotations") or {}).get(ol.A_NAME) in wanted]
        layout.write_index()
        if wanted:
            step("images", "done", f"{len(wanted)} image(s), {len(list((root / 'images/blobs/sha256').iterdir()))} "
                                   f"blob(s)")
        else:
            shutil.rmtree(root / "images")
        charts = [it for it in desc["items"] if it["kind"] == "chart"]
        for n, it in enumerate(charts, 1):
            c = it["chart"]
            step("charts", "running", f"charts {n}/{len(charts)}: {c['name']} {c['version']}")
            (root / "charts").mkdir(exist_ok=True)
            tgz = root / "charts" / f"{c['name']}-{c['version']}.tgz"
            fetch_chart(sources, c, tgz)
            meta = ol.chart_to_layout(tgz, ol.Layout(build / "chart-check"))     # Chart.yaml lisible
            shutil.rmtree(build / "chart-check")
            if (meta["name"], meta["version"]) != (c["name"], c["version"]):
                raise ValueError(f"chart {c['name']} {c['version']}: the archive holds "
                                 f"{meta['name']} {meta['version']}")
        if charts:
            step("charts", "done", f"{len(charts)} chart(s)")
        for it in desc["items"]:
            entry = {k: it[k] for k in ("name", "kind", "description", "values") if k in it}
            if it["kind"] == "chart":
                entry["version"] = it["chart"]["version"]
                entry["chart"] = f"charts/{it['chart']['name']}-{it['chart']['version']}.tgz"
            if it.get("images"):
                entry["images"] = list(dict.fromkeys(im["name"] for im in it["images"]))
            if it.get("files"):
                d = root / "manifests" / it["name"]
                d.mkdir(parents=True, exist_ok=True)
                entry["files"] = []
                for f in it["files"]:
                    src = Path(f) if Path(f).is_absolute() else Path(base_dir) / f
                    shutil.copyfile(src, d / Path(f).name)
                    entry["files"].append(f"manifests/{it['name']}/{Path(f).name}")
                step("manifests", "running", f"{it['name']}: {len(entry['files'])} file(s)")
            index["items"].append(entry)
        idx, errors = art.check_index(index, image_names=list(wanted))
        if errors:
            raise ValueError("index: " + "; ".join(errors[:5]))
        (root / art.INDEX).write_text(json.dumps(idx, indent=2) + "\n")
        step("package", "running", f"writing {top}.tar.gz")
        path = out_dir / f"{top}.tar.gz"
        res = write_package(root, path)
        step("package", "done", f"{top}.tar.gz, {res['size']} bytes")
        return path, {"name": desc["name"], "version": desc["version"], "package": path.name,
                      "images": len(wanted), "charts": len(charts), "items": len(index["items"]),
                      "size": res["size"]}
    finally:
        shutil.rmtree(build, ignore_errors=True)


def sign(package, key):
    """`ssh-keygen -Y sign` avec l'espace de noms des artefacts : écrit
    `<paquet>.sig`."""
    sig = Path(str(package) + ".sig")
    sig.unlink(missing_ok=True)
    try:
        r = subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", art.SIG_NAMESPACE, str(package)],
                           capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise ValueError("ssh-keygen is needed to sign") from None
    if r.returncode != 0 or not sig.is_file():
        raise ValueError(f"signing failed: {(r.stderr or '').strip()[:200]}")
    return sig


# ---------------------------------------------------------------------------
# Sous-commandes
# ---------------------------------------------------------------------------
def cmd_pack(args):
    try:
        doc = read_description(args.description)
    except (OSError, ValueError) as e:
        step("describe", "error", str(e))
        return EXIT_REFUSED
    desc, errors = check_description(doc)
    if errors:
        step("describe", "error", "; ".join(errors[:10]))
        out({"ok": False, "errors": errors})
        return EXIT_REFUSED
    try:
        sources = Sources(read_creds(args.creds), args.plain_http)
        path, res = pack(desc, Path(args.description).resolve().parent, args.out, sources, args.platform)
        if args.sign_key:
            step("sign", "running", path.name)
            sign(path, args.sign_key)
            step("sign", "done", f"{path.name}.sig")
            res["signed"] = True
    except (OSError, ValueError, op.RegistryError) as e:
        step("package", "error", str(e)[:400])
        return EXIT_FAIL
    out(dict(res, ok=True))
    return EXIT_OK


def cmd_sign(args):
    try:
        sig = sign(args.package, args.key)
    except ValueError as e:
        step("sign", "error", str(e))
        return EXIT_FAIL
    out({"ok": True, "signature": sig.name})
    return EXIT_OK


def _signers(args):
    if getattr(args, "signers", None):
        return args.signers
    p = art.signers_path(state_dir())
    return str(p) if p.is_file() else None


def _sig_of(package):
    sig = Path(str(package) + ".sig")
    return str(sig) if sig.is_file() else None


def cmd_verify(args):
    try:
        trust = art.verify_trust(args.package, _sig_of(args.package), _signers(args))
    except art.ArtefactError as e:
        step("verify", "error", str(e))
        return EXIT_FAIL
    out(trust)
    return EXIT_OK if trust["state"] == "valid" else EXIT_FAIL


def cmd_inspect(args):
    try:
        info = art.inspect_package(args.package)
        info["trust"] = art.verify_trust(args.package, _sig_of(args.package), _signers(args))
    except art.ArtefactError as e:
        step("inspect", "error", f"{e.code}: {e}")
        out({"ok": False, "code": e.code, "error": str(e)})
        return EXIT_REFUSED
    out(dict(info, ok=True))
    return EXIT_OK


def cmd_import(args):
    state = state_dir()
    ack = "unsigned" if args.accept_unsigned else "unknown-key" if args.accept_unknown_key else None
    person = args.person or getpass.getuser()
    step("check", "running", Path(args.package).name)
    try:
        info = art.inspect_package(args.package)
        trust = art.verify_trust(args.package, _sig_of(args.package), str(art.signers_path(state)))
        conf = art.settings(state, console_config())
        accepted, code = art.decide(trust, conf["strict"], ack)
        result = {"name": info["name"], "version": info["version"], "trust": trust, "code": code,
                  "strict": conf["strict"]}
        if not accepted:
            step("check", "error", f"{info['name']}-{info['version']}: {code}")
            out(dict(result, accepted=False))
            return EXIT_ACK if code.startswith("acknowledge-") and code != "acknowledge-invalid" else EXIT_REFUSED
        step("check", "done", f"{info['name']}-{info['version']}: {trust['state']}, "
                              f"{info['files']} file(s), {info['size']} bytes")
        dest = art.package_dir(state, info["name"], info["version"])
        if dest.exists():
            raise art.ArtefactError("exists", f"{info['name']}-{info['version']} is already imported "
                                              "(remove it first)")
        free = shutil.disk_usage(art.packages_dir(state) if art.packages_dir(state).is_dir() else state).free
        if free < info["size"] + (256 << 20):
            raise art.ArtefactError("no-room", f"{info['size']} bytes to extract, {free} free")
        step("extract", "running", f"{info['size']} bytes")
        res = art.extract_verified(args.package, dest)
        step("extract", "done", f"{res['files']} file(s), {res['bytes']} bytes, SHA256SUMS matches")
        art.write_trust(dest, trust, person, conf["strict"])
        sig = _sig_of(args.package)
        if sig:
            shutil.copyfile(sig, dest / Path(sig).name)
            os.chmod(dest / Path(sig).name, 0o600)
        line = art.journal_line(trust, info["name"], info["version"], person)
        step("trust", "done", line)
    except art.ArtefactError as e:
        step("extract", "error", f"{e.code}: {e}")
        out({"accepted": False, "code": e.code, "error": str(e)})
        return EXIT_REFUSED
    except OSError as e:
        step("extract", "error", str(e))
        return EXIT_FAIL
    out(dict(result, accepted=True, files=res["files"], bytes=res["bytes"], journal=line))
    return EXIT_OK


def cmd_list(args):
    out(art.list_packages(state_dir()))
    return EXIT_OK


def cmd_remove(args):
    try:
        art.remove_package(state_dir(), args.name, args.version)
    except art.ArtefactError as e:
        step("remove", "error", f"{e.code}: {e}")
        return EXIT_REFUSED
    step("remove", "done", f"{args.name}-{args.version}")
    return EXIT_OK


def parser():
    p = argparse.ArgumentParser(prog="harvester-artefacts", allow_abbrev=False,
                                description="Signed artefact packages: pack, sign, verify, import.")
    p.add_argument("--state-dir", help="console state directory (default HARVESTER_OPS_STATE_DIR)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pack", allow_abbrev=False, help="build a package from a description")
    s.add_argument("description")
    s.add_argument("--out", default=".")
    s.add_argument("--sign-key")
    s.add_argument("--creds", help="private JSON file {host: {username, password}}")
    s.add_argument("--platform", default=ol.DEFAULT_PLATFORM)
    s.add_argument("--plain-http", action="append", default=[], metavar="HOST",
                   help="registry spoken to in clear (test registries)")
    s.set_defaults(fn=cmd_pack)
    s = sub.add_parser("sign", allow_abbrev=False)
    s.add_argument("package")
    s.add_argument("--key", required=True)
    s.set_defaults(fn=cmd_sign)
    for name, fn in (("verify", cmd_verify), ("inspect", cmd_inspect)):
        s = sub.add_parser(name, allow_abbrev=False)
        s.add_argument("package")
        s.add_argument("--signers")
        s.set_defaults(fn=fn)
    s = sub.add_parser("import", allow_abbrev=False)
    s.add_argument("package")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--accept-unsigned", action="store_true")
    g.add_argument("--accept-unknown-key", action="store_true")
    s.add_argument("--person")
    s.set_defaults(fn=cmd_import)
    s = sub.add_parser("list", allow_abbrev=False)
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("remove", allow_abbrev=False)
    s.add_argument("name")
    s.add_argument("version")
    s.set_defaults(fn=cmd_remove)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.state_dir:
        os.environ["HARVESTER_OPS_STATE_DIR"] = os.path.abspath(args.state_dir)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
