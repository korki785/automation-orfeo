"""
Cockpit « tâches prioritaires » Orfeo — page locale qui répond chaque matin :
« quelles tâches traiter en premier ? ».

Pourquoi : le tri natif d'Orfeo ne filtre que par égalité/texte (pas de « note > 60 »).
L'unité de travail réelle = la TÂCHE rattachée à une fiche. Une fiche sans tâche n'est
pas à traiter. On liste donc les tâches ouvertes, ordonnées par la VALEUR (score artiste)
de leur fiche ; le retard et le funnel ne servent qu'à départager à score égal.

Règles (verrouillées avec l'utilisateur) :
  • 1 ligne = 1 tâche (une fiche à 3 tâches = 3 lignes).
  • Exclut les tâches « PLUS TARD » (différées) et les tâches faites (done).
  • Le SCORE prime. Retard + funnel = départage seulement, jamais un bonus cumulé.
  • Tâches sans score = tout en bas (funnel présent d'abord entre elles).

Sécurité : n'écoute QUE sur 127.0.0.1 (refuse de démarrer sinon). ORFEO_TOKEN reste
dans .env, jamais transmis au navigateur (la page ne reçoit que le JSON des tâches).

Endpoints :
  GET /            → page HTML (tableau trié, vanilla JS)
  GET /api/taches  → calcul à la demande + cache mémoire (TTL) → JSON trié
  GET /health      → {"ok": true}

Usage :
    python3 serveur_cockpit.py         # écoute sur 127.0.0.1:8724
    python3 serveur_cockpit.py --dump  # imprime le JSON calculé et quitte (debug)
"""

import os
import sys
import json
import time
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

BASE_URL = "https://orfeoapp.com/api"
RACINE = os.path.dirname(os.path.abspath(__file__))


# ── Chargement du .env (le serveur peut être lancé hors d'un shell) ───────────

def charger_env():
    chemin = os.path.join(RACINE, ".env")
    if not os.path.exists(chemin):
        return
    with open(chemin, encoding="utf-8") as f:
        for ligne in f:
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, _, val = ligne.partition("=")
            os.environ.setdefault(cle.strip(), val.strip().strip('"').strip("'"))


charger_env()

TOKEN = os.environ.get("ORFEO_TOKEN", "")
HOTE = os.environ.get("COCKPIT_HOST", "127.0.0.1")
PORT = int(os.environ.get("COCKPIT_PORT", "8724"))
CACHE_TTL = int(os.environ.get("CACHE_TTL", "600"))   # secondes
SEUIL_SCORE = float(os.environ.get("SEUIL_SCORE", "0"))  # 0 = tout afficher ; sert d'abord à ORDONNER
TITRES_EXCLUS = {t.strip().upper() for t in os.environ.get("TITRES_EXCLUS", "PLUS TARD").split(";") if t.strip()}

# Ordre d'avancement du funnel (pour choisir le statut le plus avancé d'une fiche).
ORDRE_FUNNEL = {"Intérêt": 1, "Interet": 1, "Option": 2, "Confirmé": 3, "Confirme": 3}


# ── Orfeo API ─────────────────────────────────────────────────────────────────

def orfeo_headers():
    return {"Authorization": f"Token {TOKEN}"}


def get_all(path, params=None):
    results, url = [], f"{BASE_URL}{path}"
    if params:
        sep = "&" if "?" in path else "?"
        url += sep + "&".join(f"{k}={v}" for k, v in params.items())
    while url:
        r = requests.get(url, headers=orfeo_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        results.extend(data.get("results", []))
        url = data.get("next")
        time.sleep(0.12)   # respect ~10 req/s
    return results


# ── Calcul des tâches prioritaires ────────────────────────────────────────────

def _num(val):
    """Caste une valeur de custom_field en float, ou None si non numérique/vide."""
    if val is None:
        return None
    try:
        return float(str(val).strip().replace(",", "."))
    except (ValueError, AttributeError):
        return None


def _cles_artistes(spectacle_names):
    """Champs personnalisés dont le label est un nom d'artiste (= un spectacle).
    Renvoie {key: label}. Logge les champs entity numériques non appariés."""
    noms = {n.strip().lower() for n in spectacle_names if n}
    cles = {}
    for f in get_all("/custom_field/"):
        label = (f.get("label") or "").strip()
        obj = f.get("object_type") or f.get("content_type")
        key = f.get("key") or f.get("slug")
        if not (label and key):
            continue
        if obj in (None, "entity") and label.lower() in noms:
            cles[key] = label
    return cles


def _index_scores(cles_artistes):
    """{structure_pk : (score_max, artiste_du_max)} via /structure/ (custom_fields inline)."""
    idx = {}
    for s in get_all("/structure/", {"page_size": "200"}):
        pk = s.get("pk") or s.get("id")
        cf = s.get("custom_fields")
        if not (pk and isinstance(cf, dict)):
            continue
        best_score, best_art = None, None
        for key, label in cles_artistes.items():
            v = _num(cf.get(key))
            if v is not None and (best_score is None or v > best_score):
                best_score, best_art = v, label
        if best_score is not None:
            idx[pk] = (best_score, best_art)
    return idx


def _index_funnel():
    """{structure_pk : statut_le_plus_avancé} via /project/ (place + status inline).
    Renvoie aussi l'ensemble des noms de spectacles rencontrés."""
    funnel, spectacles = {}, set()
    for p in get_all("/project/", {"page_size": "100"}):
        place = p.get("place") or {}
        pk = place.get("pk") or place.get("id")
        spec = p.get("spectacle") or {}
        if spec.get("name"):
            spectacles.add(spec["name"])
        st = p.get("status") or {}
        nom = st.get("name") if isinstance(st, dict) else None
        if not (pk and nom):
            continue
        rang = ORDRE_FUNNEL.get(nom, 0)
        courant = funnel.get(pk)
        if courant is None or rang > ORDRE_FUNNEL.get(courant, 0):
            funnel[pk] = nom
    return funnel, spectacles


def _est_retard(due_date):
    if not due_date:
        return False
    try:
        return datetime.fromisoformat(due_date[:10]).date() < date.today()
    except ValueError:
        return False


def build_taches():
    """Calcule la liste triée des tâches prioritaires. Lit l'API à chaque appel."""
    # 1. Funnel + noms d'artistes (une passe /project/).
    funnel, spectacles = _index_funnel()
    # 2. Champs-artiste puis scores par structure.
    cles = _cles_artistes(spectacles)
    scores = _index_scores(cles)
    # 3. Tâches ouvertes, hors « PLUS TARD », rattachées à une structure.
    lignes = []
    for t in get_all("/task/", {"page_size": "200"}):
        if t.get("done") is not False:
            continue
        titre = (t.get("title") or "").strip()
        if titre.upper() in TITRES_EXCLUS:
            continue
        if t.get("content_type") != "structure":
            continue
        spk = t.get("object_id")
        score, artiste = scores.get(spk, (None, None))
        if SEUIL_SCORE > 0 and score is not None and score < SEUIL_SCORE:
            continue
        due = t.get("due_date")
        lignes.append({
            "titre": titre or "(sans titre)",
            "task_pk": t.get("pk"),
            "structure_pk": spk,
            "artiste": artiste,
            "score": int(score) if score is not None and score == int(score) else score,
            "statut": funnel.get(spk),
            "due_date": due,
            "retard": _est_retard(due),
            "lien": f"https://orfeoapp.com/structure/{spk}/" if spk else None,
        })
    lignes.sort(key=_cle_tri)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seuil": SEUIL_SCORE,
        "total": len(lignes),
        "taches": lignes,
    }


def _cle_tri(t):
    """Le score prime. Retard + funnel départagent seulement. Sans-score en bas."""
    retard_first = 0 if t["retard"] else 1
    funnel_first = 0 if t["statut"] else 1
    due = t["due_date"] or "9999-99-99"
    if t["score"] is not None:
        # Groupe A : score DESC, puis retard, puis funnel présent.
        return (0, -t["score"], retard_first, funnel_first, due)
    # Groupe B (toujours sous A) : funnel présent, puis retard, puis échéance.
    return (1, funnel_first, retard_first, due)


# ── Cache mémoire ─────────────────────────────────────────────────────────────

_verrou = threading.Lock()
_cache = {"data": None, "ts": 0.0}


def taches_cachees():
    with _verrou:
        if _cache["data"] is not None and time.time() - _cache["ts"] <= CACHE_TTL:
            return _cache["data"]
    data = build_taches()   # hors verrou : le calcul (API) peut être long
    with _verrou:
        _cache["data"], _cache["ts"] = data, time.time()
    return data


# ── Page HTML (inline) ────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tâches prioritaires — Orfeo</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #f6f7f9; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) { body { background: #16171a; color: #e8e8e8; } }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: #888; font-size: 13px; margin-bottom: 16px; }
  .card { background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
          overflow: auto; }
  @media (prefers-color-scheme: dark) { .card { background: #212228; box-shadow: none; } }
  table { border-collapse: collapse; width: 100%; }
  th, td { padding: 9px 14px; text-align: left; border-bottom: 1px solid #eee; white-space: nowrap; }
  @media (prefers-color-scheme: dark) { th, td { border-color: #303138; } }
  th { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #999; }
  .score { font-weight: 700; font-variant-numeric: tabular-nums; text-align: right; font-size: 17px; }
  .muted { color: #aaa; }
  .pill { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 12px;
          background: #e8eefb; color: #2456c9; }
  @media (prefers-color-scheme: dark) { .pill { background: #22345e; color: #9dc0ff; } }
  .retard { color: #d33; font-weight: 600; }
  .sep td { background: #f0f0f2; color: #999; font-size: 12px; text-transform: uppercase;
            letter-spacing: .05em; font-weight: 600; }
  @media (prefers-color-scheme: dark) { .sep td { background: #191a1e; } }
  a { color: inherit; text-decoration: none; border-bottom: 1px dotted #bbb; }
  button { font: inherit; padding: 6px 14px; border-radius: 8px; border: 1px solid #ccc;
           background: #fff; cursor: pointer; }
  @media (prefers-color-scheme: dark) { button { background: #2a2b31; border-color: #444; color: #ddd; } }
</style></head>
<body>
  <h1>Tâches prioritaires</h1>
  <div class="meta"><span id="meta">Chargement…</span> · <button onclick="charger()">↻ Rafraîchir</button></div>
  <div class="card"><table>
    <thead><tr><th style="text-align:right">Score</th><th>Tâche</th><th>Structure</th>
      <th>Artiste</th><th>Statut</th><th>Échéance</th></tr></thead>
    <tbody id="corps"></tbody>
  </table></div>
<script>
function esc(s){ return (s==null?'':String(s)).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function ligne(t){
  const score = t.score==null ? '<span class="muted">—</span>' : esc(t.score);
  const statut = t.statut ? '<span class="pill">'+esc(t.statut)+'</span>' : '<span class="muted">—</span>';
  const ech = t.due_date ? '<span class="'+(t.retard?'retard':'')+'">'+esc(t.due_date)+(t.retard?' ⚠':'')+'</span>'
                         : '<span class="muted">—</span>';
  const titre = t.lien ? '<a href="'+esc(t.lien)+'" target="_blank">'+esc(t.titre)+'</a>' : esc(t.titre);
  return '<tr><td class="score">'+score+'</td><td>'+titre+'</td><td>'+esc(t.structure_pk||'')
       + '</td><td>'+esc(t.artiste||'')+'</td><td>'+statut+'</td><td>'+ech+'</td></tr>';
}
async function charger(){
  const corps = document.getElementById('corps');
  corps.innerHTML = '<tr><td colspan="6" class="muted">Chargement…</td></tr>';
  try {
    const r = await fetch('/api/taches'); const d = await r.json();
    const items = d.taches || [];
    let html = '', separe = false;
    for (const t of items){
      if (t.score==null && !separe){ html += '<tr class="sep"><td colspan="6">Sans score</td></tr>'; separe = true; }
      html += ligne(t);
    }
    corps.innerHTML = html || '<tr><td colspan="6" class="muted">Aucune tâche.</td></tr>';
    document.getElementById('meta').textContent =
      d.total + ' tâche(s) · seuil ' + d.seuil + ' · maj ' + (d.generated_at||'').replace('T',' ');
  } catch(e){
    corps.innerHTML = '<tr><td colspan="6" class="retard">Erreur : '+esc(e.message)+'</td></tr>';
  }
}
charger();
</script>
</body></html>
"""


# ── Serveur HTTP ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _html(self, code, texte):
        corps = texte.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _json(self, code, payload):
        corps = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        route = self.path.rstrip("/") or "/"
        try:
            if route == "/":
                return self._html(200, PAGE)
            if route == "/health":
                return self._json(200, {"ok": True})
            if route == "/api/taches":
                return self._json(200, taches_cachees())
            return self._json(404, {"ok": False, "error": "route inconnue"})
        except Exception as ex:   # ne jamais planter le serveur sur une requête
            return self._json(500, {"ok": False, "error": f"{type(ex).__name__}: {ex}"})

    def log_message(self, *a):
        pass


def main():
    if "--dump" in sys.argv:
        print(json.dumps(build_taches(), ensure_ascii=False, indent=2))
        return
    if not TOKEN:
        print("ERREUR : ORFEO_TOKEN non défini (.env).")
        raise SystemExit(1)
    if HOTE not in ("127.0.0.1", "localhost", "::1"):
        print(f"ERREUR : cockpit local uniquement — COCKPIT_HOST={HOTE!r} n'est pas loopback.")
        raise SystemExit(1)
    srv = ThreadingHTTPServer((HOTE, PORT), Handler)
    print(f"Cockpit tâches prioritaires Orfeo → http://{HOTE}:{PORT}")
    print("Endpoints : GET / · GET /api/taches · GET /health · Ctrl+C pour arrêter.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
        srv.shutdown()


if __name__ == "__main__":
    main()
