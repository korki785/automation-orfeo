"""
Serveur local d'enrichissement Orfeo — pont entre l'extension Chrome et la logique
Python existante (enrichir_structures.py).

  • N'écoute QUE sur 127.0.0.1 (jamais exposé sur le réseau).
  • Les clés (ORFEO_TOKEN, ANTHROPIC_API_KEY) restent dans .env, côté machine.
  • Réutilise la logique « deux bacs / ne jamais inventer / FR uniquement ».

Endpoints :
  GET  /health                        → {"ok": true}
  POST /enrich       {pk, command}    → APERÇU : ce qui SERAIT écrit (aucune écriture)
  POST /apply        {pk}             → ÉCRIT le plan prévisualisé pour ce pk (API Orfeo)
  POST /score        {pk, artiste}    → APERÇU du score de compatibilité artiste ↔ lieu
  POST /score_apply  {pk, artiste}    → ÉCRIT le score (champ perso + note « Notes »)

Flux : /enrich lance la recherche Claude (web search) une fois, met le résultat en
cache par pk ; /apply réécrit EXACTEMENT le plan prévisualisé sans relancer de recherche.
Même schéma aperçu → écriture pour /score → /score_apply.

Le SITE WEB ne passe pas par l'API : `structure.web_addresses` est read_only côté
serveur Orfeo (vérifié 2026-07-13). Il est renvoyé dans `site_web` et c'est
l'extension qui remplit le formulaire inline de la fiche (voir content.js).

Usage :
    python3 serveur_enrichissement.py        # écoute sur 127.0.0.1:8723
"""

import os
import re
import json
import time
import datetime
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

# Import APRÈS le chargement du .env : les deux modules lisent ORFEO_TOKEN/MODEL à l'import.
import enrichir_structures as e   # noqa: E402
import scorer_artiste as sc       # noqa: E402


# ── État partagé (construit une seule fois, paresseusement) ───────────────────

_verrou = threading.Lock()
_refs = None
_client = None
_cache_plans = {}    # pk -> {"enrichi": {...}, "ts": float}
_cache_scores = {}   # "pk|artiste" -> {"data": {...}, "ts": float}
CACHE_TTL = 600      # secondes : un aperçu reste applicable ~10 min


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
    for cache in (_cache_plans, _cache_scores):
        for k in [k for k, v in cache.items() if maintenant - v["ts"] > CACHE_TTL]:
            cache.pop(k, None)


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

    res = e.appliquer_enrichissement(struct, enrichi, refs(), apply=False, pour_ecran=True)
    res["preview"] = True
    return 200, res


def action_visuel(corps):
    """Mode hybride (extension phase 3) : aperçu API + plan de remplissage à l'écran.
    Renvoie {api: <aperçu API>, ecran: [{field_id, valeur, source, confiance}]}."""
    pk = str(corps.get("pk") or "").strip()
    command = (corps.get("command") or "").strip()
    champs = corps.get("fields") or []
    shot = corps.get("screenshot") or ""
    if not pk:
        return 400, {"ok": False, "error": "pk manquant"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 500, {"ok": False, "error": "ANTHROPIC_API_KEY non défini dans .env"}

    struct = structure_par_pk(pk)
    if not struct:
        return 404, {"ok": False, "error": f"structure pk={pk} introuvable"}

    # Les deux appels Claude sont indépendants → on les lance en parallèle.
    # L'API n'écrit JAMAIS que cet ensemble fixe (logique deux-bacs) ; la vision
    # ignore donc ces libellés sans attendre le résultat API.
    # « site web » est dans la liste : il est rempli par le formulaire inline de la
    # fiche (remplissage déterministe), la vision n'a pas à le proposer en double.
    DEJA_API = ["adresse", "address1", "zipcode", "code postal", "région", "region",
                "notes", "tags", "téléphone", "telephone", "phone", "mail", "email",
                "site web", "site internet", "adresse web", "url"]
    b64 = (shot.split(",", 1)[1] if shot.startswith("data:") else shot) if shot else ""

    def tache_api():
        enrichi = e.enrichir_via_claude(client(), struct, refs(), commande=command)
        return enrichi, e.appliquer_enrichissement(struct, enrichi, refs(), apply=False,
                                                   pour_ecran=True)

    def tache_vision():
        if not (b64 and champs):
            return []
        return e.enrichir_visuel_via_claude(client(), struct, champs, b64, DEJA_API, command)

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        f_api = ex.submit(tache_api)
        f_vis = ex.submit(tache_vision)
        enrichi, api_res = f_api.result()
        try:
            ecran = f_vis.result()
        except Exception as exc:
            ecran = []
            api_res["message"] = (api_res.get("message") or "") + f"Vision indisponible : {exc}. "

    purger_cache()
    _cache_plans[pk] = {"enrichi": enrichi, "ts": time.time()}
    return 200, {"ok": True, "preview": True, "api": api_res, "ecran": ecran}


def action_apply(corps):
    pk = str(corps.get("pk") or "").strip()
    command = (corps.get("command") or "").strip()
    if not pk:
        return 400, {"ok": False, "error": "pk manquant"}

    struct = structure_par_pk(pk)   # re-lecture : état courant de la fiche
    if not struct:
        return 404, {"ok": False, "error": f"structure pk={pk} introuvable"}

    # On n'écrit QUE le plan que l'utilisateur a vu et validé. Si l'aperçu a expiré,
    # on refuse : relancer une recherche ici écrirait un plan que personne n'a
    # approuvé (Claude ne rend pas deux fois le même résultat).
    entree = _cache_plans.get(pk)
    if not entree or time.time() - entree["ts"] > CACHE_TTL:
        return 409, {"ok": False, "error": "Aperçu expiré (plus de 10 min). Relance « Compléter la fiche » "
                                           "avant d'écrire — sinon on écrirait autre chose que ce que tu as vu."}
    enrichi = entree["enrichi"]

    res = e.appliquer_enrichissement(struct, enrichi, refs(), apply=True, pour_ecran=True)
    _cache_plans.pop(pk, None)
    res["preview"] = False
    return 200, res


# ── Score de compatibilité artiste ↔ lieu (scorer_artiste.py) ─────────────────
# Deux écritures, toutes deux par l'API Orfeo (contrairement au site web) :
#   • le score seul   → champ personnalisé portant le nom de l'artiste (« Gipsy Kings »)
#   • la justification → une note de la section « Notes », idempotente (créée ou mise à jour)
# Le champ perso doit préexister dans Orfeo (Réglages → Champs personnalisés) :
# l'API refuse de le créer. S'il manque, seule la note est écrite.

ARTISTE_DEFAUT = "Gipsy Kings"


def _score_preview(pk, artiste):
    """Score via Claude (recherche web + historique d'e-mails), mis en cache."""
    struct = sc.get_structure(pk)
    if not struct:
        return None, None, (404, {"ok": False, "error": f"structure pk={pk} introuvable"})

    data = sc.scorer_via_claude(client(), struct, artiste)
    # Glitch Haiku intermittent (justification au découpage cassé) → un nouvel essai.
    if data and sc.texte_semble_corrompu(data.get("justification", "")):
        data = sc.scorer_via_claude(client(), struct, artiste) or data
    if not data:
        return None, None, (502, {"ok": False, "error": "Pas de score exploitable (Claude)."})

    data["score"] = max(0, min(100, int(data["score"])))   # re-borne 0-100 par sécurité
    purger_cache()
    _cache_scores[f"{pk}|{artiste}"] = {"data": data, "ts": time.time()}
    return struct, data, None


def _reponse_score(pk, artiste, data, applique, champ_ok=None, note_action=None, message=""):
    return {
        "ok": True, "preview": not applique, "applique": applique,
        "pk": pk, "artiste": artiste,
        "score": data["score"],
        "confiance": data.get("confiance"),
        "discussion": data.get("discussion"),
        "type_lieu": data.get("type_lieu"),
        "jauge_estimee": data.get("jauge_estimee"),
        "justification": data.get("justification"),
        "signaux": data.get("signaux") or [],
        "sources": data.get("sources") or [],
        "champ_ok": champ_ok, "note_action": note_action, "message": message,
    }


def action_score(corps):
    pk = str(corps.get("pk") or "").strip()
    artiste = (corps.get("artiste") or ARTISTE_DEFAUT).strip()
    if not pk:
        return 400, {"ok": False, "error": "pk manquant"}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return 500, {"ok": False, "error": "ANTHROPIC_API_KEY non défini dans .env"}

    _, data, err = _score_preview(pk, artiste)
    if err:
        return err

    cle, _ = sc.trouver_champ_artiste(artiste)
    message = "" if cle else (
        f"Champ personnalisé « {artiste} » absent d'Orfeo → seule la note sera écrite. "
        "(À créer une fois : Réglages → Champs personnalisés.)"
    )
    return 200, _reponse_score(pk, artiste, data, applique=False, message=message)


def action_score_apply(corps):
    pk = str(corps.get("pk") or "").strip()
    artiste = (corps.get("artiste") or ARTISTE_DEFAUT).strip()
    if not pk:
        return 400, {"ok": False, "error": "pk manquant"}

    # Même règle que /apply : on n'écrit que le score que l'utilisateur a vu.
    entree = _cache_scores.get(f"{pk}|{artiste}")
    if not entree or time.time() - entree["ts"] > CACHE_TTL:
        return 409, {"ok": False, "error": "Aperçu du score expiré (plus de 10 min). Relance "
                                           "« Compléter la fiche » avant d'écrire."}
    data = entree["data"]                 # écrit EXACTEMENT le score prévisualisé
    struct = sc.get_structure(pk)         # re-lecture : custom_fields à jour
    if not struct:
        return 404, {"ok": False, "error": f"structure pk={pk} introuvable"}

    # 1. Justification → section « Notes » (objet /api/note/), idempotente.
    contenu = sc.construire_note_texte(
        artiste, data["score"], data["justification"], data.get("sources") or [],
        datetime.date.today().isoformat(),
        type_lieu=data.get("type_lieu"), jauge_estimee=data.get("jauge_estimee"),
        discussion=data.get("discussion"),
    )
    note_ok, note_action = sc.ecrire_note_scoring(pk, artiste, contenu)

    # 2. Score seul → champ personnalisé dédié, s'il existe (fusion : jamais d'écrasement
    #    des autres champs perso de la fiche).
    cle, field_type = sc.trouver_champ_artiste(artiste)
    champ_ok, message = None, ""
    if cle:
        cf = struct.get("custom_fields")
        cf = cf if isinstance(cf, dict) else {}
        r = sc.patch_structure(pk, {"custom_fields": {**cf, cle: sc.valeur_champ(data["score"], field_type)}})
        champ_ok = r.status_code in (200, 201)
        if not champ_ok:
            message += f"Échec écriture du champ « {artiste} » (HTTP {r.status_code}). "
    else:
        message += f"Champ personnalisé « {artiste} » absent d'Orfeo → score non écrit dans un champ. "
    if not note_ok:
        message += "Échec écriture de la note. "

    _cache_scores.pop(f"{pk}|{artiste}", None)
    return 200, _reponse_score(pk, artiste, data, applique=True, champ_ok=champ_ok,
                               note_action=note_action, message=message)


# ── Journal de diagnostic (le navigateur nous raconte ce qu'il voit) ─────────
# Le remplissage du site web se passe dans la page Orfeo : aucun log serveur ne
# peut en témoigner. L'extension poste donc ici ce qu'elle a vu et fait, et on
# l'écrit dans un fichier lisible — sinon un échec de remplissage reste muet.

DEBUG_LOG = os.path.join(RACINE, "debug_extension.log")


def action_debug(corps):
    horodatage = datetime.datetime.now().isoformat(timespec="seconds")
    with open(DEBUG_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n=== {horodatage} ===\n")
        f.write(json.dumps(corps, ensure_ascii=False, indent=2) + "\n")
    return 200, {"ok": True}


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
            elif route == "/visuel":
                code, payload = action_visuel(corps)
            elif route == "/apply":
                code, payload = action_apply(corps)
            elif route == "/score":
                code, payload = action_score(corps)
            elif route == "/score_apply":
                code, payload = action_score_apply(corps)
            elif route == "/debug":
                code, payload = action_debug(corps)
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
    print("Endpoints : GET /health · POST /enrich · POST /apply · POST /score · POST /score_apply")
    print("Ctrl+C pour arrêter.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
        srv.shutdown()


if __name__ == "__main__":
    main()
