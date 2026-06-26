// popup.js — UI seule. Parle au serveur local (127.0.0.1:8723), jamais à Orfeo directement.
const HELPER = "http://127.0.0.1:8723";

const $ = (id) => document.getElementById(id);
let PK = null;

// ── Détection du pk de la fiche affichée ──────────────────────────────────────
async function detecterCible() {
  const cible = $("cible");
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https:\/\/orfeoapp\.com\//.test(tab.url || "")) {
      cible.textContent = "Ouvre une fiche structure sur orfeoapp.com.";
      cible.className = "no";
      return;
    }
    let ctx = null;
    try {
      ctx = await chrome.tabs.sendMessage(tab.id, { type: "ORFEO_GET_CONTEXT" });
    } catch (_) { /* content script pas encore injecté */ }

    // Repli : parser l'URL de l'onglet directement.
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
    cible.textContent = "Impossible de lire l'onglet.";
    cible.className = "no";
  }
}

// ── Appels au serveur local ───────────────────────────────────────────────────
async function appeler(route, corps) {
  let r;
  try {
    r = await fetch(`${HELPER}${route}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(corps),
    });
  } catch (e) {
    throw new Error("Serveur local injoignable. Lance : python3 serveur_enrichissement.py");
  }
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `Erreur serveur (HTTP ${r.status})`);
  }
  return data;
}

// ── Rendu ─────────────────────────────────────────────────────────────────────
function ligneKV(k, v, cls) {
  return `<div class="kv ${cls || ""}"><span class="k">${k}</span><span class="v">${v}</span></div>`;
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function rendreApercu(res, applique) {
  const ap = $("apercu");
  const blocs = [];

  const champs = Object.entries(res.ecrit || {});
  const tags = res.tags_ajoutes || [];
  if (champs.length || tags.length) {
    let html = `<div class="bloc"><h4>${applique ? "Écrit dans la fiche" : "Sera écrit dans la fiche"}</h4>`;
    for (const [k, v] of champs) html += ligneKV(esc(k), esc(v));
    if (tags.length) html += ligneKV("tags +", tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(""));
    html += `</div>`;
    blocs.push(html);
  }

  const contacts = res.contacts || [];
  if (contacts.length) {
    let html = `<div class="bloc"><h4>Contacts</h4>`;
    for (const c of contacts) {
      const tag = applique ? (c.statut === "ok" ? "✓ " : `✗ (${esc(c.statut)}) `) : "";
      html += ligneKV(esc(c.type), tag + esc(c.valeur));
    }
    html += `</div>`;
    blocs.push(html);
  }

  const av = res.a_valider || [];
  if (av.length) {
    let html = `<div class="bloc valider"><h4>À valider à la main (non écrit)</h4>`;
    for (const l of av) html += ligneKV(esc(l.bloc + "/" + l.champ), `${esc(l.valeur)} <small class="note">(${esc(l.confiance)})</small>`);
    html += `<small class="note">Source affichée côté serveur. Jamais écrit automatiquement.</small></div>`;
    blocs.push(html);
  }

  if (!blocs.length) {
    blocs.push(`<div class="bloc"><small class="note">${esc(res.message || "Rien à proposer pour cette fiche.")}</small></div>`);
  } else if (res.message) {
    blocs.push(`<div class="bloc valider"><small class="note">${esc(res.message)}</small></div>`);
  }
  ap.innerHTML = blocs.join("");
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function apercu() {
  if (!PK) return setEtat("Aucune fiche détectée.", true);
  const cmd = $("cmd").value.trim();
  setEtat("Recherche web en cours… (peut prendre ~1 min)");
  setOccupe(true);
  $("row-ecrire").style.display = "none";
  $("apercu").innerHTML = "";
  try {
    const res = await appeler("/enrich", { pk: PK, command: cmd });
    rendreApercu(res, false);
    const aDuContenu = (Object.keys(res.ecrit || {}).length || (res.tags_ajoutes || []).length || (res.contacts || []).length);
    $("row-ecrire").style.display = aDuContenu ? "flex" : "none";
    setEtat(aDuContenu ? "Vérifie, puis « Écrire dans Orfeo »." : "Rien à écrire automatiquement.");
  } catch (e) {
    setEtat(e.message, true);
  } finally {
    setOccupe(false);
  }
}

async function ecrire() {
  if (!PK) return;
  setEtat("Écriture dans Orfeo…");
  setOccupe(true);
  try {
    const res = await appeler("/apply", { pk: PK, command: $("cmd").value.trim() });
    rendreApercu(res, true);
    $("row-ecrire").style.display = "none";
    setEtat("Terminé. Recharge la fiche Orfeo pour voir les changements.");
  } catch (e) {
    setEtat(e.message, true);
  } finally {
    setOccupe(false);
  }
}

function setEtat(t, bad) { const el = $("etat"); el.textContent = t; el.className = bad ? "bad" : ""; }
function setOccupe(b) { $("btn-apercu").disabled = b; $("btn-ecrire").disabled = b; }

// ── Câblage ───────────────────────────────────────────────────────────────────
$("btn-apercu").addEventListener("click", apercu);
$("btn-ecrire").addEventListener("click", ecrire);
$("cmd").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); apercu(); }
});
detecterCible();
