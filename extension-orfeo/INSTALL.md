# Extension Chrome — Enrichir la fiche Orfeo

Enrichis la structure Orfeo que tu **es en train de regarder** : tu cliques sur
l'extension, tu tapes une commande en français ("complète l'adresse et le style"),
Claude cherche sur le web, tu vois un **aperçu**, puis tu cliques **Écrire dans Orfeo**.

## Comment ça marche (en bref)

```
Popup Chrome (commande)  ──▶  serveur local 127.0.0.1:8723  ──▶  enrichir_structures.py
   lit le pk de l'onglet        (tes clés restent dans .env)        ──▶ Claude (web) + API Orfeo
```

L'extension ne contient **aucune clé** : elle parle seulement au petit serveur Python
qui tourne sur ton Mac. Tes clés (`ORFEO_TOKEN`, `ANTHROPIC_API_KEY`) restent dans `.env`.

## 1. Lancer le serveur local

Dans un terminal :

```bash
cd /Users/naeldarwish/automation-orfeo
python3 serveur_enrichissement.py
```

Tu dois voir `Serveur d'enrichissement Orfeo → http://127.0.0.1:8723`.
Laisse ce terminal ouvert (ou installe le démarrage auto, étape 4).

## 2. Installer l'extension (une seule fois)

1. Ouvre `chrome://extensions`.
2. Active **Mode développeur** (en haut à droite).
3. **Charger l'extension non empaquetée** → choisis le dossier
   `/Users/naeldarwish/automation-orfeo/extension-orfeo`.
4. (Option) épingle l'icône pour l'avoir sous la main.

## 3. Utiliser

1. Ouvre une **fiche structure** dans Orfeo (orfeoapp.com).
2. Clique l'icône de l'extension. Elle affiche « Fiche détectée : … pk … ».
3. Tape ta commande, **Entrée** → aperçu (rien n'est écrit).
   - Vert = sera écrit dans la fiche (adresse, région, notes, tags, contacts).
   - Rouge « À valider » = trouvé mais **jamais** écrit auto (site web, contact
     programmateur) — à recopier à la main après vérification.
4. Si l'aperçu te convient → **Écrire dans Orfeo**. Recharge la fiche pour voir.

> Le pk n'est pas détecté ? Dis-moi le format d'URL d'une fiche Orfeo
> (ex. `orfeoapp.com/…/structure/123…`) pour affiner la détection.

## 4. (Option) Démarrage automatique du serveur

Pour ne plus avoir à lancer le serveur à la main :

```bash
cp /Users/naeldarwish/automation-orfeo/com.maisondarwish.orfeo.enrich.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.maisondarwish.orfeo.enrich.plist
```

Le serveur démarrera à chaque ouverture de session. Journal :
`/Users/naeldarwish/automation-orfeo/serveur_enrichissement.log`.

Pour l'arrêter :
```bash
launchctl unload ~/Library/LaunchAgents/com.maisondarwish.orfeo.enrich.plist
```

## Sécurité

- Le serveur n'écoute que sur `127.0.0.1` (ta machine), jamais sur le réseau.
- Règle absolue conservée : **ne jamais inventer**. Données incertaines →
  « À valider », jamais écrites automatiquement. Écriture auto limitée aux lieux
  **français** (cohérent avec le script existant).
