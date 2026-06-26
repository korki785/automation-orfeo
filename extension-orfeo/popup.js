// popup.js — UI seule. Parle au serveur local (127.0.0.1:8723), jamais à Orfeo directement.
// Mode hybride : aperçu API (écriture via API) + plan de remplissage à l'écran (vision).
const HELPER = "http://127.0.0.1:8723";

const $ = (id) => document.getElementById(id);
let PK = null, TAB = null, ECRAN = [];

// ── Détection de la fiche affichée ────────────────────────────────────────────
async function detecterCible() {
  const cible = $("cible");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    TAB = tab;
    if (!tab || !/^https:\/\/orfeoapp\.com\//.test(tab.url || "")) {
      cible.textContent = "Ouvre une fiche structure sur orfeoapp.com.";
      cible.className = "no"; return;
    }
    let ctx = null;
    try { ctx = await chrome.tabs.sendMessage(tab.id, { type: "ORFEO_GET_CONTEXT" }); } catch (_) {}
    if (!ctx || !ctx.pk) {
      const m = (tab.url.match(/(?:structure|entity|lieu|contact)\/(\d{3,})/i)
              || tab.url.match(/(?:^|[\/#=])(\d{5,})(?:[\/?#]|$)/));
      ctx = { pk: m ? m[1] : null, nom: ctx && ctx.nom };
    }
    if (ctx.pk) {
      PK = ctx.pk;
      cible.textContent = `Fiche détectée : ${ctx.nom ? ctx.nom + " — " : ""}pk ${PK}`;
      cible.className = "ok";
    } else {
      cible.textContent = "Fiche non détectée. Ouvre une structure (URL avec son pk).";
      cible.className = "no";
    }
  } catch (e) {
    cible.textContent = "Impossible de lire l'onglet."; cible.className = "no";
  }
}

// ── Serveur local ──────────────────────────────────────────────────────────────
async function appeler(route, corps) {
  let r;
  try {
    r = await fetch(`${HELPER}${route}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corps),
    });
  } catch (e) {
    throw new Error("Serveur local injoignable. Lance : python3 serveur_enrichissement.py");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) throw new Error(data.error || `Erreur serveur (HTTP ${r.status})`);
  return data;
}

// ── Page Orfeo : champs + screenshot ──────────────────────────────────────────
async function collecterChamps() {
  try {
    const rep = await chrome.tabs.sendMessage(TAB.id, { type: "ORFEO_COLLECT_FIELDS" });
    return (rep && rep.fields) || [];
  } catch (_) { return []; }
}
async function capturer() {
  try { return await chrome.tabs.captureVisibleTab(TAB.windowId, { format: "png" }); }
  catch (_) { return ""; }
}

// ── Rendu ──────────────────────────────────────────────────────────────────────
const esc = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const kv = (k, v, cls) => `<div class="kv ${cls || ""}"><span class="k">${k}</span><span class="v">${v}</span></div>`;

function rendre(api, ecran, applique) {
  const blocs = [];
  const champs = Object.entries((api && api.ecrit) || {});
  const tags = (api && api.tags_ajoutes) || [];
  if (champs.length || tags.length) {
    let h = `<div class="bloc"><h4>${applique ? "Écrit via API" : "API — sera écrit"}</h4>`;
    for (const [k, v] of champs) h += kv(esc(k), esc(v));
    if (tags.length) h += kv("tags +", tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(""));
    blocs.push(h + `</div>`);
  }
  const contacts = (api && api.contacts) || [];
  if (contacts.length) {
    let h = `<div class="bloc"><h4>Contacts (API)</h4>`;
    for (const c of contacts) {
      const t = applique ? (c.statut === "ok" ? "✓ " : `✗ (${esc(c.statut)}) `) : "";
      h += kv(esc(c.type), t + esc(c.valeur));
    }
    blocs.push(h + `</div>`);
  }
  if (ecran && ecran.length) {
    let h = `<div class="bloc"><h4>${applique ? "Rempli à l'écran" : "Écran — à remplir (tu enregistres dans Orfeo)"}</h4>`;
    for (const c of ecran) h += kv(esc(c.field_id), `${esc(c.valeur)} <small class="note">(${esc(c.confiance)})</small>`);
    blocs.push(h + `</div>`);
  }
  const av = (api && api.a_valider) || [];
  if (av.length) {
    let h = `<div class="bloc valider"><h4>À valider à la main (non écrit)</h4>`;
    for (const l of av) h += kv(esc(l.bloc + "/" + l.champ), `${esc(l.valeur)} <small class="note">(${esc(l.confiance)})</small>`);
    blocs.push(h + `<small class="note">Jamais écrit automatiquement.</small></div>`);
  }
  if (!blocs.length) blocs.push(`<div class="bloc"><small class="note">${esc((api && api.message) || "Rien à proposer.")}</small></div>`);
  else if (api && api.message) blocs.push(`<div class="bloc valider"><small class="note">${esc(api.message)}</small></div>`);
  $("apercu").innerHTML = blocs.join("");
}

// ── Actions ──────────────────────────────────────────────────────────────────
async function apercu() {
  if (!PK) return setEtat("Aucune fiche détectée.", true);
  const vision = $("vision").checked;
  setOccupe(true); $("row-actions").style.display = "none"; $("apercu").innerHTML = "";
  setEtat(vision ? "Lecture de la page + recherche web… (1–2 min, la vision est lente)"
                 : "Recherche web… (~40 s)");
  try {
    let api, ecran = [];
    if (vision) {
      const [fields, screenshot] = await Promise.all([collecterChamps(), capturer()]);
      const res = await appeler("/visuel", { pk: PK, command: $("cmd").value.trim(), fields, screenshot });
      api = res.api; ecran = res.ecran || [];
    } else {
      api = await appeler("/enrich", { pk: PK, command: $("cmd").value.trim() });
    }
    ECRAN = ecran;
    rendre(api, ECRAN, false);
    const aApi = api && (Object.keys(api.ecrit || {}).length || (api.tags_ajoutes || []).length || (api.contacts || []).length);
    $("btn-ecrire").style.display = aApi ? "block" : "none";
    $("btn-remplir").style.display = ECRAN.length ? "block" : "none";
    $("row-actions").style.display = (aApi || ECRAN.length) ? "flex" : "none";
    setEtat(`Vérifie. ${aApi ? "« Écrire (API) »" : ""}${aApi && ECRAN.length ? " et/ou " : ""}${ECRAN.length ? "« Remplir la page »" : ""}`.trim() || "Rien à écrire.");
  } catch (e) { setEtat(e.message, true); }
  finally { setOccupe(false); }
}

async function ecrire() {
  if (!PK) return;
  setEtat("Écriture API…"); setOccupe(true);
  try {
    const res = await appeler("/apply", { pk: PK, command: $("cmd").value.trim() });
    rendre(res, ECRAN, true);
    $("btn-ecrire").style.display = "none";
    setEtat("API écrit. Recharge la fiche pour voir." + (ECRAN.length ? " (Écran : « Remplir la page » avant de recharger.)" : ""));
  } catch (e) { setEtat(e.message, true); }
  finally { setOccupe(false); }
}

async function remplir() {
  if (!ECRAN.length) return;
  setEtat("Remplissage de la page…"); setOccupe(true);
  try {
    const rep = await chrome.tabs.sendMessage(TAB.id, { type: "ORFEO_FILL", plan: ECRAN });
    const n = (rep && rep.filled) || 0;
    setEtat(`${n} champ(s) rempli(s) à l'écran. Vérifie puis clique Enregistrer dans Orfeo.`);
    $("btn-remplir").style.display = "none";
  } catch (e) { setEtat("Impossible de remplir (recharge la fiche Orfeo).", true); }
  finally { setOccupe(false); }
}

function setEtat(t, bad) { const el = $("etat"); el.textContent = t; el.className = bad ? "bad" : ""; }
function setOccupe(b) { for (const id of ["btn-apercu", "btn-ecrire", "btn-remplir"]) $(id).disabled = b; }

$("btn-apercu").addEventListener("click", apercu);
$("btn-ecrire").addEventListener("click", ecrire);
$("btn-remplir").addEventListener("click", remplir);
$("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); apercu(); } });
detecterCible();
