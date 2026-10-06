// harvester-ops : menu du site sur téléphone (v1.86.0)
(() => {
  const btn = document.querySelector('.menu-btn');
  const nav = document.getElementById('topnav');
  if (!btn || !nav) return;
  btn.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
  });
  nav.addEventListener('click', (e) => { if (e.target.closest('a')) { nav.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); } });
})();
