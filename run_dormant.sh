#!/bin/bash

SEMAINE_COURANTE=$(date +"%Y-%W")
FICHIER_SEMAINE="/Users/naeldarwish/automation-orfeo/.derniere_semaine"

# Si le script a déjà tourné cette semaine, on sort sans rien faire
if [ -f "$FICHIER_SEMAINE" ] && [ "$(cat $FICHIER_SEMAINE)" = "$SEMAINE_COURANTE" ]; then
    exit 0
fi

set -a
source /Users/naeldarwish/automation-orfeo/.env
set +a

/usr/bin/python3 /Users/naeldarwish/automation-orfeo/dormant_dates.py

# Enregistre la semaine courante pour ne pas relancer
echo "$SEMAINE_COURANTE" > "$FICHIER_SEMAINE"
