"""harvester-ops : petit client vSphere pour la source d'une vague (v1.76.0).

Deux usages de harvester-forklift : rallumer la VM source (retour arrière
après une bascule) et retirer les instantanés que Forklift laisse sur la
source après un essai raté.

Ce que vCenter 8.0 offre réellement (vu sur le banc vmwlab) :

- l'API REST (`/api`) sait l'alimentation : session par Basic, jeton rendu
  en chaîne JSON, en-tête `vmware-api-session-id` ;
- l'API REST n'a PAS d'instantanés (`/api/vcenter/vm/<vm>/snapshots` rend
  404) : ils passent par SOAP (`/sdk`, vim25) : RetrieveServiceContent,
  SessionManager.Login (cookie `vmware_soap_session`), PropertyCollector
  .RetrievePropertiesEx sur `snapshot`, RemoveSnapshot_Task, `info.state`
  de la tâche, Logout ;
- Forklift nomme ses instantanés `forklift-migration-precopy`, chacun enfant
  du précédent. On ne retire QUE ceux-là, des feuilles vers la racine, avec
  removeChildren=false : un instantané de l'exploitant n'est jamais touché
  (s'il est enfant d'un instantané de Forklift, vSphere le rattache au
  parent).

Aucun identifiant, mot de passe, jeton ni chemin de fichier dans un message
d'erreur : toute panne devient une VSphereError courte qui nomme l'hôte et
l'opération. Bibliothèque standard seulement.
"""

import base64
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

FORKLIFT_SNAPSHOT = "forklift-migration-precopy"

# Délai d'attente de la tâche d'allumage : vu sur vCenter 8.0.1 (banc
# vmwlab), un démarrage peut prendre plus d'une minute sans être en panne.
POWER_ON_TIMEOUT = 300
# Lecture de la tâche d'allumage et de la question VMware pendant l'attente.
POWER_ON_POLL = 2.0
# Une question VMware est un texte libre, parfois long (chemins, explications) :
# on la coupe pour un message d'étape.
QUESTION_TEXT_MAX = 300

NS_VIM = "urn:vim25"
NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_V = "{%s}" % NS_VIM

# Identifiant d'objet géré (vm-16, snapshot-1001, task-42) : validé avant
# d'entrer dans une URL ou un document XML.
MOREF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")

TASK_DONE = ("success", "error")


class VSphereError(Exception):
    """`kind` : type de la faute vim25 quand il y en a une (InvalidPowerState...)."""
    kind = ""


class VSphereQuestion(VSphereError):
    """VMware attend une réponse avant de laisser démarrer la VM ; `question`
    porte {id, text, choices}."""

    def __init__(self, msg, question):
        super().__init__(msg)
        self.question = question


def forklift_snapshots(tree):
    """Identifiants des instantanés de Forklift d'un arbre (liste rendue par
    `VSphere.snapshots`), des feuilles vers la racine (parcours postfixe) :
    retirer dans cet ordre ne supprime jamais un parent avant ses enfants."""
    out = []

    def walk(nodes):
        for n in nodes or ():
            walk(n.get("children"))
            if n.get("name") == FORKLIFT_SNAPSHOT:
                out.append(n["id"])

    walk(tree)
    return out


def _check_moref(value, what):
    if not isinstance(value, str) or not MOREF_RE.match(value):
        raise VSphereError(f"invalid {what} identifier")
    return value


def _ssl_context(cacert, insecure):
    """Contexte TLS : non vérifié si demandé, sinon l'autorité donnée (texte
    PEM ou fichier), sinon le magasin du système."""
    if insecure:
        return ssl._create_unverified_context()
    try:
        if cacert and "-----BEGIN" in cacert:
            return ssl.create_default_context(cadata=cacert)
        if cacert:
            return ssl.create_default_context(cafile=cacert)
    except (OSError, ssl.SSLError, ValueError):
        raise VSphereError("unreadable CA certificate") from None
    return ssl.create_default_context()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


class VSphere:
    """Client minimal d'un vCenter : REST pour l'alimentation, SOAP pour les
    instantanés. `url` peut être celle du fournisseur Forklift
    (`https://vcenter/sdk`) : seuls le schéma et l'hôte comptent."""

    def __init__(self, url, user, password, cacert=None, insecure=False, timeout=60,
                 power_on_timeout=POWER_ON_TIMEOUT):
        u = urllib.parse.urlparse(str(url or "").strip())
        if u.scheme not in ("http", "https") or not u.netloc:
            raise VSphereError("vCenter URL must be http(s)://host")
        self.host = u.hostname or u.netloc
        self.base = f"{u.scheme}://{u.netloc}"
        self.user = user or ""
        self.password = password or ""
        self.timeout = timeout
        self.power_on_timeout = power_on_timeout
        self.power_on_poll = POWER_ON_POLL
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context(cacert, insecure)))
        self.token = None
        self.cookie = None
        self.content = None
        self.soap_action = NS_VIM
        # Injectables pour les tests (attente d'une tâche).
        self.sleep = time.sleep
        self.clock = time.monotonic

    # -- utilitaires -----------------------------------------------------

    def _scrub(self, text):
        """Un message de vCenter ne doit jamais renvoyer l'identifiant ou le
        mot de passe : on les masque, par précaution."""
        text = str(text or "").strip()[:200]
        for secret in (self.password, self.user):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _err(self, what, detail):
        return VSphereError(f"vCenter {self.host}: {what}: {self._scrub(detail)}")

    def _open(self, req, what, timeout=None):
        """(code, corps). Une erreur HTTP rend son code et son corps ; une
        panne réseau ou TLS devient une VSphereError sans URL."""
        try:
            with self.opener.open(req, timeout=timeout if timeout is not None else self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                reason = "certificate not trusted"
            raise self._err(what, f"unreachable ({reason})") from None
        except TimeoutError:
            raise self._err(what, "unreachable (TimeoutError)") from None
        except (OSError, ValueError) as e:
            raise self._err(what, f"unreachable ({type(e).__name__})") from None

    # -- REST : session et alimentation ------------------------------------

    def session(self):
        """Ouvre une session REST (Basic) et garde son jeton."""
        raw = f"{self.user}:{self.password}".encode()
        req = urllib.request.Request(self.base + "/api/session", data=b"", method="POST",
                                     headers={"Authorization": "Basic " + base64.b64encode(raw).decode()})
        code, body = self._open(req, "session")
        if code in (401, 403):
            raise self._err("session", f"refused (HTTP {code})")
        if code not in (200, 201):
            raise self._err("session", f"HTTP {code}")
        try:
            token = json.loads(body)
        except ValueError:
            token = None
        if not isinstance(token, str) or not token:
            raise self._err("session", "no session token in the answer")
        self.token = token
        return True

    def _rest(self, method, path, what, timeout=None):
        """Appel REST authentifié ; un jeton expiré (401) fait rouvrir la
        session une fois. Rend (code, JSON ou None)."""
        for attempt in (0, 1):
            if not self.token:
                self.session()
            req = urllib.request.Request(self.base + path, method=method,
                                         data=b"" if method == "POST" else None,
                                         headers={"vmware-api-session-id": self.token,
                                                  "Accept": "application/json"})
            code, body = self._open(req, what, timeout=timeout)
            if code == 401 and attempt == 0:
                self.token = None
                continue
            break
        try:
            data = json.loads(body) if body else None
        except ValueError:
            data = None
        return code, data

    @staticmethod
    def _rest_error_type(data):
        if isinstance(data, dict):
            return str(data.get("error_type") or data.get("type") or "")
        return ""

    @staticmethod
    def _rest_message(data):
        if isinstance(data, dict):
            for m in data.get("messages") or ():
                if isinstance(m, dict) and m.get("default_message"):
                    return m["default_message"]
            return VSphere._rest_error_type(data)
        return ""

    def power_state(self, vm_id):
        """POWERED_ON, POWERED_OFF ou SUSPENDED."""
        vm = _check_moref(vm_id, "VM")
        what = f"power state of {vm}"
        code, data = self._rest("GET", f"/api/vcenter/vm/{vm}/power", what)
        if code == 404:
            raise self._err(what, "VM not found")
        if code != 200 or not isinstance(data, dict) or not data.get("state"):
            raise self._err(what, f"HTTP {code} {self._rest_message(data)}")
        return data["state"]

    def power_on(self, vm_id):
        """Allume la VM. Déjà allumée : rien à faire, pas une erreur (le
        retour arrière doit pouvoir être rejoué). Rend True si la VM a été
        allumée par cet appel, False si elle l'était déjà ; lève
        VSphereQuestion si VMware attend une réponse pour la démarrer.

        Par la tâche SOAP PowerOnVM_Task, surveillée, et non par l'API REST.
        Vu sur vCenter 8.0.1 (banc vmwlab) : quand VMware pose une question à
        la mise sous tension (fichier du port série déjà présent), l'appel
        REST reste bloqué tant que personne ne répond ; sur le banc l'ESXi a
        répondu tout seul au bout de 4 min, ailleurs l'appel finissait en
        « injoignable ». Et relancé pendant la question, il répond « déjà
        allumée » alors que la VM est éteinte. La tâche rend la main tout de
        suite ; on lit son état et la question toutes les
        `power_on_poll` secondes, jusqu'à `power_on_timeout`."""
        vm = _check_moref(vm_id, "VM")
        what = f"power on {vm}"
        if self.power_state(vm) == "POWERED_ON":
            return False
        self._raise_question(vm, what)        # une question restée d'un essai précédent
        try:
            resp = self._soap('<PowerOnVM_Task xmlns="urn:vim25">'
                              f'<_this type="VirtualMachine">{escape(vm)}</_this>'
                              '</PowerOnVM_Task>', what)
        except VSphereError as e:
            if e.kind == "InvalidPowerState":
                self._raise_question(vm, what)
                return False
            raise
        task = resp.findtext(f"{_V}returnval")
        if not task or not MOREF_RE.match(task):
            raise self._err(what, "no task in the answer")
        deadline = self.clock() + self.power_on_timeout
        while True:
            state, kind, msg = self._task_info(task, what)
            if state == "success":
                return True
            if state == "error":
                if kind == "InvalidPowerState":
                    self._raise_question(vm, what)
                    return False
                raise self._err(what, f"failed: {msg or kind or 'no reason given'}")
            self._raise_question(vm, what)
            if self.clock() >= deadline:
                raise self._err(what, f"still {state or 'starting'} after {int(self.power_on_timeout)} s")
            self.sleep(self.power_on_poll)

    def _raise_question(self, vm, what):
        q = self.pending_question(vm)
        if q:
            e = VSphereQuestion(f"vCenter {self.host}: {what}: VMware waits for an answer: "
                                f"{self._scrub(q['text'])}", q)
            raise e

    def logout(self):
        """Ferme la session REST ; au mieux, une panne ici est ignorée."""
        if not self.token:
            return
        req = urllib.request.Request(self.base + "/api/session", method="DELETE",
                                     headers={"vmware-api-session-id": self.token})
        try:
            self._open(req, "logout")
        except VSphereError:
            pass
        self.token = None

    # -- SOAP : session, instantanés, tâches ---------------------------------

    def _soap(self, body, what):
        """Envoie un corps vim25, rend l'élément réponse (premier enfant du
        Body). Une faute SOAP devient une VSphereError nommée par son type."""
        doc = ('<?xml version="1.0" encoding="UTF-8"?>'
               f'<soapenv:Envelope xmlns:soapenv="{NS_SOAP}" xmlns:xsi="{NS_XSI}">'
               f'<soapenv:Body>{body}</soapenv:Body></soapenv:Envelope>').encode()
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": self.soap_action}
        if self.cookie:
            headers["Cookie"] = self.cookie
        req = urllib.request.Request(self.base + "/sdk", data=doc, method="POST", headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                code, raw, set_cookie = r.status, r.read(), r.headers.get("Set-Cookie")
        except urllib.error.HTTPError as e:
            try:
                code, raw, set_cookie = e.code, e.read(), None
            finally:
                e.close()
        except urllib.error.URLError as e:
            reason = e.reason
            if isinstance(reason, ssl.SSLCertVerificationError):
                reason = "certificate not trusted"
            raise self._err(what, f"unreachable ({reason})") from None
        except (OSError, ValueError) as e:
            raise self._err(what, f"unreachable ({type(e).__name__})") from None
        if set_cookie and "vmware_soap_session" in set_cookie:
            self.cookie = set_cookie.split(";", 1)[0].strip()
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise self._err(what, f"HTTP {code}, not a SOAP answer") from None
        sbody = root.find(f"{{{NS_SOAP}}}Body")
        if sbody is None or not len(sbody):
            raise self._err(what, f"HTTP {code}, empty SOAP answer")
        first = sbody[0]
        if _local(first.tag) == "Fault":
            raise self._fault(first, what)
        if code != 200:
            raise self._err(what, f"HTTP {code}")
        return first

    def _fault(self, fault, what):
        kind = ""
        detail = fault.find("detail")
        if detail is not None and len(detail):
            d = detail[0]
            kind = d.get(f"{{{NS_XSI}}}type") or _local(d.tag)
            if kind.endswith("Fault"):
                kind = kind[:-5]
        msg = fault.findtext("faultstring") or ""
        if kind == "InvalidLogin":
            return self._err(what, "login refused")
        if kind == "NotAuthenticated":
            return self._err(what, "not authenticated (session expired)")
        if kind == "NoPermission":
            return self._err(what, "permission denied")
        if kind == "ManagedObjectNotFound":
            e = self._err(what, "object not found")
        else:
            e = self._err(what, f"{kind or 'fault'}: {msg}")
        e.kind = kind
        return e

    def soap_login(self):
        """RetrieveServiceContent puis Login ; garde le cookie de session."""
        sc = self._soap('<RetrieveServiceContent xmlns="urn:vim25">'
                        '<_this type="ServiceInstance">ServiceInstance</_this>'
                        '</RetrieveServiceContent>', "service content")
        rv = sc.find(f"{_V}returnval")
        if rv is None:
            raise self._err("service content", "unreadable answer")
        self.content = {
            "sessionManager": rv.findtext(f"{_V}sessionManager"),
            "propertyCollector": rv.findtext(f"{_V}propertyCollector"),
            "apiVersion": rv.findtext(f"{_V}about/{_V}apiVersion"),
        }
        if not self.content["sessionManager"] or not self.content["propertyCollector"]:
            raise self._err("service content", "no session manager or property collector")
        if self.content["apiVersion"] and re.match(r"^[0-9][0-9.]{0,15}$", self.content["apiVersion"]):
            self.soap_action = f"{NS_VIM}/{self.content['apiVersion']}"
        self._soap('<Login xmlns="urn:vim25">'
                   f'<_this type="SessionManager">{escape(self.content["sessionManager"])}</_this>'
                   f'<userName>{escape(self.user)}</userName>'
                   f'<password>{escape(self.password)}</password>'
                   '</Login>', "login")
        if not self.cookie:
            raise self._err("login", "no session cookie in the answer")
        return True

    def _ensure_soap(self):
        if not self.cookie or not self.content:
            self.soap_login()

    def _retrieve(self, obj_type, obj_id, paths, what):
        """RetrievePropertiesEx d'un objet : {chemin: élément val}."""
        self._ensure_soap()
        path_xml = "".join(f"<pathSet>{escape(p)}</pathSet>" for p in paths)
        resp = self._soap(
            '<RetrievePropertiesEx xmlns="urn:vim25">'
            f'<_this type="PropertyCollector">{escape(self.content["propertyCollector"])}</_this>'
            f'<specSet><propSet><type>{obj_type}</type>{path_xml}</propSet>'
            f'<objectSet><obj type="{obj_type}">{escape(obj_id)}</obj></objectSet></specSet>'
            '<options/></RetrievePropertiesEx>', what)
        out = {}
        for ps in resp.iter(f"{_V}propSet"):
            name = ps.findtext(f"{_V}name")
            val = ps.find(f"{_V}val")
            if name and val is not None:
                out[name] = val
        return out

    @staticmethod
    def _tree(elements):
        nodes = []
        for el in elements:
            nodes.append({
                "id": el.findtext(f"{_V}snapshot") or "",
                "name": el.findtext(f"{_V}name") or "",
                "created": el.findtext(f"{_V}createTime") or "",
                "children": VSphere._tree(el.findall(f"{_V}childSnapshotList")),
            })
        return nodes

    def snapshots(self, vm_id):
        """Arbre des instantanés d'une VM : [{id, name, created, children}],
        liste vide si elle n'en a aucun."""
        vm = _check_moref(vm_id, "VM")
        props = self._retrieve("VirtualMachine", vm, ["snapshot"], f"snapshots of {vm}")
        val = props.get("snapshot")
        if val is None:
            return []
        return self._tree(val.findall(f"{_V}rootSnapshotList"))

    def pending_question(self, vm_id):
        """Question que VMware pose avant de laisser tourner la VM
        (`runtime.question`, SOAP seulement : l'API REST n'en dit rien), ou
        None. Vu sur le banc : une source rallumée par le retour arrière
        s'arrête sur « le fichier du port série existe déjà », l'API REST
        la dit POWERED_ON et l'invité ne démarre pas tant qu'on ne répond
        pas. Rend {id, text, choices} ; le texte est ramené sur une ligne."""
        vm = _check_moref(vm_id, "VM")
        props = self._retrieve("VirtualMachine", vm, ["runtime.question"], f"question of {vm}")
        val = props.get("runtime.question")
        if val is None:
            return None
        text = " ".join((val.findtext(f"{_V}text") or "").split())
        if len(text) > QUESTION_TEXT_MAX:
            text = text[:QUESTION_TEXT_MAX - 3].rstrip() + "..."
        # vu sur vCenter 8.0.1 : `label` est une clé interne
        # (« button.serial.file.append »), le libellé lisible est `summary`
        choices = [ci.findtext(f"{_V}summary") or ci.findtext(f"{_V}label") or ci.findtext(f"{_V}key") or ""
                   for ci in val.iter(f"{_V}choiceInfo")]
        return {"id": (val.findtext(f"{_V}id") or "").strip(), "text": text, "choices": [c for c in choices if c]}

    def remove_snapshot(self, snap_id, consolidate=True):
        """Lance RemoveSnapshot_Task (sans les enfants) ; rend l'identifiant
        de la tâche."""
        snap = _check_moref(snap_id, "snapshot")
        self._ensure_soap()
        what = f"remove snapshot {snap}"
        resp = self._soap('<RemoveSnapshot_Task xmlns="urn:vim25">'
                          f'<_this type="VirtualMachineSnapshot">{escape(snap)}</_this>'
                          '<removeChildren>false</removeChildren>'
                          f'<consolidate>{"true" if consolidate else "false"}</consolidate>'
                          '</RemoveSnapshot_Task>', what)
        task = resp.findtext(f"{_V}returnval")
        if not task or not MOREF_RE.match(task):
            raise self._err(what, "no task in the answer")
        return task

    def _task_info(self, task, what):
        """(état, type de faute, message) d'une tâche."""
        props = self._retrieve("Task", task, ["info.state", "info.error"], what)
        state = (props["info.state"].text or "").strip() if "info.state" in props else ""
        kind = msg = ""
        err = props.get("info.error")
        if err is not None:
            msg = err.findtext(f"{_V}localizedMessage") or ""
            f = err.find(f"{_V}fault")
            kind = (f.get(f"{{{NS_XSI}}}type") if f is not None else "") or ""
        return state, kind, msg

    def wait_task(self, task_id, timeout=600, poll=2.0):
        """Attend la fin d'une tâche (info.state success ou error)."""
        task = _check_moref(task_id, "task")
        what = f"task {task}"
        deadline = self.clock() + timeout
        while True:
            state, kind, msg = self._task_info(task, what)
            if state == "success":
                return True
            if state == "error":
                raise self._err(what, f"failed: {msg or kind or 'no reason given'}")
            if state not in ("queued", "running"):
                raise self._err(what, f"unknown state {state!r}")
            if self.clock() >= deadline:
                raise self._err(what, f"still {state} after {int(timeout)} s")
            self.sleep(poll)

    def clear_forklift_snapshots(self, vm_id, timeout=600, poll=2.0):
        """Retire les instantanés de Forklift d'une VM, des feuilles vers la
        racine, une tâche après l'autre ; rend leurs identifiants."""
        removed = []
        for snap in forklift_snapshots(self.snapshots(vm_id)):
            self.wait_task(self.remove_snapshot(snap), timeout=timeout, poll=poll)
            removed.append(snap)
        return removed

    def soap_logout(self):
        """Ferme la session SOAP ; au mieux."""
        if not self.cookie or not self.content:
            self.cookie = None
            return
        try:
            self._soap('<Logout xmlns="urn:vim25">'
                       f'<_this type="SessionManager">{escape(self.content["sessionManager"])}</_this>'
                       '</Logout>', "logout")
        except VSphereError:
            pass
        self.cookie = None

    def close(self):
        self.logout()
        self.soap_logout()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


VSphere.forklift_snapshots = staticmethod(forklift_snapshots)
