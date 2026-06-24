"""
Setup Orfeo — création des champs personnalisés de scoring manquants.
Idempotent : ne recrée pas ce qui existe déjà.

Note : les statuts du tunnel (Lead, Qualifié, etc.) doivent être créés
manuellement dans l'interface Orfeo — l'API ne supporte pas leur création.

Usage :
    export ORFEO_TOKEN="..."
    python3 setup_orfeo.py [--dry-run]
"""

import os
import sys
import time
import argparse
import requests

BASE_URL = "https://orfeoapp.com/api"
TOKEN = os.environ.get("ORFEO_TOKEN", "")

# Champs personnalisés à créer sur les structures (entity)
# genre_musical, capacite, type_lieu → déjà gérés via les champs natifs Salle ou les tags
CHAMPS_VOULUS = [
    {"key": "score_compatibilite", "label": "Score compatibilité", "field_type": "text"},
    {"key": "score_artiste",       "label": "Score artiste",       "field_type": "text"},
    {"key": "score_date",          "label": "Date du score",       "field_type": "date"},
]


def headers():
    return {"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"}


def get_all(path):
    results = []
    url = f"{BASE_URL}{path}?page_size=100"
    while url:
        r = requests.get(url, headers=headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        results.extend(data.get("results", []))
        url = data.get("next")
        time.sleep(0.12)
    return results


def post(path, payload):
    r = requests.post(f"{BASE_URL}{path}", headers=headers(), json=payload, timeout=15)
    return r


def section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── Champs personnalisés ─────────────────────────────────────────────────────

def setup_champs(dry_run):
    section("Champs personnalisés (custom_field)")

    existants = get_all("/custom_field/")
    keys_existantes = {f.get("key") for f in existants}
    print(f"  Existants : {', '.join(sorted(keys_existantes)) or '(aucun)'}")

    # Détecter le nom interne de l'objet "structure" depuis les champs existants
    obj_type = None
    for f in existants:
        obj_type = f.get("object_type") or f.get("content_type")
        if obj_type:
            break
    if not obj_type:
        obj_type = "entity"  # valeur observée dans le diagnostic
    print(f"  object_type utilisé : {obj_type!r}")

    a_creer = [c for c in CHAMPS_VOULUS if c["key"] not in keys_existantes]
    if not a_creer:
        print("  ✓ Tous les champs sont déjà présents.")
        return

    for champ in a_creer:
        payload = {
            "key":         champ["key"],
            "label":       champ["label"],
            "field_type":  champ["field_type"],
            "object_type": obj_type,
        }
        if dry_run:
            print(f"  [dry-run] POST custom_field  {payload}")
            continue
        r = post("/custom_field/", payload)
        if r.status_code in (200, 201):
            pk = r.json().get("pk") or r.json().get("id")
            print(f"  ✓ Créé : {champ['key']!r}  (pk={pk})")
        else:
            print(f"  ✗ Échec {champ['key']!r}  HTTP {r.status_code} : {r.text[:200]}")
        time.sleep(0.12)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Setup Orfeo — statuts + champs")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche ce qui serait créé sans rien écrire")
    args = parser.parse_args()

    if not TOKEN:
        print("ERREUR : ORFEO_TOKEN non défini.")
        sys.exit(1)

    mode = "DRY-RUN" if args.dry_run else "ÉCRITURE RÉELLE"
    print(f"\n=== Setup Orfeo [{mode}] ===")

    setup_champs(args.dry_run)

    print(f"\n{'─' * 60}")
    print("  Setup terminé.")
    print(f"{'─' * 60}\n")


if __name__ == "__main__":
    main()
