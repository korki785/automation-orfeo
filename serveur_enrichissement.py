"""
Serveur local d'enrichissement Orfeo — pont entre l'extension Chrome et la logique
Python existante (enrichir_structures.py).

  • N'écoute QUE sur 127.0.0.1 (jamais exposé sur le réseau).
  • Les clés (ORFEO_TOKEN, ANTHROPIC_API_KEY) restent dans .env, côté machine.
  • Réutilise la logique « deux bacs / ne jamais inventer / FR uniquement ».

Endpoints :
  GET  /health                      → {"ok": true}
  POST /enrich  {pk, command}       → APERÇU : ce qui SERAIT écrit (aucune écriture)
  POST /apply   {pk}                → ÉCRIT le plan prévisualisé pour ce pk (API Orfeo)

Flux : /enrich lance la recherche Claude (web search) une fois, met le résultat en
cache par pk ; /apply réécrit EXACTEMENT le plan prévisualisé sans relancer de recherche.

Usage :
    python3 serveur_enrichissement.py        # écoute sur 127.0.0.1:8723
"""

import os
import re
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOTE = "127.0.0.1"
PORT = int(os.environ.get("ENRICH_PORT", "8723"))
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
            cle, val = cle.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(cle, val)


charger_env()

# Import APRÈS le chargement du .env : enrichir_structures lit ORFEO_TOKEN/MODEL à l'import.
import enrichir_structures as e  # noqa: E402


# ── État partagé (construit une seule fois, paresseusement) ───────────────────

_verrou = threading.Lock()
_refs = None
_client = None
_cache_plans = {}   # pk -> {"enrichi": {...}, "ts": float}
CACHE_TTL = 600     # secondes : un aperçu reste applicable ~10 min


def refs():
    global _refs
    if _refs is None:
        with _verrou:
            if _refs is None:
                _refs = e.Referentiels()
    return _refs


def client():
    global _client
    if _client is None:
        with _verrou:
            if _client is None:
                import anthropic
                _client = anthropic.Anthropic()
    return _client


def structure_par_pk(pk):
    lot = e.structures_par_pks([str(pk)])
    return lot[0] if lot else None


def purger_cache():
    maintenant = time.time()
    for k in [k for k, v in _cache_plans.items() if maintenant - v["ts"] > CACHE_TTL]:
        _cache_plans.pop(k, None)


# ── Actions ───────────────────────────────────────────────────────────────────

def action_enrich(corps):
    pk = str(corps.get("pk") or "").strip()
    command = (corps.get("command") or "").strip()
    if not pk:
        return 400, {"ok": False, "error": "pk manquant"}

    struct = structure_par_pk(pk)
    if not struct:
        return 404, {"ok": False, "error": f"structure pk={pk} introuvable"}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 500, {"ok": False, "error": "ANTHROPIC_API_KEY non défini dans .env"}

    enrichi = e.enrichir_via_claude(client(), struct, refs(), commande=command)
    purger_cache()
    _cache_plans[pk] = {"enrichi": enrichi, "ts": time.time()}

    res = e.appliquer_enrichissement(struct, enrichi, refs(), apply=False)
    res["preview"] = True
    return 200, res


def action_apply(corps):
    pk = str(corps.get("pk") or "").strip()
    command = (corps.get("command") or "").strip()
    if not pk:
        return 400, {"ok": False, "error": "pk manquant"}

    struct = structure_par_pk(pk)   # re-lecture : état courant de la fiche
    if not struct:
        return 404, {"ok": False, "error": f"structure pk={pk} introuvable"}

    entree = _cache_plans.get(pk)
    if entree and time.time() - entree["ts"] <= CACHE_TTL:
        enrichi = entree["enrichi"]      # applique exactement le plan prévisualisé
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return 500, {"ok": False, "error": "ANTHROPIC_API_KEY non défini dans .env"}
        enrichi = e.enrichir_via_claude(client(), struct, refs(), commande=command)

    res = e.appliquer_enrichissement(struct, enrichi, refs(), apply=True)
    _cache_plans.pop(pk, None)
    res["preview"] = False
    return 200, res


# ── Serveur HTTP ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _repondre(self, code, payload):
        corps = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            return self._repondre(200, {"ok": True})
        return self._repondre(404, {"ok": False, "error": "route inconnue"})

    def do_POST(self):
        route = self.path.rstrip("/")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            corps = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._repondre(400, {"ok": False, "error": "JSON invalide"})

        try:
            if route == "/enrich":
                code, payload = action_enrich(corps)
            elif route == "/apply":
                code, payload = action_apply(corps)
            else:
                code, payload = 404, {"ok": False, "error": "route inconnue"}
        except Exception as ex:  # ne jamais planter le serveur sur une requête
            code, payload = 500, {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
        self._repondre(code, payload)

    def log_message(self, *a):  # journal silencieux (pas de spam stdout)
        pass


def main():
    if not os.environ.get("ORFEO_TOKEN"):
        print("ERREUR : ORFEO_TOKEN non défini (.env).")
        raise SystemExit(1)
    srv = ThreadingHTTPServer((HOTE, PORT), Handler)
    print(f"Serveur d'enrichissement Orfeo → http://{HOTE}:{PORT}")
    print("Endpoints : GET /health · POST /enrich · POST /apply")
    print("Ctrl+C pour arrêter.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
        srv.shutdown()


if __name__ == "__main__":
    main()
