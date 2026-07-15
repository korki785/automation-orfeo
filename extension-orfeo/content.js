// content.js — détecte le pk, collecte les champs du formulaire en direct, et
// remplit les champs à l'écran (phase 3). Aucune connaissance préalable du DOM Orfeo.

// ── Détection du pk ──────────────────────────────────────────────────────────
function pkDepuisURL() {
  const url = location.href;
  const motcle = url.match(/(?:structure|entity|entit[eé]|lieu|contact)\/(\d{3,})/i);
  if (motcle) return motcle[1];
  const isole = url.match(/(?:^|[\/#=])(\d{5,})(?:[\/?#]|$)/);
  return isole ? isole[1] : null;
}

function nomDepuisDOM() {
  for (const s of ["h1", "h2", '[class*="title"]', '[class*="name"]']) {
    const el = document.querySelector(s);
    const t = el && el.textContent && el.textContent.trim();
    if (t && t.length > 1 && t.length < 120) return t;
  }
  return null;
}

// ── Collecte des champs visibles et éditables ────────────────────────────────
function estVisible(el) {
  if (!el || el.disabled || el.readOnly) return false;
  const t = (el.getAttribute("type") || "").toLowerCase();
  if (["hidden", "submit", "button", "reset", "file", "image", "password"].includes(t)) return false;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return false;
  const st = getComputedStyle(el);
  return st.visibility !== "hidden" && st.display !== "none";
}

function libelle(el) {
  // 1) <label for=id>
  if (el.id) {
    const lab = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lab && lab.textContent.trim()) return lab.textContent.trim();
  }
  // 2) label ancêtre
  const anc = el.closest("label");
  if (anc && anc.textContent.trim()) return anc.textContent.trim().slice(0, 80);
  // 3) aria-label / placeholder / name
  for (const a of ["aria-label", "placeholder", "name"]) {
    const v = el.getAttribute(a);
    if (v && v.trim()) return v.trim();
  }
  // 4) texte d'un élément précédent proche
  let p = el.parentElement, hops = 0;
  while (p && hops < 3) {
    const lab = p.querySelector("label, .label, [class*='label']");
    if (lab && lab.textContent.trim()) return lab.textContent.trim().slice(0, 80);
    p = p.parentElement; hops++;
  }
  return null;
}

function collecterChamps() {
  const els = Array.from(document.querySelectorAll("input, select, textarea")).filter(estVisible);
  const champs = [];
  els.forEach((el, i) => {
    const fid = "f" + i;
    el.setAttribute("data-orfeo-fid", fid);
    const tag = el.tagName.toLowerCase();
    const type = tag === "input" ? (el.getAttribute("type") || "text") : tag;
    const champ = { id: fid, label: libelle(el), type, value: el.value || "" };
    if (tag === "select") {
      champ.options = Array.from(el.options).map((o) => o.text.trim()).filter(Boolean);
    }
    champs.push(champ);
  });
  return champs;
}

// ── Remplissage à l'écran (compatible SPA / React) ───────────────────────────
function poser(el, valeur) {
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
              : el.tagName === "SELECT" ? HTMLSelectElement.prototype
              : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
  setter.call(el, valeur);
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
}

function remplirChamps(plan) {
  let n = 0;
  for (const c of plan || []) {
    const el = document.querySelector(`[data-orfeo-fid="${CSS.escape(c.field_id)}"]`);
    if (!el) continue;
    if (el.tagName === "SELECT") {
      const opt = Array.from(el.options).find(
        (o) => o.text.trim().toLowerCase() === String(c.valeur).trim().toLowerCase()
      );
      if (!opt) continue;
      poser(el, opt.value);
    } else {
      poser(el, c.valeur);
    }
    el.style.outline = "2px solid #0a7c4a";
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    n++;
  }
  return n;
}

// ── Site web : le seul champ qu'Orfeo n'expose PAS en écriture sur son API ───
// `structure.web_addresses` est read_only (PATCH accepté mais ignoré). L'UI passe
// par POST /backend/entitywebaddress/bulk_set/, qui exige la session navigateur.
// On ne rejoue pas cet appel : on remplit le formulaire inline de la fiche et on
// clique son bouton — Orfeo enregistre lui-même, avec son propre CSRF.
const CHAMP_WEB = "input[type='text'], input[type='url'], input.form-control";
const BOUTON_WEB = "button[type='submit'], button.btn-primary, input[type='submit']";

function attendre(trouver, ms = 3000) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    (function reessayer() {
      const el = trouver();
      if (el) return resolve(el);
      if (Date.now() - t0 > ms) return resolve(null);
      setTimeout(reessayer, 100);
    })();
  });
}

// La zone du site : d'abord l'id observé sur la fiche, sinon le formulaire qui
// poste vers bulk_set (signature la plus fiable — c'est l'endpoint d'Orfeo lui-même).
function zoneWeb() {
  const parId = document.querySelector("#inline-web-infos");
  if (parId) return parId;
  const parAction = document.querySelector("form[action*='entitywebaddress']");
  if (parAction) return parAction.closest("td, div, section") || parAction;
  return null;
}

// Photographie de ce que le script VOIT réellement dans la page — le popup la
// renvoie au serveur local, qui la journalise. Sans ça, un échec de remplissage
// est muet : la page est le seul endroit où l'on peut constater le problème.
function diagnosticWeb() {
  const zone = zoneWeb();
  const d = {
    url: location.href,
    zone_par_id: Boolean(document.querySelector("#inline-web-infos")),
    zone_par_action: Boolean(document.querySelector("form[action*='entitywebaddress']")),
    zone_trouvee: Boolean(zone),
  };
  if (zone) {
    d.zone_tag = zone.tagName;
    d.zone_id = zone.id || "(sans id)";
    d.inputs = [...zone.querySelectorAll("input")].map(
      (i) => `${i.type}|name=${i.name || "-"}|val=${(i.value || "").slice(0, 30)}`);
    d.boutons = [...zone.querySelectorAll("button, a, input[type=submit]")].map(
      (b) => `${b.tagName}|${(b.type || "")}|${(b.textContent || b.value || "").trim().slice(0, 25)}`);
    d.html = (zone.innerHTML || "").replace(/\s+/g, " ").slice(0, 600);
  }
  return d;
}

async function remplirSiteWeb(url) {
  const zone = zoneWeb();
  if (!zone) {
    return { ok: false, raison: "Zone « Site internet » introuvable — ouvre la fiche en vue (/view/) et recharge la page.",
             diagnostic: diagnosticWeb() };
  }

  // Le formulaire inline n'apparaît qu'après un clic sur la zone.
  let input = zone.querySelector(CHAMP_WEB);
  if (!input) {
    (zone.querySelector("a, button") || zone).click();
    input = await attendre(() => zone.querySelector(CHAMP_WEB));
  }
  if (!input) {
    return { ok: false, raison: "Zone trouvée, mais aucun champ de saisie après le clic.",
             diagnostic: diagnosticWeb() };
  }

  // Ne jamais écraser une adresse déjà saisie — même règle que l'API : on ne
  // remplit que le vide.
  const actuel = (input.value || "").trim();
  if (actuel) {
    return { ok: false, raison: `Déjà renseigné (${actuel}) — non écrasé.`, diagnostic: diagnosticWeb() };
  }

  poser(input, url);
  const bouton = zone.querySelector(BOUTON_WEB);
  if (!bouton) {
    return { ok: false, raison: "Champ rempli, mais bouton d'enregistrement introuvable — valide à la main.",
             diagnostic: diagnosticWeb() };
  }
  bouton.click();

  // Orfeo enregistre en arrière-plan (POST bulk_set). On laisse le temps à sa
  // réponse, puis on photographie la zone : c'est le seul moyen de savoir si
  // l'enregistrement a réellement pris, ou si le clic n'a rien déclenché.
  await new Promise((r) => setTimeout(r, 1500));
  return { ok: true, valeur: url, diagnostic: diagnosticWeb() };
}

// ── Messages depuis le popup ─────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "ORFEO_GET_CONTEXT") {
    sendResponse({ pk: pkDepuisURL(), nom: nomDepuisDOM(), url: location.href });
  } else if (msg.type === "ORFEO_COLLECT_FIELDS") {
    sendResponse({ fields: collecterChamps() });
  } else if (msg.type === "ORFEO_FILL") {
    sendResponse({ filled: remplirChamps(msg.plan) });
  } else if (msg.type === "ORFEO_FILL_WEB") {
    remplirSiteWeb(msg.url).then(sendResponse);   // asynchrone : le canal reste ouvert
  }
  return true;
});
