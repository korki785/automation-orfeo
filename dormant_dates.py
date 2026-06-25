"""
Dates dormantes — notifie par email les dates Orfeo sans activité depuis X jours.
Statuts surveillés : Intérêt, Option.

Variables d'environnement requises :
    ORFEO_TOKEN          Token API Orfeo
    GMAIL_USER           Adresse Gmail expéditeur (ex: toi@gmail.com)
    GMAIL_APP_PASSWORD   Mot de passe d'application Gmail (16 caractères)
    EMAIL_TO             Adresse destinataire

Optionnelles :
    SEUIL_JOURS          Jours sans activité avant alerte (défaut : 7)

Usage :
    python3 dormant_dates.py [--dry-run]
"""

import os
import sys
import time
import argparse
import smtplib
import requests
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText

BASE_URL = "https://orfeoapp.com/api"

TOKEN = os.environ.get("ORFEO_TOKEN", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", "")
SEUIL_JOURS = int(os.environ.get("SEUIL_JOURS", "7"))

STATUTS_SURVEILLES = {"Intérêt", "Option"}


def orfeo_headers():
    return {"Authorization": f"Token {TOKEN}"}


def get_all(path, params=None):
    results = []
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
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


def fetch_statuts():
    statuts = get_all("/project_status/")
    return {s["name"]: (s.get("pk") or s.get("id")) for s in statuts}


def fetch_dormant_dates(status_pks):
    # Note : Orfeo n'expose pas de date d'activité fiable — `update_date` est
    # souvent null. Le filtre serveur `update_date__lte` laisse alors tout passer.
    # On filtre donc côté client sur `update_date or creation_date`.
    pk_list = ",".join(str(pk) for pk in status_pks)
    projets = get_all("/project/", {
        "status__in": pk_list,
        "page_size": "100",
    })
    return [p for p in projets if _inactivite_jours(p) >= SEUIL_JOURS]


def _inactivite_jours(project):
    """Jours d'inactivité = jours depuis la dernière activité connue.
    Dernière activité = update_date si présent, sinon creation_date.
    Retourne -1 si aucune date exploitable (jamais considéré comme dormant)."""
    raw = project.get("update_date") or project.get("creation_date")
    if not raw:
        return -1
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return (date.today() - dt.date()).days
    except Exception:
        return -1


def days_since(project):
    jours = _inactivite_jours(project)
    return jours if jours >= 0 else "?"


def build_email(dormantes, statuts_par_pk):
    n = len(dormantes)
    lignes = [
        f"Bonjour,",
        f"",
        f"{n} date(s) sans activité depuis plus de {SEUIL_JOURS} jours :",
        f"",
    ]
    for d in dormantes:
        titre = d.get("title") or (d.get("place") or {}).get("name") or "Lieu inconnu"
        status_field = d.get("status") or d.get("project_status")
        if isinstance(status_field, dict):
            status_name = status_field.get("name", "?")
        else:
            status_name = statuts_par_pk.get(status_field, "?")
        jours = days_since(d)
        pk = d.get("pk") or d.get("id")
        lien = f"https://orfeoapp.com/project/{pk}/" if pk else "—"
        lignes.append(f"• {titre} ({status_name}) — {jours} jours — {lien}")

    lignes += ["", "Bonne prospection,"]
    return "\n".join(lignes)


def send_email(subject, body):
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        smtp.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())


def main():
    parser = argparse.ArgumentParser(description="Notification des dates Orfeo dormantes")
    parser.add_argument("--dry-run", action="store_true",
                        help="Affiche le récap sans envoyer d'email")
    args = parser.parse_args()

    if not TOKEN:
        print("ERREUR : ORFEO_TOKEN non défini.")
        sys.exit(1)
    if not args.dry_run and not all([GMAIL_USER, GMAIL_APP_PASSWORD, EMAIL_TO]):
        print("ERREUR : GMAIL_USER, GMAIL_APP_PASSWORD et EMAIL_TO sont requis.")
        sys.exit(1)

    # 1. Récupérer les statuts
    tous_statuts = fetch_statuts()
    statuts_par_pk = {pk: name for name, pk in tous_statuts.items()}
    pks_surveilles = [pk for name, pk in tous_statuts.items() if name in STATUTS_SURVEILLES]

    manquants = STATUTS_SURVEILLES - set(tous_statuts.keys())
    if manquants:
        print(f"Avertissement : statuts introuvables dans Orfeo : {manquants}")

    if not pks_surveilles:
        print("Aucun statut surveillé trouvé dans Orfeo. Vérifiez 'Intérêt' et 'En négociation'.")
        sys.exit(0)

    # 2. Récupérer les dates dormantes
    dormantes = fetch_dormant_dates(pks_surveilles)

    if not dormantes:
        print(f"Aucune date dormante depuis {SEUIL_JOURS} jours. Rien à signaler.")
        sys.exit(0)

    # 3. Formater et envoyer
    subject = f"[Orfeo] {len(dormantes)} date(s) sans activité depuis +{SEUIL_JOURS} jours"
    body = build_email(dormantes, statuts_par_pk)

    if args.dry_run:
        print(f"Sujet : {subject}")
        print("─" * 60)
        print(body)
    else:
        send_email(subject, body)
        print(f"Email envoyé : {len(dormantes)} date(s) signalée(s).")


if __name__ == "__main__":
    main()
