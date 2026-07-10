#!/bin/bash
# watchdog_enrichissement.sh
# Vérifie que le serveur d'enrichissement Orfeo répond ; sinon force un
# redémarrage propre du LaunchAgent com.maisondarwish.orfeo.enrich.
#
# Couvre les deux cas que KeepAlive ne gère pas seul :
#   - le process est FIGÉ (vivant mais ne répond plus à /health) ;
#   - une fenêtre de crash-loop où launchd throttle la relance.
#
# Lancé par com.maisondarwish.orfeo.enrich-watchdog.plist (StartInterval 120 s).

PORT="${ENRICH_PORT:-8723}"
URL="http://127.0.0.1:${PORT}/health"
AGENT="com.maisondarwish.orfeo.enrich"
LOG="/Users/naeldarwish/automation-orfeo/watchdog_enrichissement.log"
UID_NUM="$(id -u)"

horodatage() { date "+%Y-%m-%d %H:%M:%S"; }

# 2 tentatives (le serveur peut être en train de démarrer) avant de conclure au KO.
repond=0
for _ in 1 2; do
    corps="$(curl -s -m 5 "$URL" 2>/dev/null)"
    if printf '%s' "$corps" | grep -q '"ok":[[:space:]]*true'; then
        repond=1
        break
    fi
    sleep 3
done

if [ "$repond" -eq 1 ]; then
    # Tout va bien : on reste silencieux (pas de spam dans le log).
    exit 0
fi

echo "[$(horodatage)] /health KO -> kickstart $AGENT" >> "$LOG"
launchctl kickstart -k "gui/${UID_NUM}/${AGENT}" >> "$LOG" 2>&1

# Vérifie que la relance a pris.
sleep 4
corps="$(curl -s -m 5 "$URL" 2>/dev/null)"
if printf '%s' "$corps" | grep -q '"ok":[[:space:]]*true'; then
    echo "[$(horodatage)] relance OK, /health repond" >> "$LOG"
else
    echo "[$(horodatage)] ECHEC relance, /health toujours KO (corps: ${corps:-<vide>})" >> "$LOG"
fi
