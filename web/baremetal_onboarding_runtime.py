"""Onboarding orchestration using the request's existing kubectl identity."""
import json
import os
import subprocess
import time
import uuid

import baremetal_onboarding as profile

# Délai propre aux vérifications de préparation, borné en plus par le budget
# d'après installation (HARVESTER_OPS_POOLS_TIMEOUT, 1800 s par défaut).
READINESS_TIMEOUT = int(os.environ.get('HARVESTER_OPS_ONBOARDING_TIMEOUT', 1800))
# Tentatives d'un patch conditionnel refusé (resourceVersion périmée).
PATCH_ATTEMPTS = int(os.environ.get('HARVESTER_OPS_ONBOARDING_PATCH_ATTEMPTS', 5))
POLL_SECONDS = 5


def items(api, kc, kind, *args):
    doc = api._kubectl_json(kc, 'get', kind, *args, timeout=20)
    profile.require(isinstance(doc, dict) and isinstance(doc.get('items'), list),
                    'cannot read ' + kind)
    return doc['items']


def _install_id(run):
    return (getattr(run, '_onboarding_plan', None) or {}).get('install_id')


def _native(run):
    return bool((getattr(run, '_onboarding_plan', None) or {}).get('native_cordon'))


def preflight(api, data, cfg):
    choice = profile.selection(data, cfg)
    spec = profile.definition(choice['profile'])
    kc = data.get('_cluster_kc')
    profile.require(bool(kc), 'onboarding requires an existing target cluster '
                              'and the current user kubeconfig')
    hostname = cfg['os']['hostname']
    # Un seul nœud lu, pas la liste : absent = NotFound, tout autre refus bloque.
    try:
        found = api._kubectl_run(['kubectl', '--kubeconfig', kc, 'get', 'node', hostname,
                                  '-o', 'json'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise profile.ProfileError('cannot read the target cluster nodes') from None
    profile.require(found.returncode != 0,
                    'onboarding is only for a new node, not a reinstall of a registered host')
    profile.require('NotFound' in (found.stderr or ''), 'cannot read the target cluster nodes')
    # Version lue par le kubeconfig figé de la requête : v1.9.0 et plus
    # cordonnent le nœud eux-mêmes (install.cordoned), la console n'y écrit rien.
    native = profile.native_cordon(api._hup.parse_version(api._harvester_server_version(kc)))
    if not native:
        # Do not discover insufficient write access after installing a host.
        allowed = api._kubectl_run(['kubectl', '--kubeconfig', kc, 'auth', 'can-i', 'patch',
                                    'nodes'], capture_output=True, text=True, timeout=20)
        profile.require(allowed.returncode == 0 and allowed.stdout.strip() == 'yes',
                        'node patch permission is required to keep the host cordoned')
    items(api, kc, 'nodes.longhorn.io', '-n', 'longhorn-system')
    items(api, kc, profile.VC)
    lm = api._kubectl_json(kc, 'get', profile.LM, 'nic', timeout=20)
    profile.require(isinstance(lm, dict),
                    'the network controller LinkMonitor "nic" is required to verify storage NICs')
    storage_macs = {port['mac'] for port in choice['ports'].values()}
    for host, links in ((lm.get('status') or {}).get('linkStatus') or {}).items():
        seen = {str(link.get('mac') or '').lower() for link in links or []}
        profile.require(host == hostname or not storage_macs & seen,
                        'a selected storage MAC is already reported by registered host ' + host)
    csi = spec['csi_node']
    if csi:
        ds = api._kubectl_json(kc, 'get', 'daemonset', csi['daemonset'], '-n', csi['namespace'],
                               timeout=20)
        profile.require(bool(ds) and not ds['metadata'].get('deletionTimestamp'),
                        'profile CSI DaemonSet must already be installed')
        _node_pods(api, kc, csi, hostname)
    return {'choice': choice, 'install_id': uuid.uuid4().hex, 'native_cordon': native}


def _node_pods(api, kc, csi, host):
    # filtrés côté serveur : seuls les pods du nœud rejoint
    return items(api, kc, 'pods', '-n', csi['namespace'], '--field-selector', 'spec.nodeName=' + host)


UNCERTAIN = 'node patch result uncertain; inspect the joined host, no write was retried'


def patch(api, kc, hostname, operations):
    """Un patch JSON conditionnel. Rend l'objet, ou None quand le serveur l'a
    refusé : un `test` en échec n'applique RIEN (RFC 6902), on peut relire et
    recommencer. Un délai dépassé laisse l'issue inconnue : jamais renvoyé."""
    try:
        result = api._kubectl_run(['kubectl', '--kubeconfig', kc, 'patch', 'node', hostname,
                                   '--type=json', '-p', json.dumps(operations), '-o', 'json'],
                                  capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        raise profile.ProfileError(UNCERTAIN) from None
    if result.returncode != 0:
        err = (result.stderr or '').lower()
        profile.require('forbidden' not in err and 'unauthorized' not in err,
                        'node patch refused for the current user; host remains tainted')
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise profile.ProfileError(UNCERTAIN) from None


def _check_identity(node, run):
    profile.require(bool(node), 'joined node cannot be read; inspect its cordon state')
    labels = node['metadata'].get('labels', {})
    profile.require(labels.get(profile.INSTALL_ID_LABEL) == _install_id(run),
                    'registered node does not carry this installation identity; no node was patched')
    # Un installeur plus ancien que le cluster ignore install.cordoned en silence.
    profile.require(not _native(run) or labels.get(profile.NATIVE_CORDON_LABEL) == 'true',
                    'registered node lacks the install-cordoned label: the installer ignored '
                    'install.cordoned; cordon the host by hand')


def cordon(api, kc, node, run):
    """Cordon conditionnel (UID + resourceVersion) qui retire la seule taint
    d'onboarding. Relit le nœud et recommence après un refus, au plus
    PATCH_ATTEMPTS fois."""
    name = node['metadata']['name']
    for _ in range(max(1, PATCH_ATTEMPTS)):
        _check_identity(node, run)
        out = patch(api, kc, name, profile.guard_patch(node))
        if out is not None:
            profile.require(out.get('spec', {}).get('unschedulable'), 'cordon was not confirmed')
            return out
        node = api._kubectl_json(kc, 'get', 'node', name, timeout=20)
    raise profile.ProfileError('the node kept changing during %d cordon attempts; '
                               'host remains tainted' % max(1, PATCH_ATTEMPTS))


def guard(api, kc, node, run):
    """Protège le nœud dès qu'il apparaît. Rend le nœud (patché ou non)."""
    _check_identity(node, run)
    # Cordon natif : Harvester le pose, la console ne fait que le constater (finish).
    if _native(run) or getattr(run, '_onboarding_guarded', False):
        return node
    status = profile.promote_status(node)
    # Mécanisme de la console, pour les clusters antérieurs à v1.9.0. Le
    # contrôleur de promotion de Harvester (pkg/controller/master/node/
    # promote_controller.go, v1.8.2) pose Unschedulable=true au début
    # (setPromoteStart) et le lève à la fin (setPromoteResult, statut
    # « complete »). En v1.8.2, selectPromoteNode n'écarte pas un nœud
    # cordonné : une promotion peut commencer avant comme après notre cordon
    # (voir finish). Pendant qu'elle tourne avant notre cordon,
    # la taint d'onboarding reste en place : elle couvre la levée du cordon
    # en fin de promotion jusqu'à notre patch.
    if status == profile.PROMOTE_RUNNING:
        return node
    profile.require(status in (None, profile.PROMOTE_COMPLETE),
                    'Harvester promotion of this host ended as "%s"; host remains tainted' % status)
    out = cordon(api, kc, node, run)
    run._onboarding_guarded = True
    run._onboarding_promote_seen = profile.promote_status(out)
    return out


def _verify_nics(api, kc, node, links, choice):
    """Chaque MAC de stockage : une seule carte physique hors bond dans
    l'inventaire LinkMonitor de l'hôte. Les noms ne sont connus qu'ici : une
    configuration réseau automatique qui viserait l'hôte ne doit pas prendre
    une carte de stockage."""
    names = {}
    for role, port in choice['ports'].items():
        matches = [link for link in links if str(link.get('mac', '')).lower() == port['mac']
                   and link.get('type') == 'device']
        profile.require(len(matches) == 1 and matches[0].get('masterIndex') in (None, 0),
                        'storage NIC identity or ownership changed: ' + role)
        names[matches[0].get('name')] = role
    labels = node['metadata'].get('labels') or {}
    for config in items(api, kc, profile.VC):
        spec = config.get('spec') or {}
        selector = spec.get('nodeSelector') or {}
        if config['metadata'].get('deletionTimestamp') or any(
                labels.get(k) != v for k, v in selector.items()):
            continue
        claimed = set(names) & set((spec.get('uplink') or {}).get('nics') or [])
        profile.require(not claimed, 'network configuration ' + config['metadata']['name']
                        + ' would use storage NIC ' + ', '.join(sorted(names[n] for n in claimed))
                        + '; scope its selector, host remains cordoned')


def finish(api, opts, run, step, deadline):
    kc = opts['_cluster_kc']
    host = opts['hostname']
    choice = opts['_onboarding_plan']['choice']
    spec = profile.definition(choice['profile'])
    csi = spec['csi_node']
    deadline = min(deadline, time.time() + READINESS_TIMEOUT)
    verified = False
    previous = None

    def report(message):
        nonlocal previous
        if message != previous:
            step('onboarding', 'progress', message)
            previous = message

    while time.time() < deadline:
        profile.require(not getattr(run, '_cancel', False),
                        'onboarding cancelled; host remains cordoned')
        node = api._kubectl_json(kc, 'get', 'node', host, timeout=20)
        node = guard(api, kc, node, run)
        if _native(run):
            annotations = node['metadata'].get('annotations') or {}
            if annotations.get(profile.NATIVE_CORDON_DONE) != 'true':
                report('Waiting for Harvester to apply the install cordon')
                time.sleep(POLL_SECONDS)
                continue
            # reconcileInstallCordonedNode laisse planifiable le dernier nœud
            # disponible ; tout autre retrait du cordon vient d'ailleurs.
            profile.require(node['spec'].get('unschedulable'),
                            'host is schedulable after the install cordon: no other node was '
                            'available, or it was uncordoned outside this job; onboarding stopped')
        elif not getattr(run, '_onboarding_guarded', False):
            report('Waiting for the Harvester promotion of this host; it stays tainted')
            time.sleep(POLL_SECONDS)
            continue
        elif not node['spec'].get('unschedulable'):
            status = profile.promote_status(node)
            # Promotion achevée après notre cordon : le contrôleur l'a levé
            # (setPromoteResult). On le repose, sous les mêmes conditions.
            seen = getattr(run, '_onboarding_promote_seen', None)
            profile.require(status == profile.PROMOTE_COMPLETE and seen != profile.PROMOTE_COMPLETE,
                            'host was uncordoned outside this job; onboarding stopped')
            node = cordon(api, kc, node, run)
            run._onboarding_promote_seen = profile.PROMOTE_COMPLETE
            step('onboarding', 'progress',
                 'Harvester promotion completed and lifted the cordon; host cordoned again')
        status = profile.promote_status(node)
        # Promotion lancée après notre cordon (possible en v1.8.2) : rien n'est
        # déclaré prêt tant qu'elle tourne, le contrôleur lèverait le cordon
        # après la fin de cette tâche.
        if status == profile.PROMOTE_RUNNING:
            report('Waiting for the Harvester promotion of this host; host remains cordoned')
            time.sleep(POLL_SECONDS)
            continue
        profile.require(status in (None, profile.PROMOTE_COMPLETE),
                        'Harvester promotion of this host ended as "%s"; '
                        'host remains cordoned' % status)
        if not verified:
            lm = api._kubectl_json(kc, 'get', profile.LM, 'nic', timeout=20)
            links = (((lm or {}).get('status') or {}).get('linkStatus') or {}).get(host, [])
            if not links:
                report('Waiting for joined host NIC inventory; host remains cordoned')
                time.sleep(POLL_SECONDS)
                continue
            _verify_nics(api, kc, node, links, choice)
            verified = True
        lh = api._kubectl_json(kc, 'get', 'nodes.longhorn.io', host, '-n', 'longhorn-system',
                               timeout=20)
        pods = _node_pods(api, kc, csi, host) if csi else []
        latest = profile.readiness(node, lh, pods, choice, csi)
        report('; '.join(k + ': ' + ('Ready' if v else 'pending') for k, v in latest.items()))
        if all(latest.values()):
            step('onboarding', 'done', 'Selected storage profile checks passed. Host remains cordoned; '
                 'storage connectivity and guest I/O still require validation.')
            return latest
        time.sleep(POLL_SECONDS)
    raise profile.ProfileError('readiness checks timed out; host remains cordoned. '
                               + (previous or 'No readiness report available'))
