"""harvester-ops : cluster enfant amorcé entièrement sans Internet (2.8.0).

Décision 191, point 237. RKE2 lui-même et ses images système viennent du
registre :

- sorte de paquet `rke2` : `version` RKE2 exacte (`v1.34.11+rke2r1`),
  `files` `rke2/<version>/{rke2.linux-amd64.tar.gz, sha256sum-amd64.txt,
  install.sh}`, `images` = images système de cette version
  (`rke2-images-all.linux-amd64.txt` filtrée par la CNI choisie) ;
- publication : chaque fichier en blob du dépôt `<registre>/harvester-ops/rke2`,
  manifeste d'artefact (type `application/vnd.harvester-ops.rke2.v1+json`)
  étiqueté par la version, le `+` (interdit dans une étiquette OCI)
  remplacé par `_` ;
- option du cluster `offline: true` : dans les deux gabarits RKE2, fichier
  `/etc/rancher/rke2/hops-fetch.netrc` (0600, compte de lecture, depuis un
  Secret par `contentFrom`), `preRKE2Commands` qui récupèrent les trois blobs
  par empreinte avec `curl --netrc-file ... --cacert ...`, vérifient chaque
  empreinte, et ne posent `/opt/install.sh` qu'ensuite, puis
  `agentConfig.airGapped: true` : CAPRKE2 lance alors `/opt/install.sh` sur
  `/opt/rke2-artifacts` (champ relevé dans CAPRKE2 v1beta2, essai 7 de la
  tâche 1 ; `curl` 8.22 et `sha256sum` présents dans l'image Leap 15.6).
  `offline_artifacts: image` : l'image de VM porte déjà ces fichiers, aucune
  récupération.

Les commandes ne portent aucun identifiant : le fichier netrc vient d'un
Secret. Bibliothèque standard seulement.

2.8.1 (décision 241) : cloud-init sans réseau. CAPHV v0.10.1 fusionne dans
le cloud-init de chaque machine une base à lui (`package_update: true`,
`packages: [qemu-guest-agent, iptables]`, harvestermachine_controller.go,
buildVMTemplate) ; sans Internet, zypper attendait chaque dépôt de l'image
(environ 5 minutes chacun, R11 de 2.8.0) avant les commandes de RKE2.
`user_data()` est posé dans `agentConfig.additionalUserData.config` des
deux gabarits (CAPRKE2 l'ajoute à la fin de son cloud-config) : clés de
paquets à false (CAPHV fusionne les clés simples « la dernière gagne », et
les données de CAPRKE2 passent en dernier, util/cloudinit.go) et `bootcmd`
qui met de côté dépôts et services zypper une fois par instance : la liste
`packages` est une section concaténée qu'on ne peut pas retirer, et
l'installation rafraîchirait les dépôts. `repos_back()` les remet en place
en tête des commandes sans Internet.
"""

import json
import re

VERSION_RE = re.compile(r"^v1\.(?P<minor>\d{2})\.(?P<patch>\d{1,3})\+rke2r(?P<rel>\d{1,2})$")
FILES = ("rke2.linux-amd64.tar.gz", "sha256sum-amd64.txt", "install.sh")
REPO = "harvester-ops/rke2"
ARTIFACT_TYPE = "application/vnd.harvester-ops.rke2.v1+json"
LAYER_TYPE = "application/octet-stream"
A_TITLE = "org.opencontainers.image.title"
ARTIFACTS_DIR = "/opt/rke2-artifacts"
INSTALL = "/opt/install.sh"
NETRC = "/etc/rancher/rke2/hops-fetch.netrc"
CA_FILE = "/etc/rancher/rke2/hops-fetch-ca.crt"
# images d'une CNI dans rke2-images-all : mots relevés dans la liste de
# v1.34.11+rke2r1 (rke2-calico, rke2-cilium, rke2-flannel...) ; une image
# qui porte le mot d'une autre CNI est retirée
CNI_WORDS = {"calico": ("calico", "tigera"), "canal": ("calico", "flannel"), "cilium": ("cilium",),
             "flannel": ("flannel",), "none": ()}
DOWNLOAD = "https://github.com/rancher/rke2/releases/download"
# 2.8.1 : dépôts et services zypper mis de côté pendant le premier démarrage
ZYPP_DIRS = ("/etc/zypp/repos.d", "/etc/zypp/services.d")
ASIDE = ".harvester-ops-offline"
# Relecture I-2 : `skip` met les dépôts de côté (défaut), `keep` les laisse
OFFLINE_PACKAGES = ("skip", "keep")
# Ce que CAPHV v0.10.1 demande à cloud-init (cloudInitBase), que l'image
# doit porter en mode `skip` (constat offline-node-packages)
NODE_PACKAGES = ("iptables", "qemu-guest-agent")


def finding(code, level="block", **facts):
    return {"code": code, "level": level, "facts": facts}


def tag_of(version):
    """Étiquette OCI de la version (`+` interdit dans une étiquette)."""
    return version.replace("+", "_")


def k8s_of(version):
    """`v1.34.11` de `v1.34.11+rke2r1`, ou None."""
    m = VERSION_RE.match(version or "")
    return f"v1.{m.group('minor')}.{m.group('patch')}" if m else None


def files_of(version):
    return [f"rke2/{version}/{f}" for f in FILES]


def check_item(entry):
    """[erreur] d'un élément `rke2` d'un index : version exacte, les trois
    fichiers et eux seuls, des images."""
    errors = []
    v = entry.get("version")
    if not isinstance(v, str) or not VERSION_RE.match(v):
        return ["version: an exact RKE2 version such as v1.34.11+rke2r1"]
    if sorted(entry.get("files") or []) != sorted(files_of(v)):
        errors.append(f"files: rke2/{v}/{{{', '.join(FILES)}}}, and these only")
    images = entry.get("images")
    if not isinstance(images, list) or not images or not all(isinstance(x, str) and x for x in images):
        errors.append("images: the system images of this version, one at least")
    return errors


def images_for(text, cni="calico"):
    """Images système d'une version (`rke2-images-all.linux-amd64.txt`) pour
    la CNI choisie : les images d'une autre CNI retirées, l'ordre gardé."""
    if cni not in CNI_WORDS:
        raise ValueError(f"cni: one of {', '.join(CNI_WORDS)}")
    keep = set(CNI_WORDS[cni])
    other = {w for ws in CNI_WORDS.values() for w in ws} - keep
    out = []
    for line in (text or "").splitlines():
        ref = line.strip()
        if not ref or ref.startswith("#"):
            continue
        if any(w in ref for w in other):
            continue
        if ref not in out:
            out.append(ref)
    return out


def download_urls(version):
    """{fichier: adresse} des fichiers d'une version (côté connecté) ;
    `install.sh` vient du site officiel de RKE2."""
    from urllib.parse import quote
    base = f"{DOWNLOAD}/{quote(version)}"
    return {"rke2.linux-amd64.tar.gz": f"{base}/rke2.linux-amd64.tar.gz",
            "sha256sum-amd64.txt": f"{base}/sha256sum-amd64.txt",
            "rke2-images-all.linux-amd64.txt": f"{base}/rke2-images-all.linux-amd64.txt",
            "install.sh": "https://get.rke2.io"}


def manifest(digests, sizes):
    """Manifeste OCI de l'artefact (`{fichier: sha256:...}`, `{fichier:
    octets}`) : configuration vide, une couche par fichier, nommée."""
    import json
    empty = b"{}"
    import hashlib
    return json.dumps({
        "schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "artifactType": ARTIFACT_TYPE,
        "config": {"mediaType": "application/vnd.oci.empty.v1+json",
                   "digest": "sha256:" + hashlib.sha256(empty).hexdigest(), "size": len(empty)},
        "layers": [{"mediaType": LAYER_TYPE, "digest": digests[f], "size": sizes[f],
                    "annotations": {A_TITLE: f}} for f in FILES]},
        sort_keys=True, separators=(",", ":")).encode()


def netrc(host, user, password):
    """Contenu du fichier netrc des nœuds (rendu dans un Secret, jamais en
    argument) : curl compare le nom d'hôte sans le port."""
    machine = host.rsplit(":", 1)[0] if re.match(r"^[^:]+:\d+$", host) else host
    for v in (user, password):
        if not v or any(c.isspace() for c in v):
            raise ValueError("netrc: the read account has no spaces")
    return f"machine {machine} login {user} password {password}\n"


def pre_commands(registry_host, repo, digests, ca_path=CA_FILE, netrc_path=NETRC):
    """`preRKE2Commands` : récupération des trois blobs par empreinte, chacun
    vérifié ; `install.sh` n'est posé à `/opt/install.sh` (que CAPRKE2 lance)
    qu'après la vérification de tous. Une empreinte fausse efface tout et
    échoue : l'installation ne part pas. Aucun identifiant ici."""
    def url(f):
        return f"https://{registry_host}/v2/{repo}/blobs/{digests[f]}"
    curl = f"curl --fail --silent --show-error --location --retry 5 --netrc-file {netrc_path}" \
        + (f" --cacert {ca_path}" if ca_path else "")
    out = [f"mkdir -p {ARTIFACTS_DIR}", f"rm -f {INSTALL}"]
    dest = {"rke2.linux-amd64.tar.gz": f"{ARTIFACTS_DIR}/rke2.linux-amd64.tar.gz",
            "sha256sum-amd64.txt": f"{ARTIFACTS_DIR}/sha256sum-amd64.txt",
            "install.sh": f"{ARTIFACTS_DIR}/install.sh.part"}
    for f in FILES:
        out.append(f"{curl} -o {dest[f]} {url(f)}")
    check = " && ".join(f"echo '{digests[f].split(':', 1)[1]}  {dest[f]}' | sha256sum -c -" for f in FILES)
    out.append(f"( {check} && cd {ARTIFACTS_DIR} && sha256sum -c --ignore-missing sha256sum-amd64.txt ) || "
               f"{{ rm -rf {ARTIFACTS_DIR} {INSTALL}; echo 'harvester-ops: RKE2 artefacts do not match their "
               "digests' >&2; exit 1; }")
    out.append(f"install -m 0700 {dest['install.sh']} {INSTALL} && rm -f {dest['install.sh']}")
    return out


def published_rke2(packages):
    """[{version, package, item}] des éléments `rke2` de paquets (index)."""
    out = []
    for pkg in packages or []:
        for it in (pkg.get("index") or {}).get("items") or []:
            if it.get("kind") == "rke2":
                out.append({"version": it.get("version"), "package": pkg["index"].get("name"),
                            "pkg_version": pkg["index"].get("version"), "item": it.get("name")})
    return out


def findings(spec, published, entries):
    """Constats de l'option `offline` d'une demande : `published` =
    {registre: [éléments rke2 publiés]} pour les registres de la demande,
    `entries` = registres normalisés de la demande avec la déclaration
    (`decl`) de chacun. `offline-needs-no-fallback` : aucun registre ne
    miroite docker.io avec le repli coupé ; `offline-rke2-missing`,
    `offline-rke2-version-mismatch` (mode `fetch` seulement)."""
    if not spec.get("offline"):
        return []
    out = []
    cut = any(e.get("no_fallback") for e in entries or [])
    mirrors_docker = any("docker.io" in ((e.get("decl") or {}).get("mirrors") or [])
                         and "images" in ((e.get("decl") or {}).get("usage") or [])
                         for e in entries or [])
    if not (cut and mirrors_docker):
        out.append(finding("offline-needs-no-fallback"))
    if spec.get("offline_artifacts") == "image":
        return out
    items = [dict(x, registry=r) for r, xs in (published or {}).items() for x in xs or []]
    if not items:
        out.append(finding("offline-rke2-missing", version=spec.get("k8s_version")))
    elif not any(k8s_of(x.get("version")) == spec.get("k8s_version") for x in items):
        out.append(finding("offline-rke2-version-mismatch", version=spec.get("k8s_version"),
                           published=", ".join(sorted({x.get("version") or "?" for x in items}))))
    return out


def chosen(spec, published):
    """(registre, élément rke2) pour la version de la demande, ou None."""
    for r, xs in (published or {}).items():
        for x in xs or []:
            if k8s_of(x.get("version")) == spec.get("k8s_version"):
                return r, x
    return None


def template_patch(registry_host, digests, netrc_secret, ca=True):
    """Ce que l'option pose dans chaque gabarit RKE2 : fichiers (netrc 0600
    depuis le Secret `netrc_secret`, CA du registre), commandes, `airGapped`."""
    files = [{"path": NETRC, "owner": "root:root", "permissions": "0600",
              "contentFrom": {"secret": {"name": netrc_secret, "key": "netrc"}}}]
    if ca:
        files.append({"path": CA_FILE, "owner": "root:root", "permissions": "0644",
                      "contentFrom": {"secret": {"name": netrc_secret, "key": "ca.crt"}}})
    return {"files": files, "preRKE2Commands": pre_commands(registry_host, REPO, digests, CA_FILE if ca else None)}


def repos_aside(dirs=ZYPP_DIRS):
    """Ligne de shell : chaque répertoire de `dirs` renommé en `<rép>` +
    ASIDE et remplacé par un répertoire vide ; une copie déjà de côté n'est
    jamais écrasée, un répertoire absent est ignoré."""
    return (f"for d in {' '.join(dirs)}; do if [ -d \"$d\" ] && [ ! -e \"$d{ASIDE}\" ]; then "
            f"mv \"$d\" \"$d{ASIDE}\" && mkdir -m 0755 \"$d\"; fi; done")


def repos_back(dirs=ZYPP_DIRS):
    """Ligne de shell : l'inverse de `repos_aside`, seulement si le
    répertoire mis à la place est resté vide (rien posé entre-temps).
    Relecture M-2 : une ligne sur stderr pour chaque répertoire (remis,
    rien de côté, copie laissée de côté), lue dans cloud-init-output.log."""
    return (f"for d in {' '.join(dirs)}; do if [ -d \"$d{ASIDE}\" ]; then "
            f"if [ ! -e \"$d\" ] || rmdir \"$d\" 2>/dev/null; then mv \"$d{ASIDE}\" \"$d\" && "
            f"echo \"harvester-ops: zypper repositories restored in $d\" >&2; "
            f"else echo \"harvester-ops: $d is not empty, repositories left in $d{ASIDE}\" >&2; fi; "
            f"else echo \"harvester-ops: nothing set aside for $d\" >&2; fi; done")


def user_data(packages="skip"):
    """`agentConfig.additionalUserData.config` d'un cluster sans Internet :
    aucune mise à jour de paquets (les deux modes). `skip` : dépôts zypper
    de côté au premier démarrage (`cloud-init-per instance` : bootcmd tourne
    à chaque démarrage, la remise en place une seule fois), l'installation
    de la liste de CAPHV échoue tout de suite. `keep` (relecture I-2) : dépôts
    laissés en place, pour une image dont les dépôts visent un miroir du
    réseau local (RMT, MLM) ; cloud-init installe alors la liste de CAPHV."""
    if packages not in OFFLINE_PACKAGES:
        raise ValueError(f"offline_packages: {packages!r}, expected skip or keep")
    text = "package_update: false\npackage_upgrade: false\npackage_reboot_if_required: false\n"
    if packages == "keep":
        return text
    entry = ["cloud-init-per", "instance", "harvester-ops-repos-aside", "sh", "-c", repos_aside()]
    return text + f"bootcmd:\n- {json.dumps(entry)}\n"
