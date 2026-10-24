"""Onboarding orchestration using the request's existing kubectl identity."""
import json
import subprocess
import time
import uuid

import baremetal_onboarding as profile


def items(api, kc, kind, *args):
    doc = api._kubectl_json(kc, 'get', kind, *args, timeout=20)
    profile.require(isinstance(doc, dict) and isinstance(doc.get('items'), list), 'cannot read ' + kind)
    return doc['items']


def preflight(api, data, cfg):
    choice = profile.selection(data, cfg)
    kc = data.get('_cluster_kc')
    profile.require(bool(kc), 'onboarding requires an existing target cluster and the current user kubeconfig')
    nodes = items(api, kc, 'nodes')
    hostname = cfg['os']['hostname']
    profile.require(all(n['metadata']['name'] != hostname for n in nodes), 'onboarding is only for a new node, not a reinstall of a registered host')
    labels = dict(cfg['os'].get('labels') or {}, **{'kubernetes.io/hostname': hostname})
    storage_names = {port['name'] for port in choice['ports'].values()}
    for config in items(api, kc, 'vlanconfigs.network.harvesterhci.io'):
        spec = config.get('spec') or {}
        if all(labels.get(k) == v for k, v in (spec.get('nodeSelector') or {}).items()):
            profile.require(not storage_names.intersection((spec.get('uplink') or {}).get('nics') or []),
                            'an existing automatic VM network would use a selected storage NIC; scope its selector first')
    # Do not discover insufficient write access after installing a host.
    allowed = api._kubectl_run(['kubectl', '--kubeconfig', kc, 'auth', 'can-i', 'patch', 'nodes'],
                              capture_output=True, text=True, timeout=20)
    profile.require(allowed.returncode == 0 and allowed.stdout.strip() == 'yes', 'node patch permission is required to keep the host cordoned')
    items(api, kc, 'nodes.longhorn.io', '-n', 'longhorn-system')
    api._kubectl_json(kc, 'get', profile.LM, 'nic', timeout=20)
    csi = profile.definition(choice['profile'])['csi_node']
    if csi:
        ds = api._kubectl_json(kc, 'get', 'daemonset', csi['daemonset'], '-n', csi['namespace'], timeout=20)
        profile.require(bool(ds) and not ds['metadata'].get('deletionTimestamp'), 'profile CSI DaemonSet must already be installed')
        items(api, kc, 'pods', '-n', csi['namespace'])
    return {'choice': choice, 'install_id': uuid.uuid4().hex}


def patch(api, kc, hostname, operations):
    try:
        result = api._kubectl_run(['kubectl', '--kubeconfig', kc, 'patch', 'node', hostname,
                                  '--type=json', '-p', json.dumps(operations), '-o', 'json'],
                                 capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        raise profile.ProfileError('node patch result uncertain; inspect the joined host, no write was retried') from None
    profile.require(result.returncode == 0, 'node patch not confirmed; inspect the joined host, no write was retried')
    return json.loads(result.stdout)


def guard(api, kc, node, run):
    profile.require(node['metadata'].get('labels', {}).get('harvester-ops.io/install-id') == run._onboarding_install_id,
                    'registered node does not carry this installation identity; no node was patched')
    if getattr(run, '_onboarding_guarded', False):
        return node
    out = patch(api, kc, node['metadata']['name'], profile.guard_patch(node))
    profile.require(out.get('spec', {}).get('unschedulable'), 'cordon was not confirmed')
    run._onboarding_guarded = True
    return out


def finish(api, opts, run, step, deadline):
    kc = opts['_cluster_kc']
    host = opts['hostname']
    prepared = opts['_onboarding_plan']
    choice = prepared['choice']
    verified = False
    previous = None
    latest = None
    while time.time() < deadline:
        profile.require(not getattr(run, '_cancel', False), 'onboarding cancelled; host remains cordoned')
        node = api._kubectl_json(kc, 'get', 'node', host, timeout=20)
        profile.require(bool(node), 'joined node cannot be read; inspect its cordon state')
        profile.require(node['metadata'].get('labels', {}).get('harvester-ops.io/install-id') == prepared['install_id'], 'joined node identity changed')
        profile.require(node['spec'].get('unschedulable'), 'host was uncordoned outside this job; onboarding stopped')
        if not verified:
            lm = api._kubectl_json(kc, 'get', profile.LM, 'nic', timeout=20)
            links = (((lm or {}).get('status') or {}).get('linkStatus') or {}).get(host, [])
            if not links:
                step('onboarding', 'progress', 'Waiting for joined host NIC inventory; host remains cordoned')
                time.sleep(5)
                continue
            for role, port in choice['ports'].items():
                matches = [l for l in links if l.get('name') == port['name'] and str(l.get('mac', '')).lower() == port['mac']]
                profile.require(len(matches) == 1 and matches[0].get('type') == 'device' and matches[0].get('masterIndex') in (None, 0),
                                'storage NIC identity or ownership changed: ' + role)
            verified = True
        lh = api._kubectl_json(kc, 'get', 'nodes.longhorn.io', host, '-n', 'longhorn-system', timeout=20)
        csi = profile.definition(choice['profile'])['csi_node']
        pods = items(api, kc, 'pods', '-n', csi['namespace']) if csi else []
        latest = profile.readiness(node, lh, pods, choice)
        message = '; '.join(k + ': ' + ('Ready' if v else 'pending') for k, v in latest.items())
        if message != previous:
            step('onboarding', 'progress', message)
            previous = message
        if all(latest.values()):
            step('onboarding', 'done', 'Selected storage profile checks passed. Host remains cordoned; storage connectivity and guest I/O still require validation.')
            return latest
        time.sleep(5)
    raise profile.ProfileError('readiness checks timed out; host remains cordoned. ' + (previous or 'No readiness report available'))
