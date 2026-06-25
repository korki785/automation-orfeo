"""
Enrichissement des fiches lieux (structure) Orfeo — automation #2.

Repère les structures à l'adresse incomplète, cherche les infos manquantes sur
le web via l'API Claude (web search), puis applique une logique en deux bacs :

  • Bac « fiable »   → écriture directe dans Orfeo (PATCH). Reproduit la saisie
                       manuelle observée sur les fiches déjà enrichies :
                         - address1, zipcode  → trouvés par Claude (texte)
                         - department_id, region, country_id → DÉDUITS du code
                           postal via les référentiels Orfeo (aucune invention).
                       Uniquement si le champ est vide ET la confiance « haute ».
  • Bac « à valider » → email générique, téléphone, site web, type de lieu, style
                       musical, contact programmateur. JAMAIS écrits en auto :
                       exportés dans un CSV avec source + niveau de confiance.

Faits Orfeo vérifiés :
  - region / department_id / country_id sont des ID entiers (clés étrangères),
    pas du texte. department_id se déduit des 2 premiers chiffres du code postal
    (le code département), region via la table département→région, country=France=3.
  - Les contacts vivent dans `contact_infos` (sous-objet), pas en champs plats :
    ils restent donc dans le bac « à valider » pour l'instant.

Règle absolue : ne jamais inventer. Info introuvable → null / « non_trouve »,
jamais écrite.

Sécurité : SANS --apply, aucune écriture (mode aperçu). Le CSV est toujours produit.

Variables d'environnement :
    ORFEO_TOKEN        Token API Orfeo (requis)
    ANTHROPIC_API_KEY  Clé API Claude (requise sauf en --list-only)
    ENRICH_MODEL       Modèle Claude (défaut : claude-haiku-4-5, le moins cher)

Usage :
    python3 enrichir_structures.py --list-only            # liste les lieux incomplets (gratuit)
    python3 enrichir_structures.py --limit 3              # aperçu enrichi (aucune écriture)
    python3 enrichir_structures.py --limit 3 --apply      # écrit les champs fiables dans Orfeo
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
# Surchargeable via ENRICH_MODEL (ex : claude-sonnet-4-6, claude-opus-4-8).
MODEL = os.environ.get("ENRICH_MODEL", "claude-haiku-4-5")

# L'outil web search « dynamique » (_20260209) n'existe que sur Opus 4.6+/Sonnet 4.6.
# Pour les autres modèles (dont Haiku 4.5), il faut la variante de base _20250305.
MODELES_WEBSEARCH_DYNAMIQUE = (
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
)

CSV_A_VALIDER = "enrichissement_a_valider.csv"
CSV_APPLIQUE = "enrichissement_applique.csv"

COUNTRY_FRANCE_PK = 3  # /country/ code=FR

# Table code département (FR) → nom de région, telle qu'orthographiée dans Orfeo.
# Utilisée pour déduire region/department à partir du code postal, sans invention.
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

# Schéma de sortie imposé à Claude. region/department/country sont DÉDUITS côté
# script à partir du code postal : Claude ne renvoie que de l'adresse texte.
CONFIANCE = {"type": "string", "enum": ["haute", "moyenne", "basse", "non_trouve"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["adresse", "contact", "profil", "programmateur", "resume"],
    "properties": {
        "resume": {
            "type": "object",
            "additionalProperties": False,
            "required": ["texte", "source", "confiance"],
            "properties": {
                # Récap court (1-2 phrases) : ce qu'est le lieu et ce qu'il fait.
                "texte": {"type": ["string", "null"]},
                "source": {"type": "string"},
                "confiance": CONFIANCE,
            },
        },
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
        "profil": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type_lieu", "style_musical", "source", "confiance"],
            "properties": {
                "type_lieu": {"type": ["string", "null"]},
                "style_musical": {"type": ["string", "null"]},
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
    },
}


# ── Orfeo ────────────────────────────────────────────────────────────────────

def orfeo_headers():
    return {"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"}


def vide(valeur):
    return valeur in (None, "", [], {})


def propre(valeur):
    """Nettoie les valeurs texte renvoyées par le modèle (espaces parasites)."""
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
    """Retourne `limite` structures dont au moins un champ d'adresse
    (address1/zipcode/region) est vide, après en avoir sauté `skip`."""
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


def patch_structure(pk, payload):
    return requests.patch(f"{BASE_URL}/structure/{pk}/", headers=orfeo_headers(),
                          json=payload, timeout=15)


# ── Référentiels (chargés une fois) ──────────────────────────────────────────

class Referentiels:
    def __init__(self):
        deps = get_all("/department/")
        self.dept_pk_par_code = {d["code"]: d["pk"] for d in deps if d.get("code")}
        regs = get_all("/region/")
        self.region_pk_par_nom = {r["name"]: r["pk"] for r in regs if r.get("name")}
        pays = get_all("/country/")
        # Pays indexés par nom (français) en minuscules, pour résoudre les lieux étrangers.
        self.country_pk_par_nom = {c["name"].lower(): c["pk"] for c in pays if c.get("name")}

    def code_dept_depuis_zip(self, zipcode):
        """Code département (FR) à partir d'un code postal. DROM = 3 chiffres."""
        z = (zipcode or "").strip()
        if len(z) < 2 or not z[:2].isdigit():
            return None
        if z.startswith("97") and len(z) >= 3:
            return z[:3]
        return z[:2]

    def resoudre_geo(self, zipcode):
        """Déduit (department_id, region_id) du code postal. None si non résolu."""
        code = self.code_dept_depuis_zip(zipcode)
        if not code:
            return None, None
        dept_pk = self.dept_pk_par_code.get(code)
        region_nom = CODE_DEPT_VERS_REGION.get(code)
        region_pk = self.region_pk_par_nom.get(region_nom) if region_nom else None
        return dept_pk, region_pk


# ── Claude (recherche web + sortie structurée) ───────────────────────────────

def web_search_tool():
    version = "web_search_20260209" if MODEL in MODELES_WEBSEARCH_DYNAMIQUE else "web_search_20250305"
    return {"type": version, "name": "web_search", "max_uses": 5}


def enrichir_via_claude(client, struct):
    nom = struct.get("name") or "(nom inconnu)"
    ville = struct.get("city") or "(ville inconnue)"
    adresse = struct.get("address1") or "(adresse inconnue)"

    prompt = (
        "Tu es un assistant de recherche pour une agence de booking de spectacles "
        "vivants. Recherche sur le web les coordonnées professionnelles de ce lieu "
        "(salle de concert, festival, centre culturel) :\n\n"
        f"- Nom : {nom}\n"
        f"- Ville : {ville}\n"
        f"- Adresse connue : {adresse}\n\n"
        "RÈGLE ABSOLUE : ne jamais inventer. Si une information est introuvable ou "
        "incertaine, mets sa valeur à null et la confiance correspondante à "
        "\"non_trouve\". Un champ vide honnête vaut mieux qu'une donnée inventée. "
        "Pour l'adresse : donne la rue (address1), le code postal (zipcode), la "
        "ville seule sans le pays (city) et le pays (pays). "
        "Pour \"resume\" : 1 à 2 phrases en français décrivant ce qu'est le lieu et "
        "ce qu'il fait (type de structure, jauge si connue, type de programmation). "
        "Privilégie les coordonnées professionnelles (cadre B2B / RGPD). "
        "Pour chaque bloc, cite la source (URL) dans \"source\" et donne un niveau "
        "de confiance honnête."
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

    texte = next((b.text for b in resp.content if b.type == "text"), None) if resp else None
    if not texte:
        return None
    try:
        return json.loads(texte)
    except json.JSONDecodeError:
        return None


# ── Logique des deux bacs ────────────────────────────────────────────────────

def champs_fiables_a_ecrire(struct, enrichi, refs):
    """Bac fiable : champs d'adresse vides dans Orfeo, trouvés en confiance « haute ».
    address1/zipcode viennent de Claude ; department/region/country sont déduits."""
    adr = enrichi.get("adresse", {})
    pays = (adr.get("pays") or "").strip().lower()
    est_france = pays == "france"

    # SÉCURITÉ : écriture automatique réservée aux lieux FRANÇAIS. Sur un lieu
    # étranger, écrire l'adresse fait recalculer côté serveur les champs géo
    # dérivés (country_id/department_id, read-only) et peut les EFFACER
    # (régression observée : un lieu autrichien a perdu son country_id).
    # Les lieux étrangers passent donc uniquement par le bac « à valider » (CSV).
    if not est_france:
        return {}

    a_ecrire = {}
    # 1. Adresse : seulement si trouvée en confiance « haute ».
    if adr.get("confiance") == "haute":
        for champ in ("address1", "zipcode"):
            valeur = propre(adr.get(champ))
            if vide(struct.get(champ)) and not vide(valeur):
                a_ecrire[champ] = valeur

        # 2. region : DÉDUITE du code postal. Seul champ géo écrivable
        #    (department_id / country_id sont read-only côté Orfeo).
        zip_ref = a_ecrire.get("zipcode") or struct.get("zipcode")
        _, region_pk = refs.resoudre_geo(zip_ref)
        if region_pk and vide(struct.get("region")):
            a_ecrire["region"] = region_pk

    # 3. Notes : récap de contexte. Écrit UNIQUEMENT si les notes sont vides —
    #    on ne touche jamais à une note de booking existante.
    resume = enrichi.get("resume", {})
    texte = propre(resume.get("texte"))
    if vide(struct.get("notes")) and not vide(texte) and resume.get("confiance") in ("haute", "moyenne"):
        a_ecrire["notes"] = texte

    return a_ecrire


def lignes_a_valider(struct, enrichi):
    """Bac à valider : email/téléphone/site/type/style/programmateur, avec source
    et confiance. Jamais écrit en auto (contacts = sous-objet contact_infos)."""
    pk = struct.get("pk") or struct.get("id")
    nom = struct.get("name")
    lignes = []
    for bloc, champs in (
        ("contact", ["email_generique", "telephone", "site_web"]),
        ("profil", ["type_lieu", "style_musical"]),
        ("programmateur", ["nom", "email"]),
    ):
        b = enrichi.get(bloc, {})
        for champ in champs:
            valeur = propre(b.get(champ))
            if not vide(valeur):
                lignes.append({
                    "structure_pk": pk,
                    "structure_nom": nom,
                    "bloc": bloc,
                    "champ": champ,
                    "valeur": valeur,
                    "source": b.get("source") or "",
                    "confiance": b.get("confiance") or "",
                })
    return lignes


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Enrichissement des fiches lieux Orfeo")
    parser.add_argument("--limit", type=int, default=10,
                        help="Nombre de lieux incomplets à traiter (défaut : 10)")
    parser.add_argument("--skip", type=int, default=0,
                        help="Sauter les N premiers lieux incomplets (pour cibler d'autres exemples)")
    parser.add_argument("--apply", action="store_true",
                        help="Écrit réellement les champs fiables dans Orfeo (sinon aperçu seul)")
    parser.add_argument("--list-only", action="store_true",
                        help="Liste seulement les lieux incomplets, sans appeler Claude (gratuit)")
    args = parser.parse_args()

    if not TOKEN:
        print("ERREUR : ORFEO_TOKEN non défini.")
        sys.exit(1)

    print(f"Recherche des lieux incomplets (max {args.limit}, après {args.skip} sautés)…")
    candidats = structures_incompletes(args.limit, args.skip)
    if not candidats:
        print("Aucun lieu incomplet trouvé. Rien à faire.")
        return
    print(f"{len(candidats)} lieu(x) à traiter.\n")

    if args.list_only:
        for s in candidats:
            manquants = [c for c in ("address1", "zipcode", "region") if vide(s.get(c))]
            pk = s.get("pk") or s.get("id")
            print(f"  • {s.get('name')} ({s.get('city')}) — pk={pk} — manque : {', '.join(manquants)}")
        print("\n(--list-only : aucun appel Claude, aucune écriture.)")
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERREUR : ANTHROPIC_API_KEY non défini (requis hors --list-only).")
        print("Ajoute-le dans le .env, puis relance. Ou utilise --list-only pour un aperçu gratuit.")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic()

    print("Chargement des référentiels Orfeo (départements, régions)…")
    refs = Referentiels()

    mode = "ÉCRITURE RÉELLE (--apply)" if args.apply else "APERÇU (aucune écriture)"
    print(f"Mode : {mode} | Modèle : {MODEL}\n")

    toutes_lignes_valider = []
    lignes_appliquees = []

    for s in candidats:
        pk = s.get("pk") or s.get("id")
        nom = s.get("name")
        print(f"→ {nom} ({s.get('city')}) [pk={pk}]")
        enrichi = enrichir_via_claude(client, s)
        if not enrichi:
            print("    ⚠  Pas de résultat exploitable.")
            continue

        adr = enrichi.get("adresse", {})
        print(f"    adresse trouvée (confiance {adr.get('confiance')}): "
              f"{adr.get('address1')!r}, {adr.get('zipcode')!r}, {adr.get('city')!r}")
        resume = enrichi.get("resume", {})
        if resume.get("texte"):
            print(f"    résumé (confiance {resume.get('confiance')}): {resume.get('texte')}")

        fiables = champs_fiables_a_ecrire(s, enrichi, refs)
        if fiables:
            apercu = ", ".join(f"{k}={v!r}" for k, v in fiables.items())
            if args.apply:
                r = patch_structure(pk, fiables)
                if r.status_code in (200, 201):
                    print(f"    ✓ Écrit : {apercu}")
                    lignes_appliquees.append({"structure_pk": pk, "structure_nom": nom, **fiables})
                else:
                    print(f"    ✗ Échec PATCH HTTP {r.status_code} : {r.text[:160]}")
                time.sleep(0.12)
            else:
                print(f"    [aperçu] serait écrit : {apercu}")
        else:
            print("    (aucun champ fiable à écrire — confiance insuffisante ou déjà rempli)")

        a_valider = lignes_a_valider(s, enrichi)
        if a_valider:
            print(f"    {len(a_valider)} info(s) à valider :")
            for l in a_valider:
                print(f"        - {l['champ']}: {l['valeur']!r} (confiance {l['confiance']})")
            toutes_lignes_valider.extend(a_valider)
        print()

    if toutes_lignes_valider:
        with open(CSV_A_VALIDER, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(toutes_lignes_valider[0].keys()))
            w.writeheader()
            w.writerows(toutes_lignes_valider)
        print(f"→ {len(toutes_lignes_valider)} ligne(s) à valider écrites dans {CSV_A_VALIDER}")

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
