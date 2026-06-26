// content.js — détecte le pk de la structure Orfeo affichée et le nom si possible.
// Le pk peut être dans l'URL (route SPA) ou dans le DOM ; on tente plusieurs sources.

function pkDepuisURL() {
  const url = location.href;
  // Routes possibles : /structure/123, /entity/123, /lieu/123, /contact/structure/123…
  const motcle = url.match(/(?:structure|entity|entit[eé]|lieu|contact)\/(\d{3,})/i);
  if (motcle) return motcle[1];
  // Repli : un identifiant numérique long isolé dans l'URL (hash compris).
  const isole = url.match(/(?:^|[\/#=])(\d{5,})(?:[\/?#]|$)/);
  return isole ? isole[1] : null;
}

function nomDepuisDOM() {
  // Heuristique : le titre principal de la fiche. Best-effort, jamais bloquant.
  const sel = ["h1", "h2", '[class*="title"]', '[class*="name"]'];
  for (const s of sel) {
    const el = document.querySelector(s);
    const t = el && el.textContent && el.textContent.trim();
    if (t && t.length > 1 && t.length < 120) return t;
  }
  return null;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "ORFEO_GET_CONTEXT") {
    sendResponse({ pk: pkDepuisURL(), nom: nomDepuisDOM(), url: location.href });
  }
  return true;
});
