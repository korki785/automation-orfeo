# Maison Darwish — Automatisations Orfeo

Automatisation du suivi commercial et de l'enrichissement de données pour
Maison Darwish (agence de booking / production de spectacles vivants), basée
sur l'API d'Orfeo et des scripts Python planifiés.

---

## 1. Contexte métier

Maison Darwish est une agence de **booking / tourneur**. Activité : vendre des
concerts d'artistes à des lieux (salles, festivals, SMAC, scènes nationales,
centres culturels). 1 à 3 artistes en tournée par an, 100 à 300 lieux potentiels
identifiés par tournée.

Le quotidien : prospecter des lieux, les appeler, négocier des dates. L'objectif
de ce repo est d'**automatiser les tâches administratives et de suivi** pour
libérer du temps de prospection téléphonique et de négociation.

Deux gros postes de perte de temps visés :
1. Renseigner / compléter les fiches dans Orfeo (saisie manuelle).
2. Identifier et prioriser les lieux pertinents pour un artiste (scoring).

---

## 2. Orfeo, source de vérité

**Orfeo** (https://orfeoapp.com) est le CRM métier et la **source de vérité
commerciale**. Tout le suivi reste dans Orfeo ; les scripts l'enrichissent, ne
le remplacent pas.

### Vocabulaire Orfeo (à ne pas confondre)

- **STRUCTURE** = un **LIEU** (salle, festival). Porte des champs personnalisés.
  N'a PAS de statut.
- **DATE / PROJECT** = une **AFFAIRE commerciale** = artiste + lieu + date potentielle.
  C'est elle qui porte le **STATUT** (le tunnel de vente). Appelée "date" dans l'UI,
  "project" dans l'API.
- **SPECTACLE** = le projet artistique d'un artiste (ce qui est vendu).
- **TASK** = une tâche de suivi, rattachable à un objet.

### Tunnel commercial (statuts d'une date)

```
Intérêt → En négociation → Option → Gagnée / Perdue
```

**Règle clé :** une date n'est créée que lorsqu'il y a déjà un intérêt de la salle.
Les stades antérieurs (prospection froide, premier appel) ne génèrent pas de date —
ils restent au niveau de la structure elle-même (une structure sans date = un lead
non encore travaillé).

- `Intérêt` : statut par défaut à la création d'une date.
- `En négociation` : discussions en cours.
- `Option` : date réservée, pas encore confirmée.
- `Gagnée` / `Perdue` : statuts terminaux natifs Orfeo (non modifiables).

---

## 3. API Orfeo — faits techniques vérifiés

```
BASE URL   : https://orfeoapp.com/api/
AUTH       : header  ->  Authorization: Token <TOKEN>
RATE LIMIT : 10 requêtes / seconde max. Au-delà : HTTP 429. Throttler les boucles.
FORMAT     : JSON paginé  -> { count, next, previous, results }. Clé primaire = "pk".
PAGINATION : ?page_size=N  &  ?page=N
```

### Endpoints utiles

```
GET/POST/PATCH  /api/project/         filtres: status, status__in, start_date__gt/lt,
                                      update_date__gt/lt, creation_date__gt/lt,
                                      custom_fields, spectacle
GET             /api/project_status/  les statuts du tunnel (lecture seule via API)
GET/POST/PATCH  /api/structure/       les lieux + champs personnalisés
GET/POST/PATCH  /api/task/            POST requiert: title, due_date, assigned_to,
                                      structure_person ; rattachement via
                                      content_type + object_id
GET/POST        /api/custom_field/    chaque champ a une "key" filtrable
GET/POST        /api/note/
```

Filtre sur champ personnalisé : `?custom_fields=<key>:<valeur>`

### URL de l'appli web (≠ API)

L'API vit sous `/api/…`, mais l'**appli web** (server-rendered, auth par session
navigateur — le token API n'y donne pas accès) utilise d'autres chemins. Pour
pointer un lien cliquable vers une fiche depuis un script, utiliser :

```
Fiche structure :  https://orfeoapp.com/booker/core/structure/<pk>/view/
```

(⚠️ ce n'est PAS `https://orfeoapp.com/structure/<pk>/` — ce chemin renvoie une
page introuvable. Utilisé par le cockpit #7.)

### Limitations découvertes

- **Statuts** : `POST /api/project_status/` retourne HTTP 500 — les statuts doivent
  être créés manuellement dans l'UI Orfeo (Statuts d'opportunités).
- **Champs custom** : `POST /api/custom_field/` exige le champ `label` (pas `name`).
  L'`object_type` pour les structures est `"entity"`.
- **PATCH custom_fields** : on ne peut écrire que dans des champs qui existent déjà.
  Un champ inexistant retourne HTTP 400.

---

## 4. Architecture

```
Orfeo (source de vérité, API REST)
   │  token API, 10 req/s max
   ▼
Scripts Python planifiés (cron)   ◄── construits avec Claude Code
   │
   ├── API Claude (analyse / scoring / structuration)
   ├── Recherche web (enrichissement + programmation passée des salles)
   └── Sortie : écriture Orfeo (champs custom, statuts, tâches) + récap / tableaux
```

Principes :
- **100 % scripts Python planifiés (cron).** PAS de webhook, PAS de serveur permanent.
- **Mode polling** : chaque script se réveille, interroge Orfeo, agit, s'arrête.
- **Idempotence obligatoire** : avant d'agir sur un objet, vérifier que l'action
  n'a pas déjà été faite (ne pas recréer une tâche déjà créée à chaque passage).
- **Token via variable d'environnement** (`ORFEO_TOKEN`), jamais en dur dans le code.
- **Langue du code et des commentaires : français.**

---

## 5. Prérequis (une seule fois)

### 5a. Statuts dans Orfeo

Les statuts de projets ne peuvent pas être créés via l'API (retourne HTTP 500).
Les statuts existants dans l'API sont suffisants pour les automatisations :

| pk | Statut | Rôle |
|---|---|---|
| 22454 | `Intérêt` | Statut par défaut à la création d'une date |
| 22439 | `Option` | Date réservée, pas encore confirmée |
| 22440 | `Confirmé` | Booking confirmé |
| 22441 | `Perdu` | Deal perdu |

Statuts surveillés par `dormant_dates.py` : **Intérêt** et **Option**.

### 5b. Via script (setup_orfeo.py)

Créer les **3 champs personnalisés de scoring** sur les structures :

| Clé | Label | Type |
|---|---|---|
| `score_compatibilite` | Score compatibilité | text |
| `score_artiste` | Score artiste | text |
| `score_date` | Date du score | date |

Les champs `genre_musical`, `capacité` et `type_lieu` sont gérés par les champs
natifs de la fiche Salle dans Orfeo — pas besoin de les recréer en custom fields.

```bash
export ORFEO_TOKEN="..."
python3 setup_orfeo.py
```

### 5c. Token API

Générer le token dans Orfeo (page « API & webhooks ») et le placer dans `ORFEO_TOKEN`.

---

## 6. Feuille de route des automatisations

| # | Automatisation | Complexité | ROI | Statut |
|---|----------------|-----------|-----|--------|
| 1 | Notification quotidienne des dates dormantes | ★☆☆☆☆ | Élevé | ✅ Fait |
| 2 | Enrichissement auto des fiches incomplètes (+ extension Chrome) | ★★★☆☆ | Très élevé | ✅ Fait |
| 3 | Création auto de tâches selon statut | ★★☆☆☆ | Élevé | À faire |
| 4 | Assistant questions en français sur les données Orfeo | ★★★☆☆ | Moyen-élevé | À faire |
| 5 | Scoring de compatibilité artiste / lieu | ★★★★☆ | Très élevé | ✅ Fait |
| 6 | Brouillons de relance | ★★★☆☆ | Moyen | À faire |
| 7 | Cockpit local des tâches prioritaires | ★★☆☆☆ | Élevé | ✅ Fait |

Ordre de construction : **1 → 2 → 3 → 4 → 5 → 6** (valeur rapide d'abord,
gros chantiers une fois l'API maîtrisée). #7 ajouté ensuite (tri par score
impossible en natif Orfeo).

### Détail #1 — Dates dormantes ✅

Chaque matin, lister les dates sans activité depuis X jours (défaut : 7).
Statuts surveillés : `Intérêt` (pk=22454), `Option` (pk=22439).

```
GET /api/project/?status__in=22454,22439&update_date__lte=<J-X>
```

→ récap mail via Gmail SMTP (titre date, statut, jours d'inactivité, lien fiche).
Se déclenche au premier démarrage de session de la semaine (LaunchAgent macOS).
Ne s'envoie qu'une seule fois par semaine même si le Mac redémarre plusieurs fois.

Scripts : `dormant_dates.py` + `run_dormant.sh`

### Détail #2 — Enrichissement des fiches

Repérer les `structure` incomplètes, chercher les infos manquantes sur le web,
écrire dans Orfeo. **Logique en deux bacs :**
- **Bac « fiable » → écriture directe** (PATCH) : nom, adresse, ville/CP, site web,
  téléphone standard, mail générique (contact@…), type de salle, style musical.
- **Bac « à valider » → tableau séparé, pas d'écriture auto** : contacts
  programmateurs (nom + mail direct). Chaque info accompagnée de sa **source** et
  d'un **niveau de confiance**. Validation humaine avant injection.

**Règle absolue : ne jamais inventer un mail ou un nom.** Si non trouvé, écrire
« non trouvé ». Un champ vide honnête vaut mieux qu'une donnée inventée.
Privilégier les coordonnées professionnelles (prospection B2B, cadre RGPD).

### Détail #5 — Scoring de compatibilité ✅

Pour un artiste, scorer les 100-300 lieux et prioriser les appels.
Étapes : récupérer lieux + champs natifs (Orfeo) → récupérer la **programmation
passée** de chaque salle (**source externe : absente d'Orfeo** → recherche web
Claude) → l'API Claude juge, en booker France, si programmer l'artiste ici est
**un bon feat artistique et une date qui a du sens** → score 0-100 + justification.

**Critères (logique booker France, pas rentabilité commerciale)**, par ordre de
poids : (a) **fit artistique** — le lieu programme-t-il l'esthétique de l'artiste,
a-t-il accueilli des artistes comparables, son public suivrait-il ; (b) **budget**
— le lieu a-t-il les moyens du cachet (les SMAC = malus, sauf grosses exceptions ;
festivals/grandes salles/collectivités dotées = ok) ; (c) **échelle** — jauge dans
le bon ordre de grandeur (**trop grand = malus autant que trop petit**) ; (d) **gate**
format amplifié et lieu qui programme vraiment (pas simple location).

**Notes internes d'échange** : les notes de la section « Notes » de la fiche
(objets `/api/note/` : verdicts, relations, contacts programmateurs) sont injectées
dans le prompt comme **un facteur parmi d'autres** (pas dominant ; un « n'ira pas
dessus » fait baisser, une collaboration passée fait légèrement monter). L'absence
de notes n'est jamais pénalisée. La note de scoring auto-générée est exclue (pas
d'auto-référence). Un détecteur de sortie corrompue (glitch Haiku : espaces
intra-mots) relance un essai automatiquement.

**Historique d'e-mails (signal FORT)** : les vrais échanges agence ↔ lieu (offre
de cachet chiffrée, option de date, intérêt explicite, ou au contraire refus /
budget insuffisant) sont le meilleur indicateur d'intérêt et de budget — bien
plus fiable qu'une apparence web. **Ils ne sont PAS dans l'API Orfeo** (l'onglet
« Email d'Orfeo » n'est pas exposé — aucun endpoint `email`/`message`), donc le
script les lit **directement dans Gmail par IMAP** (recherche par domaine pro et/ou
adresse exacte du contact de la fiche ; dossier « Tous les messages » résolu quelle
que soit la langue du compte ; citations et lignes `>` retirées ; 10 derniers
messages, datés et marqués ENVOYÉ/REÇU — assez pour couvrir toute une négociation,
de l'offre de cachet à l'option de date). Ce bloc est injecté dans le prompt et
**pondère fortement** le score (un deal en cours → très haut ; un refus → très bas),
tout en restant conditionné à un fit artistique cohérent. Boîte à scanner :
`SCORING_GMAIL_USER`/`SCORING_GMAIL_APP_PASSWORD` (repli sur `GMAIL_USER`/
`GMAIL_APP_PASSWORD`) — mets `SCORING_EMAILS=0` pour désactiver. Défaillance
(creds absents, IMAP KO, aucun échange) → ignoré silencieusement, jamais pénalisant.
Exemple vérifié : *Welcome in Tziganie* passe de **88** (fit artistique seul) à
**97/100** une fois l'échange lu — offre ferme du festival à 22 k€ HT tout compris
et 2 dates optionnées pour avril 2026.

**Tags de la fiche — vocabulaire d'agence, et tags périmés.** Les tags Orfeo
portent un sens commercial que le modèle ne peut pas deviner (`EAT` n'est pas un
mot anglais : c'est « eu au téléphone »). Ils sont exposés **uniquement dans le
détail** `/structure/{pk}/`, jamais dans la liste. Surtout : ils sont saisis à la
main et **rarement mis à jour** — un `Int Gipsy Kings` survit régulièrement à un
intérêt retombé. `bloc_tags()` donne donc au modèle leur sens **et** la consigne de
les confronter aux e-mails avant d'en faire un signal.

| Tag | Effet sur le score | Pourquoi |
|---|---|---|
| `Int Gipsy Kings`, `Int GK` | Fort — **mais seulement si les mails le confirment** | Le seul vrai signal d'intérêt. Sans mail : « non vérifié », ne monte pas le score, confiance abaissée d'un cran. |
| `Budget 10K`, `Budget 12K` | **Malus lourd** | Budget constaté **sous** le cachet (15-20 k€). Une date qu'on ne peut pas financer ne se fait pas, quel que soit le fit. Prime sur toute estimation web. |
| `Budget 15K` | Neutre | Plancher du cachet : tenable mais serré. |
| `EAT` (eu au téléphone) | **Aucun** | Signifie « je connais le contact », pas « il veut l'artiste ». Ni fit ni budget. |
| `GK fest`, `GK SMAC Tour`, `GK Casino Tour` | **Aucun** | Simples étiquettes de ciblage interne. |
| `Location uniquement` | Quasi rédhibitoire | Le lieu ne programme pas, il loue sa salle. |

**Champ `discussion`** (sortie structurée : `vivante` / `morte` / `aucune` /
`indeterminee`) : état **réel** de la relation, jugé sur les mails et les notes,
**jamais sur le tag**. Il **n'influence pas le score** — il sert à l'**ordre de
rappel** : à note haute, une discussion vivante s'appelle avant une piste froide ;
une discussion vivante sur une fiche mal notée ne remonte pas pour autant. Écrit
dans le CSV et dans la note de la fiche (`Discussion : …`).

Résultat sur un lot de **80 fiches** (les mieux taguées du CRM, donc le haut du
panier théorique) : **53 scorent sous 40** et **30 sont en discussion morte** —
9 discussions vivantes seulement. Cas d'école : *Cartel Concerts*, tagué à la fois
`Int Gipsy Kings` et `Int GK` (donc n°1 du classement par tags), sort à **8/100,
discussion morte** — les mails contiennent un refus explicite pour budget
insuffisant et une note interne « laisse tomber ». Sans la lecture des mails, cette
fiche arrivait première de la liste d'appels.

**Profil par artiste** (`PROFILS_ARTISTES` dans `scorer_artiste.py`) : esthétiques,
lieux compatibles/à éviter, jauge idéale, cachet, format — donnés au modèle pour
qu'il ne devine pas (ex. Gipsy Kings : festivals = bon fit, ~1000-1800 places,
cachet ~15-20 k€, amplifié). Artiste sans profil → le modèle recherche ces
paramètres sur le web (confiance abaissée). Le script sort aussi la **jauge estimée**
et le **type de lieu** (dans le CSV et la note) pour que tu filtres toi-même.

**Un champ personnalisé par artiste**, dont le **label = le nom de l'artiste**
(ex. champ « Gipsy Kings »). ⚠️ **À créer une seule fois à la main** dans Orfeo
(Réglages → Champs personnalisés) : l'API Orfeo ne permet pas de créer un champ
perso (`POST /custom_field/` renvoie 500). Le script retrouve le champ par son
label et y écrit **le score entier seul** (ex. `25`, pas « 25/100 »). Si le champ
n'existe pas encore, seule la note est écrite (voir ci-dessous) et le script
rappelle de le créer.

La **justification** (+ score, sources, datée) va dans la **section « Notes »**
de la fiche = objets `POST /api/note/` (`content` + `object_id` = pk structure).
⚠️ Ne PAS confondre avec le champ API `structure.notes`, qui est la section
**« Description »** (description du lieu, laissée intacte). La note commence par
`Score <Artiste> : N/100 (date)` — ce préfixe rend l'écriture **idempotente**
(re-scorer met à jour la note existante de l'artiste au lieu d'en créer une
nouvelle ; la PATCH d'une note exige `object_id` dans le body).

Sécurité identique à l'enrichissement : **sans `--apply`, aucune écriture** ; le
CSV `scoring_<slug>.csv` (score, confiance, justification, sources) est toujours
produit. Batch : `--limit`/`--skip` ou `--pks`. Coût ~0,05 $/lieu (web search).

```bash
python3 scorer_artiste.py --artiste "Gipsy Kings" --list-only        # liste (gratuit)
python3 scorer_artiste.py --artiste "Gipsy Kings" --limit 3          # aperçu, aucune écriture
python3 scorer_artiste.py --artiste "Gipsy Kings" --limit 3 --apply  # écrit note + justif
python3 scorer_artiste.py --artiste "Céline Dion" --pks 15714248 --apply
```

Script : `scorer_artiste.py`. (Les champs génériques `score_compatibilite` /
`score_artiste` de `setup_orfeo.py` sont rendus obsolètes par le modèle « 1 champ
par artiste » — conservés, non utilisés.)

### Détail #7 — Cockpit des tâches prioritaires ✅

**Problème.** Le tri natif d'Orfeo ne filtre que par égalité/texte (pas de
« note > 60 »). Et l'unité de travail réelle n'est pas la fiche mais la **tâche**
qui y est rattachée : une fiche sans tâche n'est pas à traiter.

**Solution.** Une page web locale qui répond chaque matin « quelles tâches traiter
en premier ». Petit serveur stdlib (`http.server`) sur `127.0.0.1:8724` — **local
uniquement** (refuse de démarrer si l'hôte n'est pas loopback ; aucun tunnel, aucun
lien public ; `ORFEO_TOKEN` jamais transmis au navigateur). Calcul à la demande
avec cache mémoire (TTL 600 s : 1er appel ~7,5 s de balayage API, suivants instantanés).

**Une passe par source, jointure en mémoire** (tout est inline, aucun fetch détail) :
- `/task/` → tâches (`title`, `done`, `due_date`, `content_type`, `object_id`).
- `/structure/` → scores (`custom_fields` inline ; label = artiste, cf. #5).
- `/project/` → funnel (`place.pk` + `spectacle.name` + `status.name` inline).

**Règles (verrouillées) :**
- **1 ligne = 1 tâche.** Seules les tâches **ouvertes** (`done=false`) rattachées à
  une `structure` apparaissent. Les tâches **« PLUS TARD »** (différées) sont exclues.
- **Le score prime.** L'ordre suit le score artiste de la fiche (valeur réelle).
  Le **retard** (`due_date` passée) et le **funnel** ne font que **départager à score
  égal** — jamais un bonus cumulé qui remonterait une tâche sans valeur.
- Les tâches **sans score** tombent en bas (funnel présent d'abord entre elles).

**Réglages** (`.env`, optionnels) : `COCKPIT_PORT` (8724), `CACHE_TTL` (600),
`SEUIL_SCORE` (0 = tout afficher ; > 0 coupe le bas), `TITRES_EXCLUS`
(défaut `PLUS TARD`, séparés par `;`).

```bash
python3 serveur_cockpit.py          # → http://127.0.0.1:8724
python3 serveur_cockpit.py --dump   # imprime le JSON calculé et quitte (debug)
```

Démarrage auto au login (optionnel) : `com.maisondarwish.orfeo.cockpit.plist`
(`RunAtLoad` + `KeepAlive`, comme l'enrichissement).

Scripts : `serveur_cockpit.py` + `com.maisondarwish.orfeo.cockpit.plist`.

---

## 7. Points d'attention

- **Rate limit 10 req/s** : throttler les boucles (`time.sleep(0.12)`) sur les gros volumes.
- **Idempotence** : chaque script polling vérifie « ai-je déjà agi sur cet objet ? ».
- **Tâches** : `POST /api/task/` exige `assigned_to` + `structure_person`. Récupérer
  son propre user/person `pk` une fois pour toutes.
- **Anti-invention** (enrichissement & scoring) : toute donnée incertaine est
  étiquetée source + confiance, jamais écrite en dur sans validation.
- **Secrets** : `ORFEO_TOKEN` et clés API en variables d'environnement, jamais
  commitées.

---

## 8. Configuration

Toutes les variables sont à placer dans un fichier `.env` à la racine (jamais committé) :

```
ORFEO_TOKEN=ton_token_orfeo
ANTHROPIC_API_KEY=ta_clé_api_claude

# Automation #1
GMAIL_USER=tonemail@gmail.com
GMAIL_APP_PASSWORD=motdepasse16caracteres
EMAIL_TO=destinataire@domaine.com
SEUIL_JOURS=7

# Automation #2 (optionnelles)
ENRICH_MODEL=claude-haiku-4-5        # modèle de l'enrichissement API (le moins cher)
VISION_MODEL=claude-opus-4-8         # modèle du mode vision (extension)
WEB_SEARCH_MAX_USES=3                # plafond de recherches web par appel (pilote le coût)
ENRICH_PORT=8723                     # port du serveur local de l'extension
```

Voir `.env.example` pour le modèle complet.

### Déclenchement automatique (macOS)

Le script se lance au premier démarrage de session de chaque semaine via un LaunchAgent :

```
~/Library/LaunchAgents/com.maisondarwish.orfeo.dormant.plist
```

Pour l'installer sur une nouvelle machine :
```bash
cp com.maisondarwish.orfeo.dormant.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.maisondarwish.orfeo.dormant.plist
```

---

## 9. Scripts disponibles

| Script | Rôle |
|---|---|
| `diagnostic.py` | Vérifie l'auth, liste statuts et champs custom, teste un PATCH |
| `setup_orfeo.py` | Crée les 3 champs de scoring manquants (idempotent) |
| `dormant_dates.py` | Notification hebdomadaire des dates sans activité ✅ |
| `run_dormant.sh` | Wrapper : charge le .env, vérifie si déjà lancé cette semaine |
| `enrichir_structures.py` | Enrichissement des lieux (CLI). `--list-only`, `--limit/--skip`, `--pks <liste>`, `--apply`. Logge le coût réel dans `cout_claude.log` |
| `serveur_enrichissement.py` | Serveur local (127.0.0.1:8723) qui relie l'extension Chrome à la logique d'enrichissement. Endpoints `/health · /enrich · /visuel · /apply` |
| `watchdog_enrichissement.sh` | Chien de garde du serveur d'enrichissement : ping `/health` toutes les 120 s, `kickstart` du LaunchAgent si figé/KO. Silencieux tant que tout va bien |
| `serveur_cockpit.py` | Cockpit local (127.0.0.1:8724) des tâches prioritaires : score prime, exclut « PLUS TARD ». Endpoints `/ · /api/taches · /health`. `--dump` pour debug ✅ |
| `scorer_artiste.py` | Scoring de compatibilité artiste/lieu (CLI). `--artiste`, `--list-only`, `--limit/--skip`, `--pks`, `--apply` ✅ |
| `extension-orfeo/` | Extension Chrome : enrichit la fiche structure ouverte via une commande en français (voir `extension-orfeo/INSTALL.md`) |

### Extension Chrome — enrichir la fiche ouverte

Sur une fiche structure dans Orfeo : clic sur l'extension → commande en français
(« complète l'adresse, le style ») → aperçu → écriture.

```
Popup Chrome ──HTTP──▶ serveur local 127.0.0.1 ──▶ enrichir_structures.py
   (lit le pk)          (clés dans .env)            ──▶ Claude (web search) + API Orfeo
```

- **Aucune clé dans l'extension** : tout passe par le serveur local ; les clés restent dans `.env`.
- **Toujours un aperçu avant écriture** ; règle anti-invention + écriture auto FR conservées.
- **Deux modes** (case « Vision écran ») :
  - *API seul* (défaut, Haiku + web) : écrit via l'API les champs fiables. **~$0.04–0.07/fiche.**
  - *Hybride* (coché, + Opus vision) : envoie un screenshot + la carte des champs pour
    remplir aussi les champs custom à l'écran (tu enregistres dans Orfeo). **~$0.15–0.20/fiche.**
- **Coût** plafonné par `WEB_SEARCH_MAX_USES` (défaut 3) et journalisé dans `cout_claude.log`.

Installation : voir `extension-orfeo/INSTALL.md`. Démarrage auto du serveur (option) :
`com.maisondarwish.orfeo.enrich.plist`.

#### Robustesse — le serveur ne doit jamais rester « injoignable »

Si l'extension affiche **« Serveur local injoignable. Lance : python3 serveur_enrichissement.py »**,
c'est que rien n'écoute sur `127.0.0.1:8723`. Deux garde-fous couvrent tous les cas de panne :

| Panne | Rattrapée par | Délai |
|---|---|---|
| Le serveur **plante** (le process se termine) | `KeepAlive` de `com.maisondarwish.orfeo.enrich.plist` | immédiat |
| Le serveur **se fige** (process vivant mais muet) | watchdog `com.maisondarwish.orfeo.enrich-watchdog.plist` | ≤ 120 s |
| Crash-loop / throttle launchd | watchdog (re-teste toutes les 120 s) | ≤ 120 s |
| Reboot / login | `RunAtLoad` sur les deux agents | au démarrage |

`KeepAlive` seul ne suffit pas : il ne relance que si le process **se termine**, pas s'il se **fige**.
Le watchdog (`watchdog_enrichissement.sh`, planifié par `StartInterval` 120 s) comble ce trou en
appelant `/health` et en forçant un `kickstart` sinon.

Installation des deux agents sur une nouvelle machine :
```bash
cp com.maisondarwish.orfeo.enrich.plist com.maisondarwish.orfeo.enrich-watchdog.plist ~/Library/LaunchAgents/
launchctl load -w ~/Library/LaunchAgents/com.maisondarwish.orfeo.enrich.plist
launchctl load -w ~/Library/LaunchAgents/com.maisondarwish.orfeo.enrich-watchdog.plist
```

Relance manuelle de secours (si jamais ça persiste au-delà de ~2 min) :
```bash
launchctl kickstart -k gui/$(id -u)/com.maisondarwish.orfeo.enrich
```
