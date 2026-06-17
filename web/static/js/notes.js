/**
 * harvester-ops — collaborative notes (Yjs + Tiptap stage 2).
 *
 * Tiptap rich-text editor bound to a Y.Doc via the Collaboration extension.
 * The Y.Doc syncs over a WebSocket to the Flask backend (y-py on the server
 * persists every update to SQLite). Two clients editing the same note see
 * each other's changes in real-time — character-level conflict-free merge.
 *
 *   Notes.open('vm', cluster, namespace, name);   // VM-attached notes
 *   Notes.open('ns', cluster, namespace);         // Namespace-attached notes
 */
import { Editor, StarterKit, Collaboration, Y } from '/static/vendor/tiptap/tiptap-bundle.mjs';

const Notes = (() => {

  /** @type {Map<string, {doc:any, editor:any, ws:WebSocket|null, applying:boolean, reconnectTimer:number|null}>} */
  const openDocs = new Map();

  function docId(kind, cluster, namespace, name) {
    if (kind === 'vm')   return `vm/${cluster}/${namespace}/${name}`;
    if (kind === 'node') return `node/${cluster}/${namespace}`; // namespace = node name
    return `ns/${cluster}/${namespace}`;
  }

  function open(kind, cluster, namespace, name) {
    const id = docId(kind, cluster, namespace, name);
    const panelId = 'notes-' + id.replace(/[^a-zA-Z0-9_-]/g, '_');
    const title = kind === 'vm'
      ? `📝 Notes — ${namespace}/${name}`
      : kind === 'node'
        ? `📝 Notes — node ${namespace}`
        : `📝 Notes — namespace ${namespace}`;

    if (openDocs.has(id)) {
      FloatingPanels.restore(panelId);
      return;
    }

    const html = `
      <div class="notes-toolbar">
        <span class="notes-status" data-status>connecting…</span>
        <div class="notes-toolbar-buttons">
          <button type="button" data-cmd="bold"      title="Gras (Ctrl+B)"><strong>B</strong></button>
          <button type="button" data-cmd="italic"    title="Italique (Ctrl+I)"><em>I</em></button>
          <button type="button" data-cmd="strike"    title="Barré"><s>S</s></button>
          <button type="button" data-cmd="code"      title="Code inline"><code>{ }</code></button>
          <span class="notes-toolbar-sep">·</span>
          <button type="button" data-cmd="h1" title="Heading 1">H1</button>
          <button type="button" data-cmd="h2" title="Heading 2">H2</button>
          <button type="button" data-cmd="h3" title="Heading 3">H3</button>
          <span class="notes-toolbar-sep">·</span>
          <button type="button" data-cmd="bullet"    title="Liste à puces">•</button>
          <button type="button" data-cmd="ordered"   title="Liste numérotée">1.</button>
          <button type="button" data-cmd="blockquote" title="Citation">”</button>
          <button type="button" data-cmd="codeblock" title="Bloc de code">⌜⌝</button>
          <span class="notes-toolbar-sep">·</span>
          <button type="button" data-cmd="undo"      title="Annuler (Ctrl+Z)">↶</button>
          <button type="button" data-cmd="redo"      title="Rétablir (Ctrl+Shift+Z)">↷</button>
        </div>
        <span class="notes-stats" data-stats></span>
      </div>
      <div class="notes-editor-wrap"><div class="notes-editor" data-editor></div></div>`;

    const panel = FloatingPanels.open({
      id: panelId,
      title,
      bodyHtml: html,
      width: 720,
      height: 520,
      restoreSpec: { type: 'notes', args: { kind, cluster, namespace, name } },
      onClose: () => disconnect(id),
    });

    const editorEl = panel.el.querySelector('[data-editor]');
    const statusEl = panel.el.querySelector('[data-status]');
    const statsEl  = panel.el.querySelector('[data-stats]');

    // Shared Y.Doc — Tiptap's Collaboration extension takes ownership of the
    // 'content' XmlFragment inside it. The same fragment is what the server
    // syncs via the WebSocket update stream.
    const doc = new Y.Doc();
    const editor = new Editor({
      element: editorEl,
      extensions: [
        // Disable the built-in history (Collaboration provides its own).
        StarterKit.configure({ undoRedo: false }),
        // IMPORTANT: don't use 'content' as the field — the pre-Tiptap
        // version of this module stored a Y.Text under that name, and the
        // collision (Tiptap expects Y.XmlFragment, Yjs sees Y.Text) makes
        // the Collaboration extension call disableCollaboration() →
        // doc.destroy() right after the first keystroke, which is what
        // makes the typed character vanish instantly. 'prosemirror' is the
        // safe, conventional Tiptap field name.
        Collaboration.configure({ document: doc, field: 'prosemirror' }),
      ],
      onUpdate: () => {
        if (statsEl) statsEl.textContent = `${editor.storage.characterCount?.characters?.() || editor.getText().length} chars`;
      },
    });

    const entry = { doc, editor, ws: null, reconnectTimer: null };
    openDocs.set(id, entry);

    // Forward local Y updates to the server. ySyncPlugin tags its own
    // transactions with `origin === ySyncPluginKey`-ish — we just skip the
    // ones that came from the network ('remote' origin).
    doc.on('update', (update, origin) => {
      if (origin === 'remote') return;
      if (!entry.ws || entry.ws.readyState !== WebSocket.OPEN) return;
      entry.ws.send(JSON.stringify({ type: 'update', data: bytesToBase64(update) }));
    });

    // Toolbar wiring
    panel.el.querySelectorAll('.notes-toolbar [data-cmd]').forEach(btn => {
      btn.addEventListener('click', () => runCmd(editor, btn.dataset.cmd));
    });

    connect(id, entry, statusEl, statsEl);
  }

  function runCmd(editor, cmd) {
    const c = editor.chain().focus();
    switch (cmd) {
      case 'bold':       c.toggleBold().run(); break;
      case 'italic':     c.toggleItalic().run(); break;
      case 'strike':     c.toggleStrike().run(); break;
      case 'code':       c.toggleCode().run(); break;
      case 'h1':         c.toggleHeading({ level: 1 }).run(); break;
      case 'h2':         c.toggleHeading({ level: 2 }).run(); break;
      case 'h3':         c.toggleHeading({ level: 3 }).run(); break;
      case 'bullet':     c.toggleBulletList().run(); break;
      case 'ordered':    c.toggleOrderedList().run(); break;
      case 'blockquote': c.toggleBlockquote().run(); break;
      case 'codeblock':  c.toggleCodeBlock().run(); break;
      case 'undo':       c.undo().run(); break;
      case 'redo':       c.redo().run(); break;
    }
  }

  function connect(id, entry, statusEl, statsEl) {
    if (statusEl) { statusEl.textContent = '○ connecting…'; statusEl.className = 'notes-status'; }
    const url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/notes/' + id;
    let ws;
    try { ws = new WebSocket(url); } catch (e) {
      console.warn('notes WS create failed', e); return;
    }
    entry.ws = ws;

    ws.addEventListener('open', () => {
      if (statusEl) { statusEl.textContent = '● connected'; statusEl.classList.add('connected'); }
      ws.send(JSON.stringify({ type: 'hello' }));
    });

    ws.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.type === 'ping') {
        try { ws.send(JSON.stringify({ type: 'pong', ts: msg.ts })); } catch {}
        return;
      }
      if (msg.type !== 'snapshot' && msg.type !== 'update') return;
      let bytes;
      try { bytes = base64ToBytes(msg.data); } catch { return; }
      try { Y.applyUpdate(entry.doc, bytes, 'remote'); }
      catch (e) { console.warn('notes applyUpdate failed', e); }
    });

    ws.addEventListener('close', () => {
      if (statusEl) { statusEl.textContent = '○ disconnected — reconnecting…'; statusEl.classList.remove('connected'); }
      entry.ws = null;
      if (openDocs.has(id)) {
        entry.reconnectTimer = setTimeout(() => connect(id, entry, statusEl, statsEl), 3000);
      }
    });

    ws.addEventListener('error', (e) => { console.warn('notes WS error', e); });
  }

  function disconnect(id) {
    const entry = openDocs.get(id);
    if (!entry) return;
    if (entry.reconnectTimer) clearTimeout(entry.reconnectTimer);
    try { entry.editor?.destroy(); } catch {}
    if (entry.ws) { try { entry.ws.close(); } catch {} }
    openDocs.delete(id);
  }

  function bytesToBase64(bytes) {
    let bin = '';
    for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }
  function base64ToBytes(b64) {
    const bin = atob(b64);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  return { open, disconnect };
})();

window.Notes = Notes;
if (window.FloatingPanels) {
  FloatingPanels.registerType('notes', (args) =>
    Notes.open(args.kind, args.cluster, args.namespace, args.name));
}
