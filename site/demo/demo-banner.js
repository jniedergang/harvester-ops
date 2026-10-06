/**
 * harvester-ops : bandeau de la démo (v1.86.0). Dit que les données sont
 * simulées et que rien n'est modifié, dans la langue de l'interface, avec un
 * lien de retour vers la page du site dans cette langue.
 */
(() => {
  const T = {
    en: ['Live demo', 'Simulated clusters: every gesture works, nothing is changed.', 'Back to the site'],
    fr: ['Démo vivante', 'Clusters simulés : chaque geste fonctionne, rien n’est modifié.', 'Retour au site'],
    es: ['Demo en vivo', 'Clústeres simulados: cada gesto funciona, no se cambia nada.', 'Volver al sitio'],
    it: ['Demo dal vivo', 'Cluster simulati: ogni gesto funziona, nulla viene modificato.', 'Torna al sito'],
    de: ['Live-Demo', 'Simulierte Cluster: jede Aktion funktioniert, nichts wird verändert.', 'Zurück zur Website'],
  };
  function draw() {
    let lang = 'en';
    try { lang = localStorage.getItem('harvester_ops_language') || (navigator.language || 'en').slice(0, 2); } catch { /* stockage indisponible */ }
    if (!T[lang]) lang = 'en';
    const [title, text, back] = T[lang];
    let b = document.getElementById('demo-banner');
    if (!b) {
      b = document.createElement('div');
      b.id = 'demo-banner';
      b.setAttribute('role', 'note');
      document.body.appendChild(b);
    }
    b.innerHTML = '';
    const s = document.createElement('strong');
    s.textContent = title;
    const p = document.createElement('span');
    p.textContent = text;
    const a = document.createElement('a');
    a.href = `../${lang}/`;
    a.textContent = back;
    b.append(s, p, a);
  }
  draw();
  // la langue se change dans les réglages de la console : le bandeau suit
  setInterval(draw, 2000);
})();
