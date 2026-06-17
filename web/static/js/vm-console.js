/**
 * harvester-ops — VM console panel (noVNC placeholder)
 *
 * Stage 1 (now): opens an overlay informing the user that the VNC console
 * is in development. Provides quick-link to use virtctl/kubectl console on
 * the operator host.
 *
 * Stage 2 (next iteration): bundle noVNC + Flask WebSocket proxy to KubeVirt
 * /apis/subresources.kubevirt.io/v1/namespaces/<ns>/virtualmachineinstances/<name>/vnc
 */
const VMConsole = (() => {
  function open(cluster, namespace, name) {
    const panelId = `vm-console-${cluster}-${namespace}-${name}`;
    const title = `🖥 Console — ${namespace}/${name}`;
    const body = `
      <div class="vm-console-placeholder">
        <h3>VM console — coming soon</h3>
        <p class="form-hint">
          The in-browser VNC console will arrive in the next iteration.
          It requires a noVNC bundle + WebSocket proxy to the KubeVirt
          <code>/vnc</code> subresource.
        </p>
        <h4>Use these commands meanwhile</h4>
        <pre>
# Graphical (VNC) — opens a viewer locally:
virtctl --kubeconfig &lt;path&gt; vnc ${name} -n ${namespace}

# Serial console:
virtctl --kubeconfig &lt;path&gt; console ${name} -n ${namespace}

# Or via kubectl proxy + a websocket client:
kubectl --kubeconfig &lt;path&gt; proxy --port=8001 &amp;
# then connect to:
ws://localhost:8001/apis/subresources.kubevirt.io/v1/\\
namespaces/${namespace}/virtualmachineinstances/${name}/vnc
        </pre>
        <p class="form-hint">
          The container running harvester-ops needs the
          <code>get virtualmachineinstances/vnc</code> RBAC permission
          (already in the standard cluster-admin role).
        </p>
      </div>`;
    FloatingPanels.open({
      id: panelId, title, bodyHtml: body, width: 720, height: 460,
      restoreSpec: { type: 'vm-console', args: { cluster, namespace, name } },
    });
  }
  return { open };
})();

window.VMConsole = VMConsole;
FloatingPanels.registerType('vm-console', (args) =>
  VMConsole.open(args.cluster, args.namespace, args.name));
