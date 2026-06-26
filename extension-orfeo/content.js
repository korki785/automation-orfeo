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

// ── Messages depuis le popup ─────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "ORFEO_GET_CONTEXT") {
    sendResponse({ pk: pkDepuisURL(), nom: nomDepuisDOM(), url: location.href });
  } else if (msg.type === "ORFEO_COLLECT_FIELDS") {
    sendResponse({ fields: collecterChamps() });
  } else if (msg.type === "ORFEO_FILL") {
    sendResponse({ filled: remplirChamps(msg.plan) });
  }
  return true;
});
