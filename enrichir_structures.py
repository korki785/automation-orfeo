"""
Enrichissement des fiches lieux (structure) Orfeo — automation #2.

Repère les structures à l'adresse incomplète, cherche les infos manquantes sur
le web via l'API Claude (web search), puis applique une logique en deux bacs.

  • Bac « fiable » → écriture directe dans Orfeo, RÉSERVÉE AUX LIEUX FRANÇAIS :
        - address1, zipcode      → trouvés par Claude (texte), si vides
        - region                 → DÉDUITE du code postal (référentiels Orfeo)
        - notes                  → court récap de contexte, si notes vide
        - tags                   → tags PRÉEXISTANTS pertinents (type de lieu,
                                   styles), choisis parmi le vocabulaire Orfeo,
                                   fusionnés avec les tags déjà posés
        - contacts (téléphone, mail générique) → POST /entitycontactinfo/
  • Bac « à valider » → site web, contact programmateur (nom+mail direct), et —
        pour les lieux étrangers — l'adresse/les contacts non écrits. Exporté en
        CSV avec source + niveau de confiance. JAMAIS écrit automatiquement.

Faits Orfeo vérifiés :
  - region écrivable ; department_id / country_id read-only (dérivés serveur).
    Écrire l'adresse d'un lieu ÉTRANGER peut effacer son country_id dérivé →
    l'écriture auto est donc réservée aux lieux français.
  - tags : champ marqué read-only en OPTIONS mais PATCH {"tags":[pk,…]} fonctionne.
    Toujours fusionner ; n'utiliser que des tags préexistants (/entitytag/).
  - contacts : POST /entitycontactinfo/ {type:'phone'|'mail', value, entity}.

Règle absolue : ne jamais inventer. Info introuvable → null / « non_trouve ».

Sécurité : SANS --apply, aucune écriture (mode aperçu). Le CSV est toujours produit.

Variables d'environnement :
    ORFEO_TOKEN        Token API Orfeo (requis)
    ANTHROPIC_API_KEY  Clé API Claude (requise sauf en --list-only)
    ENRICH_MODEL       Modèle Claude (défaut : claude-haiku-4-5, le moins cher)

Usage :
    python3 enrichir_structures.py --list-only            # liste les lieux incomplets (gratuit)
    python3 enrichir_structures.py --limit 3              # aperçu enrichi (aucune écriture)
    python3 enrichir_structures.py --limit 3 --apply      # écrit dans Orfeo
"""

import os
import sys
import csv
import json
import time
import argparse
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


CSV_A_VALIDER = "enrichissement_a_valider.csv"
CSV_APPLIQUE = "enrichissement_applique.csv"

COUNTRY_FRANCE_PK = 3

# Table code département (FR) → nom de région (orthographe Orfeo).
DEPTS_PAR_REGION = {
    "Auvergne-Rhône-Alpes": ["01", "03", "07", "15", "26", "38", "42", "43", "63", "69", "73", "74"],
    "Bourgogne-Franche-Comté": ["21", "25", "39", "58", "70", "71", "89", "90"],
    "Bretagne": ["22", "29", "35", "56"],
    "Centre-Val de Loire": ["18", "28", "36", "37", "41", "45"],
    "Corse": ["2A", "2B", "20"],
    "Grand Est": ["08", "10", "51", "52", "54", "55", "57", "67", "68", "88"],
    "Hauts-de-France": ["02", "59", "60", "62", "80"],
    "Île-de-France": ["75", "77", "78", "91", "92", "93", "94", "95"],
    "Normandie": ["14", "27", "50", "61", "76"],
    "Nouvelle-Aquitaine": ["16", "17", "19", "23", "24", "33", "40", "47", "64", "79", "86", "87"],
    "Occitanie": ["09", "11", "12", "30", "31", "32", "34", "46", "48", "65", "66", "81", "82"],
    "Pays de la Loire": ["44", "49", "53", "72", "85"],
    "Provence-Alpes-Côte d'Azur": ["04", "05", "06", "13", "83", "84"],
    "DROM": ["971", "972", "973", "974", "975", "976"],
}
CODE_DEPT_VERS_REGION = {code: region for region, codes in DEPTS_PAR_REGION.items() for code in codes}

# Tags internes (workflow / artistes / budgets) à NE JAMAIS assigner automatiquement.
# Liste en minuscules. Tout le reste du vocabulaire /entitytag/ est assignable
# (types de lieu, styles musicaux, rôles).
TAGS_INTERNES = {
    "budget 10k", "budget 12k", "budget 15k",
    "gipsy kings proposés", "gk casino tour", "gk fest", "gk smac tour",
    "int gipsy kings", "int gk", "int joy womack", "joy proposée",
    "kalvin love tour", "los mirlos proposés", "los mirlos tour", "make tour",
    "propose gipsy kings", "propose jean castel", "propose joy womack",
    "propose jungle sauce", "propose magic city hippies", "propose makéda manne",
    "mail only", "location uniquement", "rencontre", "semestre", "s2m",
    "damsec", "eat", "agent", "agents", "artist",
}

CONFIANCE = {"type": "string", "enum": ["haute", "moyenne", "basse", "non_trouve"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["adresse", "contact", "programmateur", "resume", "tags"],
    "properties": {
        "adresse": {
            "type": "object",
            "additionalProperties": False,
            "required": ["address1", "zipcode", "city", "pays", "source", "confiance"],
            "properties": {
                "address1": {"type": ["string", "null"]},
                "zipcode": {"type": ["string", "null"]},
                "city": {"type": ["string", "null"]},
                "pays": {"type": ["string", "null"]},
                "source": {"type": "string"},
                "confiance": CONFIANCE,
            },
        },
        "contact": {
            "type": "object",
            "additionalProperties": False,
            "required": ["email_generique", "telephone", "site_web", "source", "confiance"],
            "properties": {
                "email_generique": {"type": ["string", "null"]},
                "telephone": {"type": ["string", "null"]},
                "site_web": {"type": ["string", "null"]},
                "source": {"type": "string"},
                "confiance": CONFIANCE,
            },
        },
        "programmateur": {
            "type": "object",
            "additionalProperties": False,
            "required": ["nom", "email", "source", "confiance"],
            "properties": {
                "nom": {"type": ["string", "null"]},
                "email": {"type": ["string", "null"]},
                "source": {"type": "string"},
                "confiance": CONFIANCE,
            },
        },
        "resume": {
            "type": "object",
            "additionalProperties": False,
            "required": ["texte", "source", "confiance"],
            "properties": {
                "texte": {"type": ["string", "null"]},
                "source": {"type": "string"},
                "confiance": CONFIANCE,
            },
        },
        # Tags pertinents choisis PARMI le vocabulaire Orfeo fourni dans le prompt.
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


# ── Orfeo ────────────────────────────────────────────────────────────────────

def orfeo_headers():
    return {"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"}


def vide(valeur):
    return valeur in (None, "", [], {})


def propre(valeur):
    return valeur.strip() if isinstance(valeur, str) else valeur


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


def structures_incompletes(limite, skip=0):
    """Retourne `limite` structures à l'adresse incomplète, après en avoir sauté `skip`."""
    candidates = []
    besoin = skip + limite
    url = f"{BASE_URL}/structure/?page_size=100"
    while url and len(candidates) < besoin:
        r = requests.get(url, headers=orfeo_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        results = data if isinstance(data, list) else data.get("results", [])
        for s in results:
            if any(vide(s.get(c)) for c in ("address1", "zipcode", "region")):
                candidates.append(s)
                if len(candidates) >= besoin:
                    break
        url = None if isinstance(data, list) else data.get("next")
        time.sleep(0.12)
    return candidates[skip:skip + limite]


def structures_par_pks(pks):
    """Retourne les structures correspondant à une liste de pk explicites."""
    out = []
    for pk in pks:
        r = requests.get(f"{BASE_URL}/structure/{pk}/", headers=orfeo_headers(), timeout=15)
        if r.status_code == 200:
            out.append(r.json())
        else:
            print(f"  ⚠  pk={pk} introuvable (HTTP {r.status_code})")
        time.sleep(0.12)
    return out


def patch_structure(pk, payload):
    return requests.patch(f"{BASE_URL}/structure/{pk}/", headers=orfeo_headers(),
                          json=payload, timeout=15)


def contacts_existants(pk):
    r = requests.get(f"{BASE_URL}/entitycontactinfo/?entity={pk}", headers=orfeo_headers(), timeout=15)
    r.raise_for_status()
    d = r.json()
    return d.get("results", d if isinstance(d, list) else [])


def creer_contact(pk, type_, value):
    return requests.post(f"{BASE_URL}/entitycontactinfo/", headers=orfeo_headers(),
                         json={"type": type_, "value": value, "entity": pk}, timeout=15)


# ── Référentiels (chargés une fois) ──────────────────────────────────────────

class Referentiels:
    def __init__(self):
        deps = get_all("/department/")
        self.dept_pk_par_code = {d["code"]: d["pk"] for d in deps if d.get("code")}
        regs = get_all("/region/")
        self.region_pk_par_nom = {r["name"]: r["pk"] for r in regs if r.get("name")}
        pays = get_all("/country/")
        self.country_pk_par_nom = {c["name"].lower(): c["pk"] for c in pays if c.get("name")}
        tags = get_all("/entitytag/")
        self.tag_pk_par_nom = {t["name"].strip().lower(): t["pk"] for t in tags if t.get("name")}
        # Vocabulaire proposé à Claude : tous les tags sauf les internes.
        self.tags_assignables = sorted(
            t["name"] for t in tags if t.get("name") and t["name"].strip().lower() not in TAGS_INTERNES
        )

    def code_dept_depuis_zip(self, zipcode):
        z = (zipcode or "").strip()
        if len(z) < 2 or not z[:2].isdigit():
            return None
        if z.startswith("97") and len(z) >= 3:
            return z[:3]
        return z[:2]

    def region_depuis_zip(self, zipcode):
        code = self.code_dept_depuis_zip(zipcode)
        if not code:
            return None
        region_nom = CODE_DEPT_VERS_REGION.get(code)
        return self.region_pk_par_nom.get(region_nom) if region_nom else None

    def tag_pk(self, nom):
        """pk d'un tag à partir de son nom, seulement s'il est préexistant ET
        non interne. None sinon (jamais d'invention/de tag interne)."""
        cle = (nom or "").strip().lower()
        if cle in TAGS_INTERNES:
            return None
        return self.tag_pk_par_nom.get(cle)


# ── Claude (recherche web + sortie structurée) ───────────────────────────────

def web_search_tool(modele=None):
    modele = modele or MODEL
    version = "web_search_20260209" if modele in MODELES_WEBSEARCH_DYNAMIQUE else "web_search_20250305"
    # Plafond bas : la recherche web pilote le coût (chaque recherche + ses résultats
    # gonflent les tokens). 3 suffit pour les coordonnées d'un lieu ; configurable.
    max_uses = int(os.environ.get("WEB_SEARCH_MAX_USES", "3"))
    return {"type": version, "name": "web_search", "max_uses": max_uses}


# Modèle vision pour le remplissage à l'écran (extension phase 3). Opus 4.8 = vision + web.
VISION_MODEL = os.environ.get("VISION_MODEL", "claude-opus-4-8")

# Schéma de sortie du remplissage visuel : pour chaque champ de l'écran, une valeur.
SCHEMA_VISUEL = {
    "type": "object",
    "additionalProperties": False,
    "required": ["champs"],
    "properties": {
        "champs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field_id", "valeur", "source", "confiance"],
                "properties": {
                    "field_id": {"type": "string"},
                    "valeur": {"type": ["string", "null"]},
                    "source": {"type": "string"},
                    "confiance": CONFIANCE,
                },
            },
        }
    },
}


def enrichir_visuel_via_claude(client, struct, champs, screenshot_b64, deja, commande):
    """Vision : à partir d'un screenshot de la fiche + de la carte des champs du
    formulaire (découverts en direct par l'extension), renvoie une valeur par
    field_id à remplir À L'ÉCRAN. Ne remplit jamais un champ déjà géré par l'API
    (`deja`). Recherche web autorisée. Règle anti-invention conservée."""
    nom = struct.get("name") or "(nom inconnu)"
    ville = struct.get("city") or "(ville inconnue)"

    lignes = []
    for c in champs:
        ligne = f"- id={c.get('id')} | libellé: {c.get('label') or '(sans libellé)'} | type: {c.get('type')}"
        if c.get("value"):
            ligne += f" | valeur actuelle: {c.get('value')!r}"
        opts = c.get("options")
        if opts:
            ligne += f" | options: {', '.join(opts[:30])}"
        lignes.append(ligne)
    carte = "\n".join(lignes) or "(aucun champ détecté)"

    deja_txt = ", ".join(deja) if deja else "(aucun)"
    bloc_cmd = f'\nDemande de l\'utilisateur : "{commande.strip()}".\n' if (commande or "").strip() else ""

    prompt = (
        "Tu aides une agence de booking à compléter la fiche d'un lieu (salle, festival, "
        "centre culturel) dans son CRM. Voici une capture d'écran du formulaire et la "
        "liste des champs détectés sur la page.\n\n"
        f"Lieu : {nom} — {ville}\n"
        f"{bloc_cmd}\n"
        "Champs du formulaire (remplis-les en renvoyant leur field_id exact) :\n"
        f"{carte}\n\n"
        f"NE REMPLIS PAS ces champs déjà gérés automatiquement par l'API : {deja_txt}.\n"
        "RÈGLE ABSOLUE : ne jamais inventer. Si une valeur est introuvable ou incertaine, "
        "mets valeur=null et confiance=\"non_trouve\". Ne propose une valeur que pour un "
        "field_id présent dans la liste ci-dessus. Pour un champ à options (select), choisis "
        "une valeur exactement parmi les options listées. Cite la source (URL) de chaque valeur. "
        "Recherche sur le web ce qui manque (jauge, type de lieu, style, site, etc.)."
    )

    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64}}
    contenu = [image, {"type": "text", "text": prompt}]
    messages = [{"role": "user", "content": contenu}]
    tools = [web_search_tool(VISION_MODEL)]

    resp = None
    for _ in range(6):
        resp = client.messages.create(
            model=VISION_MODEL,
            max_tokens=4000,
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA_VISUEL}},
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    if resp:
        cout_appel(VISION_MODEL, resp, "vision")
    texte = next((b.text for b in resp.content if b.type == "text"), None) if resp else None
    if not texte:
        return []
    try:
        data = json.loads(texte)
    except json.JSONDecodeError:
        return []
    ids_valides = {c.get("id") for c in champs}
    sortie = []
    for c in (data.get("champs") or []):
        fid = c.get("field_id")
        val = propre(c.get("valeur"))
        if fid in ids_valides and not vide(val) and c.get("confiance") != "non_trouve":
            sortie.append({"field_id": fid, "valeur": val,
                           "source": c.get("source") or "", "confiance": c.get("confiance")})
    return sortie


def enrichir_via_claude(client, struct, refs, commande=None):
    nom = struct.get("name") or "(nom inconnu)"
    ville = struct.get("city") or "(ville inconnue)"
    adresse = struct.get("address1") or "(adresse inconnue)"
    vocab = ", ".join(refs.tags_assignables)

    # Consigne libre de l'utilisateur (extension Chrome) : oriente les champs à remplir
    # sans jamais lever la règle anti-invention ni le cadre du schéma.
    bloc_commande = ""
    if commande and commande.strip():
        bloc_commande = (
            f"\nL'utilisateur demande spécifiquement : \"{commande.strip()}\". "
            "Priorise ces champs dans ta recherche, mais respecte toujours le schéma "
            "et la règle anti-invention ci-dessous.\n"
        )

    prompt = (
        "Tu es un assistant de recherche pour une agence de booking de spectacles "
        "vivants. Recherche sur le web les coordonnées professionnelles de ce lieu "
        "(salle de concert, festival, centre culturel) :\n\n"
        f"- Nom : {nom}\n"
        f"- Ville : {ville}\n"
        f"- Adresse connue : {adresse}\n"
        f"{bloc_commande}\n"
        "RÈGLE ABSOLUE : ne jamais inventer. Si une information est introuvable ou "
        "incertaine, mets sa valeur à null et la confiance à \"non_trouve\". "
        "Pour l'adresse : rue (address1), code postal (zipcode), ville seule sans "
        "le pays (city), pays (pays). "
        "Pour \"resume\" : 1 à 2 phrases décrivant ce qu'est le lieu et ce qu'il fait "
        "(type, jauge si connue, type de programmation). "
        "Pour \"tags\" : choisis UNIQUEMENT dans cette liste les tags qui décrivent "
        "la NATURE du lieu (type d'établissement et styles musicaux programmés). "
        "N'invente aucun tag, n'en mets aucun hors de cette liste, et reste sobre "
        "(2 à 5 tags pertinents). Liste autorisée : "
        f"{vocab}.\n"
        "Privilégie les coordonnées professionnelles (cadre B2B / RGPD). "
        "Cite la source (URL) de chaque bloc."
    )

    messages = [{"role": "user", "content": prompt}]
    tools = [web_search_tool()]

    resp = None
    for _ in range(6):  # outils serveur : relancer sur pause_turn
        resp = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            tools=tools,
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        break

    if resp:
        cout_appel(MODEL, resp, "api")
    texte = next((b.text for b in resp.content if b.type == "text"), None) if resp else None
    if not texte:
        return None
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        return None


# ── Logique des deux bacs ────────────────────────────────────────────────────

def champs_fiables_a_ecrire(struct, enrichi, refs):
    """Champs structure à PATCHer (hors tags). Lieux FRANÇAIS uniquement."""
    adr = enrichi.get("adresse", {})
    if (adr.get("pays") or "").strip().lower() != "france":
        return {}

    a_ecrire = {}
    if adr.get("confiance") == "haute":
        for champ in ("address1", "zipcode"):
            valeur = propre(adr.get(champ))
            if vide(struct.get(champ)) and not vide(valeur):
                a_ecrire[champ] = valeur
        zip_ref = a_ecrire.get("zipcode") or struct.get("zipcode")
        region_pk = refs.region_depuis_zip(zip_ref)
        if region_pk and vide(struct.get("region")):
            a_ecrire["region"] = region_pk

    resume = enrichi.get("resume", {})
    texte = propre(resume.get("texte"))
    if vide(struct.get("notes")) and not vide(texte) and resume.get("confiance") in ("haute", "moyenne"):
        a_ecrire["notes"] = texte

    return a_ecrire


def est_francais(enrichi):
    return (enrichi.get("adresse", {}).get("pays") or "").strip().lower() == "france"


def tags_a_ajouter(struct, enrichi, refs):
    """pks de tags préexistants à ajouter (hors internes, hors déjà posés).
    Réservé aux lieux français pour rester cohérent avec l'écriture auto."""
    if not est_francais(enrichi):
        return []
    deja = {t.get("pk") for t in (struct.get("tags") or [])}
    pks = []
    for nom in enrichi.get("tags", []) or []:
        pk = refs.tag_pk(nom)
        if pk and pk not in deja and pk not in pks:
            pks.append(pk)
    return pks


def contacts_a_creer(struct, enrichi, existants):
    """[(type, value)] téléphone + mail générique à créer (confiance haute,
    non déjà présents). Lieux français uniquement."""
    if not est_francais(enrichi):
        return []
    c = enrichi.get("contact", {})
    if c.get("confiance") != "haute":
        return []
    norm = lambda v: (v or "").replace(" ", "").lower()
    deja = {(x.get("type"), norm(x.get("value"))) for x in existants}
    sortie = []
    for typ, champ in (("phone", "telephone"), ("mail", "email_generique")):
        val = propre(c.get(champ))
        if vide(val) or (typ, norm(val)) in deja:
            continue
        # La recherche web masque parfois les mails ("[email protected]") : ne jamais
        # écrire un mail masqué/invalide. Orfeo le refuse (HTTP 500) de toute façon.
        if typ == "mail" and ("email protected" in norm(val) or "@" not in (val or "")):
            continue
        sortie.append((typ, val))
    return sortie


def lignes_a_valider(struct, enrichi, ecrit_auto):
    """Bac à valider : site web + programmateur (toujours), et — si le lieu n'est
    pas auto-enrichi (étranger) — l'adresse et les contacts non écrits."""
    pk = struct.get("pk") or struct.get("id")
    nom = struct.get("name")
    lignes = []

    def ajoute(bloc, champ, valeur, b):
        if not vide(propre(valeur)):
            lignes.append({
                "structure_pk": pk, "structure_nom": nom, "bloc": bloc, "champ": champ,
                "valeur": propre(valeur), "source": b.get("source") or "",
                "confiance": b.get("confiance") or "",
            })

    contact = enrichi.get("contact", {})
    prog = enrichi.get("programmateur", {})
    ajoute("contact", "site_web", contact.get("site_web"), contact)
    ajoute("programmateur", "nom", prog.get("nom"), prog)
    ajoute("programmateur", "email", prog.get("email"), prog)

    if not ecrit_auto:  # lieu étranger : rien n'a été écrit, tout va en validation
        adr = enrichi.get("adresse", {})
        for champ in ("address1", "zipcode", "city", "pays"):
            ajoute("adresse", champ, adr.get(champ), adr)
        ajoute("contact", "email_generique", contact.get("email_generique"), contact)
        ajoute("contact", "telephone", contact.get("telephone"), contact)
    return lignes


# ── Traitement d'une structure (réutilisé par le CLI et le serveur local) ─────

def appliquer_enrichissement(s, enrichi, refs, apply):
    """À partir d'un résultat Claude `enrichi` déjà obtenu, calcule (et applique si
    apply=True) les écritures Orfeo, et renvoie un résultat JSON-able.
      apply=False → aperçu : `ecrit`/`tags_ajoutes` = ce qui SERAIT écrit, rien n'est patché.
      apply=True  → écriture réelle : `ecrit`/`tags_ajoutes` = ce qui A ÉTÉ écrit (sur succès).
    Sépare la recherche Claude de l'écriture pour que le serveur local puisse
    prévisualiser puis appliquer LE MÊME plan sans relancer de recherche web."""
    pk = s.get("pk") or s.get("id")
    res = {
        "pk": pk, "nom": s.get("name"), "ok": False, "applique": bool(apply),
        "adresse_confiance": None, "resume": None,
        "ecrit": {}, "tags_ajoutes": [], "contacts": [], "a_valider": [], "message": "",
    }

    if not enrichi:
        res["message"] = "Pas de résultat exploitable."
        return res

    adr = enrichi.get("adresse", {})
    res["adresse_confiance"] = adr.get("confiance")
    resume = enrichi.get("resume", {})
    res["resume"] = propre(resume.get("texte")) if resume.get("texte") else None

    fiables = champs_fiables_a_ecrire(s, enrichi, refs)
    tag_pks = tags_a_ajouter(s, enrichi, refs)
    tag_noms = [n for n in (enrichi.get("tags") or []) if refs.tag_pk(n) in tag_pks]
    existants = contacts_existants(pk) if est_francais(enrichi) else []
    contacts = contacts_a_creer(s, enrichi, existants)

    payload = dict(fiables)
    if tag_pks:
        deja = {t.get("pk") for t in (s.get("tags") or [])}
        payload["tags"] = sorted(deja | set(tag_pks))

    if payload:
        if apply:
            r = patch_structure(pk, payload)
            if r.status_code in (200, 201):
                res["ecrit"] = fiables
                res["tags_ajoutes"] = tag_noms
            else:
                res["message"] += f"Échec PATCH HTTP {r.status_code} : {r.text[:160]}. "
            time.sleep(0.12)
        else:
            res["ecrit"] = fiables
            res["tags_ajoutes"] = tag_noms

    for typ, val in contacts:
        statut = "aperçu"
        if apply:
            rc = creer_contact(pk, typ, val)
            statut = "ok" if rc.status_code in (200, 201) else f"HTTP {rc.status_code}"
            time.sleep(0.12)
        res["contacts"].append({"type": typ, "valeur": val, "statut": statut})

    res["a_valider"] = lignes_a_valider(s, enrichi, est_francais(enrichi))
    res["ok"] = True
    return res


def traiter_structure(s, refs, client, apply, commande=None):
    """Recherche Claude (avec consigne libre optionnelle) puis aperçu/écriture.
    `commande` oriente les champs sans jamais lever la règle anti-invention."""
    enrichi = enrichir_via_claude(client, s, refs, commande=commande)
    return appliquer_enrichissement(s, enrichi, refs, apply)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enrichissement des fiches lieux Orfeo")
    parser.add_argument("--limit", type=int, default=10,
                        help="Nombre de lieux incomplets à traiter (défaut : 10)")
    parser.add_argument("--skip", type=int, default=0,
                        help="Sauter les N premiers lieux incomplets")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit réellement dans Orfeo (sinon aperçu seul)")
    parser.add_argument("--list-only", action="store_true",
                        help="Liste seulement les lieux incomplets, sans appeler Claude (gratuit)")
    parser.add_argument("--pks", type=str, default="",
                        help="Cible des structures précises par pk (liste séparée par des virgules). "
                             "Ignore --limit/--skip et la détection automatique.")
    args = parser.parse_args()

    if not TOKEN:
        print("ERREUR : ORFEO_TOKEN non défini.")
        sys.exit(1)

    if args.pks:
        pks = [p.strip() for p in args.pks.split(",") if p.strip()]
        print(f"Ciblage explicite de {len(pks)} structure(s) par pk…")
        candidats = structures_par_pks(pks)
    else:
        print(f"Recherche des lieux incomplets (max {args.limit}, après {args.skip} sautés)…")
        candidats = structures_incompletes(args.limit, args.skip)
    if not candidats:
        print("Aucun lieu incomplet trouvé. Rien à faire.")
        return
    print(f"{len(candidats)} lieu(x) à traiter.\n")

    if args.list_only:
        for s in candidats:
            manquants = [c for c in ("address1", "zipcode", "region") if vide(s.get(c))]
            print(f"  • {s.get('name')} ({s.get('city')}) — pk={s.get('pk') or s.get('id')} "
                  f"— manque : {', '.join(manquants)}")
        print("\n(--list-only : aucun appel Claude, aucune écriture.)")
        return

    if not os.environ.get("ANTHROPIC_API_KEY", ""):
        print("ERREUR : ANTHROPIC_API_KEY non défini (requis hors --list-only).")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic()

    print("Chargement des référentiels Orfeo (départements, régions, tags)…")
    refs = Referentiels()

    mode = "ÉCRITURE RÉELLE (--apply)" if args.apply else "APERÇU (aucune écriture)"
    print(f"Mode : {mode} | Modèle : {MODEL}\n")

    toutes_lignes_valider = []
    lignes_appliquees = []

    for s in candidats:
        pk = s.get("pk") or s.get("id")
        nom = s.get("name")
        print(f"→ {nom} ({s.get('city')}) [pk={pk}]")

        res = traiter_structure(s, refs, client, args.apply)
        if not res["ok"]:
            print(f"    ⚠  {res['message'] or 'Pas de résultat exploitable.'}")
            continue

        print(f"    adresse (confiance {res['adresse_confiance']})")
        if res["resume"]:
            print(f"    résumé: {res['resume'][:90]}…")

        if res["ecrit"] or res["tags_ajoutes"]:
            apercu = ", ".join(f"{k}={v!r}" for k, v in res["ecrit"].items())
            if res["tags_ajoutes"]:
                apercu += (", " if apercu else "") + f"tags+={res['tags_ajoutes']}"
            etiquette = "✓ Structure écrite" if args.apply else "[aperçu] structure"
            print(f"    {etiquette} : {apercu}")
            if args.apply:
                lignes_appliquees.append({"structure_pk": pk, "structure_nom": nom,
                                          **res["ecrit"], "tags_ajoutes": ";".join(res["tags_ajoutes"])})
        else:
            print("    (rien à écrire sur la structure)")
        if res["message"]:
            print(f"    ✗ {res['message']}")

        for c in res["contacts"]:
            if args.apply:
                ok = c["statut"] == "ok"
                print(f"    {'✓' if ok else '✗'} contact {c['type']}: {c['valeur']}"
                      + ("" if ok else f" ({c['statut']})"))
            else:
                print(f"    [aperçu] contact {c['type']}: {c['valeur']}")

        if res["a_valider"]:
            print(f"    {len(res['a_valider'])} info(s) à valider (CSV)")
            toutes_lignes_valider.extend(res["a_valider"])
        print()

    if toutes_lignes_valider:
        with open(CSV_A_VALIDER, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(toutes_lignes_valider[0].keys()))
            w.writeheader()
            w.writerows(toutes_lignes_valider)
        print(f"→ {len(toutes_lignes_valider)} ligne(s) à valider dans {CSV_A_VALIDER}")

    if lignes_appliquees:
        with open(CSV_APPLIQUE, "w", newline="", encoding="utf-8") as f:
            cles = sorted({k for ligne in lignes_appliquees for k in ligne})
            w = csv.DictWriter(f, fieldnames=cles)
            w.writeheader()
            w.writerows(lignes_appliquees)
        print(f"→ {len(lignes_appliquees)} fiche(s) modifiée(s) loguées dans {CSV_APPLIQUE}")

    print("\nTerminé.")


if __name__ == "__main__":
    main()
