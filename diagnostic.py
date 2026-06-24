"""
Diagnostic Orfeo — vérifie l'auth, liste les statuts et champs personnalisés,
et confirme qu'on peut écrire dans un champ custom via PATCH.

Usage :
    export ORFEO_TOKEN="..."
    python diagnostic.py [--patch-structure-pk <pk>]
"""

import os
import sys
import time
import argparse
import json
import requests

BASE_URL = "https://orfeoapp.com/api"
TOKEN = os.environ.get("ORFEO_TOKEN", "")


def headers():
    return {"Authorization": f"Token {TOKEN}", "Content-Type": "application/json"}


def get(path, params=None):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, headers=headers(), params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def patch(path, data):
    url = f"{BASE_URL}{path}"
    r = requests.patch(url, headers=headers(), json=data, timeout=15)
    return r


def section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")


# ── 1. Vérification de l'auth ────────────────────────────────────────────────

def check_auth():
    section("1. Vérification de l'authentification")
    try:
        data = get("/project/", {"page_size": 1})
        total = data.get("count", "?")
        print(f"  ✓ Auth OK — {total} project(s) trouvé(s) au total.")
        return True
    except requests.HTTPError as e:
        print(f"  ✗ Erreur HTTP {e.response.status_code} : {e.response.text}")
        return False
    except Exception as e:
        print(f"  ✗ Erreur de connexion : {e}")
        return False


# ── 2. Statuts du tunnel ────────────────────────────────────────────────────

def list_statuses():
    section("2. Statuts du tunnel (project_status)")
    try:
        data = get("/project_status/")
        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            print("  (aucun statut trouvé)")
        for s in results:
            print(f"  pk={s.get('pk') or s.get('id')}  name={s.get('name')}")
        return results
    except Exception as e:
        print(f"  ✗ Erreur : {e}")
        return []


# ── 3. Champs personnalisés ─────────────────────────────────────────────────

def list_custom_fields():
    section("3. Champs personnalisés (custom_field)")
    try:
        data = get("/custom_field/")
        results = data if isinstance(data, list) else data.get("results", [])
        if not results:
            print("  (aucun champ personnalisé trouvé)")
        for f in results:
            pk   = f.get("pk") or f.get("id")
            key  = f.get("key") or f.get("slug") or "?"
            name = f.get("name") or f.get("label") or "?"
            kind = f.get("field_type") or f.get("type") or "?"
            obj  = f.get("object_type") or f.get("content_type") or "?"
            print(f"  pk={pk}  key={key!r}  name={name!r}  type={kind}  obj={obj}")
        return results
    except Exception as e:
        print(f"  ✗ Erreur : {e}")
        return []


# ── 4. PATCH de test sur une structure ──────────────────────────────────────

def test_patch(structure_pk):
    section(f"4. PATCH de test sur structure pk={structure_pk}")

    # Lire la valeur actuelle du champ _diagnostic_test s'il existe
    try:
        current = get(f"/structure/{structure_pk}/")
    except Exception as e:
        print(f"  ✗ Impossible de lire la structure : {e}")
        return

    # Chercher un champ custom existant sur lequel tester
    custom_fields = current.get("custom_fields", {})
    if not custom_fields:
        print("  ⚠  Aucun custom_field présent sur cette structure.")
        print("     Le PATCH sera quand même tenté avec un champ fictif pour")
        print("     observer la réponse de l'API.")

    payload = {"custom_fields": {"_diagnostic_test": "ok"}}
    print(f"  → PATCH {BASE_URL}/structure/{structure_pk}/")
    print(f"    payload : {json.dumps(payload)}")

    r = patch(f"/structure/{structure_pk}/", payload)
    print(f"  ← HTTP {r.status_code}")
    try:
        print(f"    réponse : {json.dumps(r.json(), ensure_ascii=False, indent=2)[:600]}")
    except Exception:
        print(f"    réponse (brute) : {r.text[:400]}")

    if r.status_code in (200, 201):
        print("  ✓ PATCH accepté — écriture des custom_fields confirmée.")
    else:
        print("  ✗ PATCH refusé ou erreur — voir la réponse ci-dessus.")


# ── 5. Première structure disponible (pour le PATCH auto) ───────────────────

def get_first_structure_pk():
    try:
        data = get("/structure/", {"page_size": 1})
        results = data if isinstance(data, list) else data.get("results", [])
        if results:
            return results[0].get("pk") or results[0].get("id")
    except Exception:
        pass
    return None


# ── Entrée principale ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Diagnostic Orfeo API")
    parser.add_argument(
        "--patch-structure-pk",
        help="pk de la structure sur laquelle tester le PATCH (auto si absent)",
    )
    parser.add_argument(
        "--skip-patch",
        action="store_true",
        help="Ne pas faire le PATCH de test",
    )
    args = parser.parse_args()

    if not TOKEN:
        print("ERREUR : variable d'environnement ORFEO_TOKEN non définie.")
        sys.exit(1)

    print("\n=== Diagnostic Orfeo ===")

    ok = check_auth()
    if not ok:
        sys.exit(1)

    time.sleep(0.15)
    list_statuses()

    time.sleep(0.15)
    list_custom_fields()

    if not args.skip_patch:
        time.sleep(0.15)
        pk = args.patch_structure_pk or get_first_structure_pk()
        if pk:
            test_patch(pk)
        else:
            print("\n  ⚠  Aucune structure trouvée pour le PATCH de test. Passez --skip-patch ou --patch-structure-pk <pk>.")

    print(f"\n{'─' * 60}")
    print("  Diagnostic terminé.")
    print(f"{'─' * 60}\n")


if __name__ == "__main__":
    main()
