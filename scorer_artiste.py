"""
Scoring de compatibilité artiste ↔ lieu (structure) Orfeo — automation #5.

Pour un artiste donné, note chaque lieu Orfeo sur 100 afin de prioriser les
appels de prospection. Le score est écrit dans un CHAMP PERSONNALISÉ DÉDIÉ À
L'ARTISTE (« Note sur 100 pour <Artiste> »), et la justification (pourquoi cette
note + sources) est ajoutée dans le champ `notes` de la fiche.

  • La programmation passée d'une salle n'est PAS dans Orfeo → recherche web via
    l'API Claude (web search), comme l'enrichissement (automation #2).
  • Un champ personnalisé par artiste : `note_<slug>` (ex. `note_gipsy_kings`).
    Créé automatiquement à la volée (idempotent) en mode --apply.
  • Écriture directe dans Orfeo, mais SANS --apply aucune écriture (mode aperçu) ;
    le CSV `scoring_<slug>.csv` est toujours produit.
  • Batch : traite plusieurs fiches (--limit / --skip) ou des pk précis (--pks).

Règle absolue : ne jamais inventer. Aucune donnée fiable trouvée → score prudent,
confiance « basse » et justification qui l'explique. Jamais d'artiste programmé
ni de jauge inventés.

Variables d'environnement :
    ORFEO_TOKEN        Token API Orfeo (requis)
    ANTHROPIC_API_KEY  Clé API Claude (requise sauf en --list-only)
    ENRICH_MODEL       Modèle Claude (défaut : claude-haiku-4-5, le moins cher)
    WEB_SEARCH_MAX_USES  Plafond de recherches web par lieu (défaut : 3)

Usage :
    python3 scorer_artiste.py --artiste "Gipsy Kings" --list-only          # liste les lieux (gratuit)
    python3 scorer_artiste.py --artiste "Gipsy Kings" --limit 3            # aperçu (aucune écriture)
    python3 scorer_artiste.py --artiste "Gipsy Kings" --limit 3 --apply    # écrit note + justif
    python3 scorer_artiste.py --artiste "Céline Dion" --pks 15714299 --apply
"""

import os
import re
import sys
import csv
import json
import time
import datetime
import argparse
import unicodedata
import requests

BASE_URL = "https://orfeoapp.com/api"
TOKEN = os.environ.get("ORFEO_TOKEN", "")
# Modèle par défaut : le moins cher (Haiku 4.5, ~1$/5$ par M tokens).
MODEL = os.environ.get("ENRICH_MODEL", "claude-haiku-4-5")

# L'outil web search « dynamique » (_20260209) n'existe que sur Opus 4.6+/Sonnet 4.6.
# Pour les autres modèles (dont Haiku 4.5), il faut la variante de base _20250305.
MODELES_WEBSEARCH_DYNAMIQUE = (
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)

# Prix Claude ($ / million de tokens) — input, output. Web search facturé à part.
PRIX = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
}
PRIX_WEB_SEARCH = 0.01   # ~$10 / 1000 recherches
COUT_LOG = "cout_claude.log"


def cout_appel(modele, resp, etiquette=""):
    """Calcule et journalise le coût réel d'un appel Claude. Renvoie le coût en $."""
    try:
        u = resp.usage
        pin, pout = PRIX.get(modele, (5.0, 25.0))
        cin = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
        cout = getattr(u, "output_tokens", 0) or 0
        sw = getattr(u, "server_tool_use", None)
        recherches = getattr(sw, "web_search_requests", 0) if sw else 0
        prix = cin / 1e6 * pin + cout / 1e6 * pout + recherches * PRIX_WEB_SEARCH
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), COUT_LOG), "a", encoding="utf-8") as f:
            f.write(f"{etiquette}\t{modele}\tin={cin}\tout={cout}\tweb={recherches}\t${prix:.4f}\n")
        return prix
    except Exception:
        return 0.0


# ── Profils artistes ─────────────────────────────────────────────────────────
# Le modèle ne connaît pas les paramètres réels de tournée d'un artiste (jauge,
# cachet, esthétique) et devine mal (« star = grande salle »). On les lui donne.
# Clé = nom d'artiste en minuscules. Ajouter un artiste = ajouter une entrée.

PROFILS_ARTISTES = {
    "gipsy kings": {
        "esthetiques": "flamenco-pop, rumba gitane, world music, latino, variété festive internationale",
        "compatibles": ("FESTIVALS (généralistes, world, latino, été) = très bon fit ; "
                        "grandes salles de variété / pop grand public ; théâtres de ville "
                        "et scènes ayant une vraie jauge ET le budget"),
        "incompatibles": ("SMAC / scènes de musiques actuelles (budget en général insuffisant "
                          "pour le cachet → malus, sauf grosses exceptions) ; petites salles "
                          "< ~600 places ; chanson française intimiste (Biolay, Olivia Ruiz), "
                          "folk d'auteur, jazz pointu, rap/électro ; format 100% acoustique/intimiste"),
        "jauge_ideale": "~1000-1800 places (remplit 1200-2000 selon le territoire)",
        "jauge_eviter": "moins de ~600 (cachet non absorbable) ; plus de ~2500 / zénith / aréna (ne remplit pas)",
        "format": "groupe amplifié, vraie scène et sono ; jamais acoustique/intimiste",
        "cachet": "~15-20 k€ — le lieu doit avoir ce budget ; les SMAC ne l'ont en général pas",
    },
}


def profil_artiste(nom):
    return PROFILS_ARTISTES.get((nom or "").strip().lower())


# Schéma de sortie : score borné + justification + signaux + sources + confiance,
# plus la jauge estimée et le type de lieu (non décisifs, mais filtrables par l'utilisateur).
SCHEMA_SCORE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["score", "justification", "signaux", "sources", "confiance",
                 "jauge_estimee", "type_lieu"],
    "properties": {
        # Borne 0-100 imposée par le prompt puis re-bornée en code : l'API Claude
        # n'accepte pas minimum/maximum sur un integer dans output_config.
        "score": {"type": "integer"},
        "justification": {"type": "string"},
        "signaux": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "confiance": {"type": "string", "enum": ["haute", "moyenne", "basse"]},
        # Filtrables par l'utilisateur (il affine la jauge lui-même).
        "jauge_estimee": {"type": ["integer", "null"]},
        "type_lieu": {"type": ["string", "null"]},
    },
}


# ── Orfeo ────────────────────────────────────────────────────────────────────

def orfeo_headers():
    return {"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"}


def get_all(path):
    results, url = [], f"{BASE_URL}{path}"
    sep = "&" if "?" in path else "?"
    url = f"{url}{sep}page_size=200"
    while url:
        r = requests.get(url, headers=orfeo_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        results.extend(data.get("results", []))
        url = data.get("next")
        time.sleep(0.12)
    return results


def structures_a_scorer(limite, skip=0):
    """Retourne `limite` structures (après `skip`), tous lieux confondus."""
    besoin = skip + limite
    out, url = [], f"{BASE_URL}/structure/?page_size=100"
    while url and len(out) < besoin:
        r = requests.get(url, headers=orfeo_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data if isinstance(data, list) else data.get("results", [])
        out.extend(results)
        url = None if isinstance(data, list) else data.get("next")
        time.sleep(0.12)
    return out[skip:skip + limite]


def structures_par_pks(pks):
    """Retourne les structures correspondant à une liste de pk explicites."""
    out = []
    for pk in pks:
        s = get_structure(pk)
        if s:
            out.append(s)
        else:
            print(f"  ⚠  pk={pk} introuvable")
        time.sleep(0.12)
    return out


def get_structure(pk):
    """Détail complet d'une structure (inclut notes + custom_fields)."""
    r = requests.get(f"{BASE_URL}/structure/{pk}/", headers=orfeo_headers(), timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"  ⚠  pk={pk} : HTTP {r.status_code}")
    return None


def patch_structure(pk, payload):
    return requests.patch(f"{BASE_URL}/structure/{pk}/", headers=orfeo_headers(),
                          json=payload, timeout=15)


# ── Champ personnalisé par artiste ───────────────────────────────────────────

def slug_artiste(nom):
    """« Gipsy Kings » -> « gipsy_kings » (ASCII, minuscules, underscores)."""
    base = unicodedata.normalize("NFKD", nom or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", base).strip("_").lower()
    return slug or "artiste"


def trouver_champ_artiste(artiste):
    """Cherche le champ personnalisé dont le label correspond à l'artiste (insensible
    à la casse). Renvoie (cle, field_type) ou (None, None).

    L'API Orfeo ne permet PAS de créer un champ personnalisé (POST /custom_field/
    renvoie 500) : il doit être créé UNE FOIS à la main dans Orfeo (Réglages →
    Champs personnalisés). S'il manque, le score n'est écrit que dans la section
    « Notes » de la fiche, pas dans un champ dédié."""
    cible = (artiste or "").strip().lower()
    for f in get_all("/custom_field/"):
        if (f.get("label") or "").strip().lower() == cible:
            return f.get("key"), f.get("field_type")
    return None, None


# ── Section « Notes » de la fiche (objets /api/note/) ────────────────────────
# Attention : le champ `structure.notes` (API) = section « Description » dans
# l'UI (description du lieu). La section « Notes » est une liste d'objets Note
# rattachés à la fiche par `object_id` = pk de la structure.

def notes_de_structure(pk):
    r = requests.get(f"{BASE_URL}/note/?object_id={pk}", headers=orfeo_headers(), timeout=15)
    r.raise_for_status()
    d = r.json()
    return d.get("results", d if isinstance(d, list) else [])


def construire_note_texte(artiste, score, justification, sources, date_str,
                          type_lieu=None, jauge_estimee=None):
    """Contenu de la note (section « Notes »), formaté avec <br> comme Orfeo.
    Le préfixe « Score <artiste> : » sert à retrouver/mettre à jour la note."""
    lignes = [f"Score {artiste} : {score}/100 ({date_str})"]
    meta = []
    if type_lieu:
        meta.append(f"Type : {type_lieu}")
    if jauge_estimee:
        meta.append(f"Jauge estimée : {jauge_estimee}")
    if meta:
        lignes.append(" — ".join(meta))
    lignes.append(justification.strip())
    srcs = [s for s in (sources or []) if s]
    if srcs:
        lignes.append("Sources : " + " ; ".join(srcs))
    return "<br>".join(lignes)


_MOTIF_NOTE_SCORE = re.compile(r"(?i)^\s*score .+?\s*:\s*\d+\s*/\s*100")


def notes_humaines(pk):
    """Notes d'échange saisies par l'humain (section « Notes »), hors note de
    scoring auto-générée. Récentes d'abord, tronquées. Sert de contexte interne
    au scoring — jamais généré ni pénalisé quand vide."""
    out = []
    for n in notes_de_structure(pk):
        c = (n.get("content") or "").replace("<br>", " ").strip()
        if not c or _MOTIF_NOTE_SCORE.match(c):
            continue
        out.append(f"[{(n.get('creation_date') or '')[:10]}] {c}"[:300])
    return out


def ecrire_note_scoring(pk, artiste, contenu):
    """Crée ou met à jour (idempotent) la note de scoring de l'artiste sur la fiche.
    Retrouvée par son préfixe « Score <artiste> : ». Renvoie (ok, action)."""
    prefixe = f"score {artiste} :".strip().lower()
    for n in notes_de_structure(pk):
        if (n.get("content") or "").strip().lower().startswith(prefixe):
            # La PATCH d'une note exige object_id dans le body (sinon 400).
            r = requests.patch(f"{BASE_URL}/note/{n['pk']}/", headers=orfeo_headers(),
                               json={"content": contenu, "object_id": pk}, timeout=15)
            return r.status_code in (200, 201), "mise à jour"
    r = requests.post(f"{BASE_URL}/note/", headers=orfeo_headers(),
                      json={"content": contenu, "object_id": pk}, timeout=15)
    return r.status_code in (200, 201), "créée"


# ── Claude (recherche web + sortie structurée) ───────────────────────────────

def web_search_tool(modele=None):
    modele = modele or MODEL
    version = "web_search_20260209" if modele in MODELES_WEBSEARCH_DYNAMIQUE else "web_search_20250305"
    max_uses = int(os.environ.get("WEB_SEARCH_MAX_USES", "3"))
    return {"type": version, "name": "web_search", "max_uses": max_uses}


def texte_semble_corrompu(t):
    """Détecte un texte au découpage anormal (glitch Haiku : espaces intra-mots),
    ex. « Mauva is f it ar t is t ique ». Français normal : ~15-25% de mots courts."""
    mots = re.findall(r"[A-Za-zÀ-ÿ]+", t or "")
    if len(mots) < 12:
        return False
    courts = sum(1 for w in mots if len(w) <= 2)
    return courts / len(mots) > 0.40


def scorer_via_claude(client, struct, artiste):
    """Recherche web la programmation passée du lieu, puis note sa compatibilité
    avec `artiste` sur 100. Renvoie le dict conforme à SCHEMA_SCORE, ou None."""
    nom = struct.get("name") or "(nom inconnu)"
    ville = struct.get("city") or "(ville inconnue)"
    tags = [t.get("name") for t in (struct.get("tags") or []) if t.get("name")]
    # `struct.notes` = « Description » du lieu (contexte utile pour juger).
    notes = (struct.get("notes") or "").strip()[:600]

    # Notes internes d'échange (section « Notes ») : facteur parmi d'autres, si présentes.
    humaines = notes_humaines(struct.get("pk") or struct.get("id"))
    if humaines:
        bloc_notes = (
            "\nNOTES INTERNES DE L'AGENCE (échanges/verdicts passés — à prendre en compte "
            "comme UN facteur parmi les autres, jamais comme critère dominant) :\n"
            + "\n".join(f"- {h}" for h in humaines[:8]) + "\n"
            "→ Si une note donne un verdict clair (déjà bouclé, refusé, « n'ira pas dessus », "
            "« rien à proposer »…), pondère le score en conséquence ; une collaboration passée "
            "positive est un léger plus. Ces notes priment sur l'apparence web quand elles sont explicites.\n"
        )
    else:
        bloc_notes = ""  # aucune note → on n'en parle pas : l'absence n'est jamais pénalisée

    # Profil de l'artiste : donné au modèle si connu, sinon consigne de recherche web.
    p = profil_artiste(artiste)
    if p:
        bloc_profil = (
            f"PROFIL DE L'ARTISTE « {artiste} » (à respecter) :\n"
            f"- Esthétiques : {p['esthetiques']}\n"
            f"- Lieux compatibles : {p['compatibles']}\n"
            f"- Lieux/formats à éviter : {p['incompatibles']}\n"
            f"- Jauge idéale : {p['jauge_ideale']}\n"
            f"- Jauge à éviter : {p['jauge_eviter']}\n"
            f"- Format scénique : {p['format']}\n"
            f"- Cachet : {p['cachet']}\n"
        )
    else:
        bloc_profil = (
            f"PROFIL DE L'ARTISTE « {artiste} » : inconnu de la base. Recherche d'abord "
            "sur le web son esthétique musicale, sa jauge de remplissage typique et son "
            "ordre de cachet, puis abaisse la confiance d'un cran (données estimées).\n"
        )

    prompt = (
        "Tu es un booker SENIOR d'une grande agence française de spectacles vivants. "
        "Tu prépares une tournée et tu dois décider quels lieux appeler en priorité. "
        "Note sur 100 la question suivante, et ELLE SEULE :\n"
        f"« Programmer {artiste} dans ce lieu, est-ce un BON FEAT ARTISTIQUE et une "
        "date qui a du sens pour la ligne de programmation et le public du lieu ? »\n\n"
        "Contexte France : beaucoup de lieux sont aidés/subventionnés — ne raisonne PAS "
        "rentabilité commerciale ni box-office. Mais le lieu doit avoir le BUDGET du cachet.\n\n"
        f"{bloc_profil}\n"
        f"LIEU À ÉVALUER :\n"
        f"- Nom : {nom}\n"
        f"- Ville : {ville}\n"
        f"- Tags Orfeo : {', '.join(tags) or '(aucun)'}\n"
        f"- Description Orfeo : {notes or '(vide)'}\n"
        f"{bloc_notes}\n"
        "ÉTAPES :\n"
        "1. Recherche sur le web : programmation passée du lieu (artistes déjà programmés), "
        "genre dominant, JAUGE/capacité, TYPE (festival, SMAC, zénith, aréna, salle de "
        "variété, théâtre de ville, club, centre culturel…), et s'il programme/achète "
        "vraiment (vs simple location de salle).\n"
        "2. Juge, par ordre de poids :\n"
        "   a) FIT ARTISTIQUE (dominant) : le lieu programme-t-il l'esthétique de l'artiste ? "
        "a-t-il accueilli des artistes COMPARABLES (pas le style à l'identique) ? son public "
        "accueillerait-il ce spectacle ?\n"
        "   b) BUDGET : le lieu a-t-il les moyens du cachet indiqué ? (malus net pour les SMAC, "
        "sauf grosses exceptions ; festivals / grandes salles / collectivités dotées = ok).\n"
        "   c) ÉCHELLE (plausibilité, pas rentabilité) : jauge dans le bon ordre de grandeur — "
        "TROP GRAND (zénith/aréna) = MALUS autant que trop petit ; ne récompense jamais une "
        "grande jauge en soi.\n"
        "   d) GATE : format scénique compatible (un lieu acoustique/intimiste pour un groupe "
        "amplifié = rédhibitoire) ; un lieu en simple location sans programmation = très faible.\n"
        "3. Donne un score 0-100 (100 = évidence, appelle en premier ; 0 = aucun sens).\n\n"
        "RÈGLE ABSOLUE : ne jamais inventer. Information introuvable → score prudent, "
        "\"confiance\":\"basse\", et explique le manque de données. N'invente aucun artiste "
        "programmé ni aucune capacité.\n"
        "- justification : 2 à 4 phrases en français expliquant le score.\n"
        "- signaux : 2 à 5 faits courts ayant pesé (fit artistique, budget, jauge, type, format).\n"
        "- jauge_estimee : capacité en nombre de places (entier) si trouvée/estimable, sinon null.\n"
        "- type_lieu : type court du lieu (ex. « festival world », « SMAC », « zénith », "
        "« théâtre de ville »), ou null.\n"
        "- sources : URLs réellement consultées. Cite au moins une source si confiance haute/moyenne."
    )

    messages = [{"role": "user", "content": prompt}]
    tools = [web_search_tool()]

    resp = None
    for _ in range(6):  # outils serveur : relancer sur pause_turn
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA_SCORE}},
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    if resp:
        cout_appel(MODEL, resp, "score")
    texte = next((b.text for b in resp.content if b.type == "text"), None) if resp else None
    if not texte:
        return None
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        return None


def valeur_champ(score, field_type):
    """Valeur écrite dans le champ perso : entier si le champ est de type
    « number », sinon chaîne (« 25 ») pour un champ texte."""
    return score if field_type == "number" else str(score)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scoring de compatibilité artiste ↔ lieu Orfeo")
    parser.add_argument("--artiste", required=True,
                        help="Nom de l'artiste à scorer (ex. \"Gipsy Kings\")")
    parser.add_argument("--limit", type=int, default=25,
                        help="Nombre de lieux à traiter (défaut : 25)")
    parser.add_argument("--skip", type=int, default=0,
                        help="Sauter les N premiers lieux")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit réellement dans Orfeo (sinon aperçu seul)")
    parser.add_argument("--list-only", action="store_true",
                        help="Liste seulement les lieux, sans appeler Claude (gratuit)")
    parser.add_argument("--pks", type=str, default="",
                        help="Cible des structures précises par pk (séparées par des virgules). "
                             "Ignore --limit/--skip.")
    args = parser.parse_args()

    if not TOKEN:
        print("ERREUR : ORFEO_TOKEN non défini.")
        sys.exit(1)

    artiste = args.artiste.strip()
    slug = slug_artiste(artiste)
    csv_path = f"scoring_{slug}.csv"

    if args.pks:
        pks = [p.strip() for p in args.pks.split(",") if p.strip()]
        print(f"Ciblage explicite de {len(pks)} structure(s) par pk…")
        candidats = structures_par_pks(pks)
    else:
        print(f"Récupération des lieux (max {args.limit}, après {args.skip} sautés)…")
        candidats = structures_a_scorer(args.limit, args.skip)
    if not candidats:
        print("Aucun lieu à traiter. Rien à faire.")
        return
    print(f"{len(candidats)} lieu(x) à traiter. Artiste : « {artiste} ».\n")

    if args.list_only:
        for s in candidats:
            print(f"  • {s.get('name')} ({s.get('city')}) — pk={s.get('pk') or s.get('id')}")
        print("\n(--list-only : aucun appel Claude, aucune écriture.)")
        return

    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        print("ERREUR : ANTHROPIC_API_KEY non défini (requis hors --list-only).")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic()

    # Champ perso de l'artiste : retrouvé par son label (l'API ne peut pas le créer).
    cle, field_type = trouver_champ_artiste(artiste)
    if cle:
        print(f"Champ perso trouvé : {cle!r} (type {field_type}). Le score y sera écrit.")
    else:
        print(f"⚠  Aucun champ perso nommé « {artiste} » dans Orfeo.\n"
              f"   Crée-le à la main (Réglages → Champs personnalisés) pour y stocker le score.\n"
              f"   En attendant, seule la note (section « Notes ») est remplie.")

    mode = "ÉCRITURE RÉELLE (--apply)" if args.apply else "APERÇU (aucune écriture)"
    print(f"Mode : {mode} | Modèle : {MODEL}\n")

    date_str = datetime.date.today().isoformat()
    lignes_csv = []

    for s in candidats:
        pk = s.get("pk") or s.get("id")
        # Détail complet (Description + custom_fields) pour un prompt riche et une écriture sûre.
        s_full = get_structure(pk) or s
        time.sleep(0.12)
        nom = s_full.get("name")
        print(f"→ {nom} ({s_full.get('city')}) [pk={pk}]")

        try:
            data = scorer_via_claude(client, s_full, artiste)
            # Glitch Haiku intermittent : justification au découpage cassé → un nouvel essai.
            if data and texte_semble_corrompu(data.get("justification", "")):
                print("    ↻ sortie corrompue (glitch modèle) — nouvel essai")
                data = scorer_via_claude(client, s_full, artiste) or data
        except Exception as e:
            msg = str(e)
            # Erreur permanente (crédit épuisé / auth) → inutile de continuer :
            # on arrête proprement pour sauvegarder le CSV des lieux déjà traités.
            if any(k in msg.lower() for k in ("credit balance", "authentication", "invalid api key", "401")):
                print(f"    ✗ Arrêt : {msg[:160]}")
                print("    → Recharge les crédits Anthropic (Plans & Billing) puis relance.")
                break
            print(f"    ⚠  Erreur sur ce lieu, on continue : {msg[:160]}")
            continue
        if not data:
            print("    ⚠  Pas de résultat exploitable.")
            continue

        score = max(0, min(100, int(data["score"])))  # re-borne 0-100 par sécurité
        conf = data["confiance"]
        justif = data["justification"]
        sources = data.get("sources", [])
        signaux = data.get("signaux", [])
        jauge = data.get("jauge_estimee")
        type_lieu = data.get("type_lieu")
        print(f"    score = {score}/100 (confiance {conf}) — {type_lieu or 'type ?'}"
              + (f", ~{jauge} places" if jauge else ""))
        if signaux:
            print(f"    signaux : {', '.join(signaux[:5])}")
        print(f"    justif : {justif[:110]}…" if len(justif) > 110 else f"    justif : {justif}")

        ecrit = False
        if args.apply:
            # 1. Justification → section « Notes » (objet /api/note/), idempotent.
            contenu = construire_note_texte(artiste, score, justif, sources, date_str,
                                            type_lieu=type_lieu, jauge_estimee=jauge)
            ok_note, action = ecrire_note_scoring(pk, artiste, contenu)
            time.sleep(0.12)
            print(f"    {'✓' if ok_note else '✗'} note {action} dans « Notes »")

            # 2. Score (entier seul) → champ perso dédié, s'il existe.
            ok_champ = True
            if cle:
                existing_cf = s_full.get("custom_fields")
                existing_cf = existing_cf if isinstance(existing_cf, dict) else {}
                rc = patch_structure(pk, {"custom_fields": {**existing_cf, cle: valeur_champ(score, field_type)}})
                ok_champ = rc.status_code in (200, 201)
                time.sleep(0.12)
                print(f"    {'✓' if ok_champ else '✗'} champ « {artiste} » = {score}"
                      + ("" if ok_champ else f" (HTTP {rc.status_code})"))
            else:
                print("    ⚠  champ perso absent — score non écrit dans un champ (note seule)")
            ecrit = ok_note and ok_champ

        lignes_csv.append({
            "pk": pk, "nom": nom, "ville": s_full.get("city"),
            "score": score, "confiance": conf,
            "type_lieu": type_lieu or "", "jauge_estimee": jauge if jauge else "",
            "justification": justif,
            "signaux": " ; ".join(signaux),
            "sources": " ; ".join(s for s in sources if s),
            "ecrit": "oui" if ecrit else "non",
        })
        print()

    if lignes_csv:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(lignes_csv[0].keys()))
            w.writeheader()
            w.writerows(lignes_csv)
        ecrits = sum(1 for l in lignes_csv if l["ecrit"] == "oui")
        print(f"→ {len(lignes_csv)} score(s) dans {csv_path} ({ecrits} écrit(s) dans Orfeo)")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
