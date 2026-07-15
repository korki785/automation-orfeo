// popup.js — UI seule. Parle au serveur local (127.0.0.1:8723), jamais à Orfeo directement.
//
// Un seul geste : « Compléter la fiche » lance TOUT en parallèle (enrichissement +
// score), montre un aperçu groupé, puis « Tout écrire » applique tout d'un coup.
//
// Trois canaux d'écriture, et ils ne sont pas interchangeables :
//   • API Orfeo    → adresse, tags, contacts (PATCH) et score (champ perso + note)
//   • formulaire   → le site web, que l'API refuse d'écrire (web_addresses read_only)
//   • écran        → les champs custom repérés par la vision (tu enregistres dans Orfeo)
const HELPER = "http://127.0.0.1:8723";
const ARTISTE = "Gipsy Kings";   // seul artiste doté d'un champ perso dans Orfeo

const $ = (id) => document.getElementById(id);
let PK = null, TAB = null;
let API = null, ECRAN = [], SITE = null, SCORE = null;

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

// Ce qui se joue dans la page Orfeo (remplissage du site web) n'apparaît dans
// aucun log serveur. On le fait remonter dans debug_extension.log — sans ça, un
// échec est invisible et on en est réduit aux hypothèses.
function journaliser(quoi) {
  fetch(`${HELPER}/debug`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(quoi),
  }).catch(() => {});   // le diagnostic ne doit jamais casser le flux
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

// `applique` : false = aperçu (ce qui SERA écrit) · true = compte rendu d'écriture.
function rendre(applique) {
  const blocs = [];

  // 1. API Orfeo — adresse, tags, contacts.
  const champs = Object.entries((API && API.ecrit) || {});
  const tags = (API && API.tags_ajoutes) || [];
  if (champs.length || tags.length) {
    let h = `<div class="bloc"><h4>${applique ? "✓ Écrit via l'API" : "API — sera écrit"}</h4>`;
    for (const [k, v] of champs) h += kv(esc(k), esc(v));
    if (tags.length) h += kv("tags +", tags.map((t) => `<span class="tag">${esc(t)}</span>`).join(""));
    blocs.push(h + `</div>`);
  }

  const contacts = (API && API.contacts) || [];
  if (contacts.length) {
    let h = `<div class="bloc"><h4>Contacts (API)</h4>`;
    for (const c of contacts) {
      const t = applique ? (c.statut === "ok" ? "✓ " : `✗ (${esc(c.statut)}) `) : "";
      h += kv(esc(c.type), t + esc(c.valeur));
    }
    blocs.push(h + `</div>`);
  }

  // 2. Site web — passe par le formulaire de la fiche, l'API refuse ce champ.
  if (SITE) {
    let h = `<div class="bloc"><h4>${applique ? (SITE.statut === "ok" ? "✓ Site web enregistré" : "✗ Site web") : "Site web — via le formulaire de la fiche"}</h4>`;
    h += kv("site", `${esc(SITE.valeur)} <small class="note">(${esc(SITE.confiance)})</small>`);
    if (applique && SITE.statut !== "ok") h += kv("échec", `<small class="note">${esc(SITE.statut)}</small>`);
    blocs.push(h + `</div>`);
  }

  // 3. Score de compatibilité — champ perso + note.
  if (SCORE) {
    const meta = [SCORE.type_lieu,
                  SCORE.jauge_estimee ? `~${SCORE.jauge_estimee} places` : null,
                  SCORE.discussion ? `discussion ${SCORE.discussion}` : null].filter(Boolean).join(" · ");
    let h = `<div class="bloc"><h4>${applique ? "✓ Score écrit" : "Score — sera écrit"}</h4>`;
    h += kv(esc(ARTISTE), `<span class="score">${esc(SCORE.score)}/100</span> <small class="note">(${esc(SCORE.confiance)})</small>`);
    if (meta) h += kv("lieu", `<small class="note">${esc(meta)}</small>`);
    if (SCORE.justification) h += kv("pourquoi", esc(SCORE.justification));
    if ((SCORE.signaux || []).length) h += kv("signaux", esc(SCORE.signaux.slice(0, 4).join(" ; ")));
    if (applique) {
      h += kv("champ perso", SCORE.champ_ok === true ? "✓ écrit" : SCORE.champ_ok === false ? "✗ échec" : "—");
      h += kv("note", SCORE.note_action ? `✓ ${esc(SCORE.note_action)}` : "—");
    }
    blocs.push(h + `</div>`);
  }

  // 4. Champs custom repérés par la vision (remplis à l'écran, tu enregistres).
  if (ECRAN.length) {
    let h = `<div class="bloc"><h4>${applique ? "Rempli à l'écran (enregistre dans Orfeo)" : "Écran — à remplir"}</h4>`;
    for (const c of ECRAN) h += kv(esc(c.field_id), `${esc(c.valeur)} <small class="note">(${esc(c.confiance)})</small>`);
    blocs.push(h + `</div>`);
  }

  // 5. Jamais écrit automatiquement.
  const av = (API && API.a_valider) || [];
  if (av.length) {
    let h = `<div class="bloc valider"><h4>À valider à la main (non écrit)</h4>`;
    for (const l of av) h += kv(esc(l.bloc + "/" + l.champ), `${esc(l.valeur)} <small class="note">(${esc(l.confiance)})</small>`);
    blocs.push(h + `<small class="note">Jamais écrit automatiquement.</small></div>`);
  }

  const messages = [API && API.message, SCORE && SCORE.message].filter(Boolean);
  if (messages.length) {
    blocs.push(`<div class="bloc valider"><small class="note">${esc(messages.join(" "))}</small></div>`);
  }
  if (!blocs.length) blocs.push(`<div class="bloc"><small class="note">Rien à proposer sur cette fiche.</small></div>`);
  $("apercu").innerHTML = blocs.join("");
}

function aQuelqueChose() {
  const aApi = API && (Object.keys(API.ecrit || {}).length
                    || (API.tags_ajoutes || []).length
                    || (API.contacts || []).length);
  return Boolean(aApi || SITE || SCORE || ECRAN.length);
}

// ── Compléter la fiche : enrichissement + score, en parallèle ─────────────────
async function completer() {
  if (!PK) return setEtat("Aucune fiche détectée.", true);
  const vision = $("vision").checked;
  API = null; ECRAN = []; SITE = null; SCORE = null;
  setOccupe(true); $("row-actions").style.display = "none"; $("apercu").innerHTML = "";
  setEtat(`Recherche web + score ${ARTISTE}… (~1 min` + (vision ? ", vision lente" : "") + ")");

  const commande = $("cmd").value.trim();

  // Les deux recherches sont indépendantes : lancées ensemble, on attend la plus lente.
  // Un échec d'un côté ne doit pas effacer le résultat de l'autre.
  const tacheEnrich = (async () => {
    if (!vision) return { api: await appeler("/enrich", { pk: PK, command: commande }), ecran: [] };
    const [fields, screenshot] = await Promise.all([collecterChamps(), capturer()]);
    const res = await appeler("/visuel", { pk: PK, command: commande, fields, screenshot });
    return { api: res.api, ecran: res.ecran || [] };
  })();
  const tacheScore = appeler("/score", { pk: PK, artiste: ARTISTE });

  const [rEnrich, rScore] = await Promise.allSettled([tacheEnrich, tacheScore]);

  const soucis = [];
  if (rEnrich.status === "fulfilled") {
    API = rEnrich.value.api;
    ECRAN = rEnrich.value.ecran;
    SITE = (API && API.site_web) || null;
  } else {
    soucis.push(`enrichissement : ${rEnrich.reason.message}`);
  }
  if (rScore.status === "fulfilled") SCORE = rScore.value;
  else soucis.push(`score : ${rScore.reason.message}`);

  journaliser({
    etape: "apercu", pk: PK,
    api_ecrit: API && API.ecrit, api_tags: API && API.tags_ajoutes,
    api_contacts: API && API.contacts, api_message: API && API.message,
    site_web: SITE, score: SCORE && SCORE.score, soucis,
  });

  rendre(false);
  const pret = aQuelqueChose();
  $("row-actions").style.display = pret ? "flex" : "none";
  setOccupe(false);

  if (soucis.length && !pret) return setEtat(soucis.join(" · "), true);
  if (soucis.length) return setEtat(`Vérifie, puis « Tout écrire ». (${soucis.join(" · ")})`, true);
  setEtat(pret ? "Vérifie, puis « Tout écrire »." : "Rien à écrire sur cette fiche.");
}

// ── Tout écrire : API + site web + score, chacun indépendant ──────────────────
// Une écriture qui échoue n'annule pas les autres : chaque canal rapporte son sort.
async function toutEcrire() {
  if (!PK) return;
  setOccupe(true);
  const faits = [], rates = [];

  // 1. API — adresse, tags, contacts.
  const aApi = API && (Object.keys(API.ecrit || {}).length
                    || (API.tags_ajoutes || []).length
                    || (API.contacts || []).length);
  if (aApi) {
    setEtat("Écriture API (adresse, tags, contacts)…");
    try {
      const res = await appeler("/apply", { pk: PK, command: $("cmd").value.trim() });
      // /apply relit la fiche : il renvoie le plan réellement appliqué.
      const site = SITE;                 // le site n'est pas de son ressort, on le garde
      API = res; SITE = site;
      faits.push("API");
      journaliser({ etape: "apply", pk: PK, ecrit: res.ecrit, tags: res.tags_ajoutes,
                    contacts: res.contacts, message: res.message });
    } catch (e) {
      rates.push(`API (${e.message})`);
      journaliser({ etape: "apply", pk: PK, erreur: e.message });
    }
  }

  // 2. Score — champ perso + note.
  if (SCORE) {
    setEtat(`Écriture du score ${ARTISTE}…`);
    try {
      const res = await appeler("/score_apply", { pk: PK, artiste: ARTISTE });
      SCORE = res;
      if (res.champ_ok === false) rates.push("score (champ perso)");
      else faits.push("score");
    } catch (e) { rates.push(`score (${e.message})`); }
  }

  // 3. Site web — par le formulaire de la fiche. En dernier : Orfeo re-rend la zone.
  if (SITE) {
    setEtat("Écriture du site web dans la fiche…");
    try {
      const rep = await chrome.tabs.sendMessage(TAB.id, { type: "ORFEO_FILL_WEB", url: SITE.valeur });
      // Ce qui se passe dans la page est invisible côté serveur : on le lui raconte,
      // sinon un échec de remplissage ne laisse aucune trace exploitable.
      journaliser({ etape: "site_web", pk: PK, url: SITE.valeur, reponse: rep || null });
      if (rep && rep.ok) { SITE = { ...SITE, statut: "ok" }; faits.push("site web"); }
      else {
        SITE = { ...SITE, statut: (rep && rep.raison) || "échec" };
        rates.push(`site web (${SITE.statut})`);
      }
    } catch (e) {
      journaliser({ etape: "site_web", pk: PK, erreur: String(e && e.message) });
      // Typiquement : l'extension a été rechargée alors que l'onglet Orfeo était
      // déjà ouvert → le script de page est orphelin, il faut recharger la fiche.
      SITE = { ...SITE, statut: "page injoignable — recharge la fiche Orfeo (⌘R) et relance" };
      rates.push("site web (recharge la fiche Orfeo)");
    }
  }

  // 4. Champs custom (vision) : remplis à l'écran, c'est toi qui enregistres.
  let remplis = 0;
  if (ECRAN.length) {
    setEtat("Remplissage des champs à l'écran…");
    try {
      const rep = await chrome.tabs.sendMessage(TAB.id, { type: "ORFEO_FILL", plan: ECRAN });
      remplis = (rep && rep.filled) || 0;
      if (remplis) faits.push(`${remplis} champ(s) à l'écran`);
    } catch (e) { rates.push("champs à l'écran (recharge la fiche)"); }
  }

  rendre(true);
  setOccupe(false);

  // Aperçu expiré : le serveur a refusé d'écrire un plan que l'utilisateur n'a pas
  // vu. Ce n'est pas un échec, c'est un garde-fou → on le renvoie à l'aperçu.
  if (rates.some((r) => /expiré/i.test(r))) {
    $("row-actions").style.display = "none";
    return setEtat("Aperçu expiré (plus de 10 min). Reclique « Compléter la fiche », "
                   + "puis écris dans la foulée.", true);
  }

  $("row-actions").style.display = "none";
  let msg = faits.length ? `Écrit : ${faits.join(", ")}.` : "Rien n'a pu être écrit.";
  if (remplis) msg += " Clique Enregistrer dans Orfeo pour les champs à l'écran.";
  else if (faits.length) msg += " Recharge la fiche pour voir.";
  if (rates.length) msg += ` Échec : ${rates.join(", ")}.`;
  setEtat(msg, rates.length > 0);
}

function setEtat(t, bad) { const el = $("etat"); el.textContent = t; el.className = bad ? "bad" : ""; }
function setOccupe(b) { for (const id of ["btn-completer", "btn-tout"]) $(id).disabled = b; }

$("btn-completer").addEventListener("click", completer);
$("btn-tout").addEventListener("click", toutEcrire);
$("cmd").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); completer(); } });
detecterCible();
