# automation-orfeo
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
- **PROJECT** = une **AFFAIRE commerciale** = artiste + lieu + date potentielle.
  C'est lui qui porte le **STATUT** (le tunnel de vente).
- **SPECTACLE** = le projet artistique d'un artiste (ce qui est vendu).
- **TASK** = une tâche de suivi, rattachable à un objet.

**Règle adoptée :** un lieu travaillé pour un artiste = un **PROJECT**, même au
tout début (stade « Lead »), avec le bon statut. Le tunnel commercial vit dans
les statuts de PROJECT, pas dans le nom des tâches.

---

## 3. API Orfeo — faits techniques vérifiés

```
BASE URL   : https://orfeoapp.com/api/
AUTH       : header  ->  Authorization: Token <TOKEN>
RATE LIMIT : 10 requêtes / seconde max. Au-delà : HTTP 429. Throttle les boucles.
FORMAT     : JSON paginé  -> { count, next, previous, results }. Clé primaire = "pk".
PAGINATION : ?page_size=N  &  ?page=N
```

### Endpoints utiles

```
GET/POST/PATCH  /api/project/         filtres: status, status__in, start_date__gt/lt,
                                      update_date__gt/lt, creation_date__gt/lt,
                                      custom_fields, spectacle
GET/POST        /api/project_status/  les statuts du tunnel
GET/POST/PATCH  /api/structure/       les lieux + champs personnalisés
GET/POST/PATCH  /api/task/            POST requiert: title, due_date, assigned_to,
                                      structure_person ; rattachement via
                                      content_type + object_id
GET/POST        /api/custom_field/    chaque champ a une "key" filtrable
GET/POST        /api/note/
```

Filtre sur champ personnalisé : `?custom_fields=<key>:<valeur>`

**À vérifier dès le départ :** `custom_fields` apparaît en lecture seule sur la
vue LISTE mais devrait s'écrire via PATCH sur la vue OBJET
(`/api/structure/<id>/`). Confirmer par un PATCH de test avant de construire dessus.

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

## 5. Prérequis fondateur (une seule fois)

Avant toute automatisation, structurer les données :

1. **Créer les statuts du tunnel** (`POST /api/project_status/`) :
   `Lead → Qualifié → Contacté → En négociation → Option → Confirmé → Perdu`
2. **Créer les champs personnalisés** sur `structure` (`POST /api/custom_field/`) :
   `genre_musical` (choices), `capacite` (int), `type_lieu` (choices),
   `score_compatibilite` (int 0-100), `score_artiste` (text), `score_date` (date)
3. **Générer le token API** dans Orfeo (page « API & webhooks ») et le placer
   dans `ORFEO_TOKEN`.

---

## 6. Feuille de route des automatisations

| # | Automatisation | Complexité | ROI |
|---|----------------|-----------|-----|
| 1 | Notification quotidienne des leads dormants | ★☆☆☆☆ | Élevé |
| 2 | Enrichissement auto des fiches incomplètes | ★★★☆☆ | Très élevé |
| 3 | Création auto de tâches selon statut | ★★☆☆☆ | Élevé |
| 4 | Assistant questions en français sur les données Orfeo | ★★★☆☆ | Moyen-élevé |
| 5 | Scoring de compatibilité artiste / lieu | ★★★★☆ | Très élevé |
| 6 | Brouillons de relance | ★★★☆☆ | Moyen |

Ordre de construction : **1 → 2 → 3 → 4 → 5 → 6** (valeur rapide d'abord,
gros chantiers une fois l'API maîtrisée).

### Détail #1 — Leads dormants
Chaque matin, lister les projects sans activité depuis X jours.
`GET /api/project/?status__in=<lead,qualifié,contacté>&update_date__lt=<J-X>`
→ récap mail (nom, statut, jours d'inactivité, lien fiche). Cron 1×/jour.

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

### Détail #5 — Scoring de compatibilité
Pour un artiste, scorer les 100-300 lieux et prioriser les appels.
Étapes : récupérer lieux + champs custom (Orfeo) → récupérer la **programmation
passée** de chaque salle (**source externe : absente d'Orfeo**, c'est la vraie
difficulté) → API Claude juge la compatibilité (genre, artistes similaires déjà
programmés, capacité, type) → score 0-100 + justification → PATCH dans Orfeo.

---

## 7. Points d'attention

- **Rate limit 10 req/s** : throttler les boucles (`time.sleep`) sur les gros volumes.
- **Idempotence** : chaque script polling vérifie « ai-je déjà agi sur cet objet ? ».
- **Tâches** : `POST /api/task/` exige `assigned_to` + `structure_person`. Récupérer
  son propre user/person `pk` une fois pour toutes.
- **Anti-invention** (enrichissement & scoring) : toute donnée incertaine est
  étiquetée source + confiance, jamais écrite en dur sans validation.
- **Secrets** : `ORFEO_TOKEN` et clés API en variables d'environnement, jamais
  commitées. Prévoir un `.gitignore` (voir §8).

---

## 8. Configuration

```bash
# Variables d'environnement requises
export ORFEO_TOKEN="<ton_token_orfeo>"
export ANTHROPIC_API_KEY="<ta_clé_api_claude>"   # pour scoring / structuration
```

`.gitignore` minimal :
```
.env
__pycache__/
*.pyc
secrets/
```

---

## 9. Première tâche (script de diagnostic)

Avant de coder du définitif :
1. Vérifier l'auth : `GET /api/project/?page_size=1` avec `ORFEO_TOKEN`.
2. Lister les statuts existants (`GET /api/project_status/`) et les champs
   personnalisés (`GET /api/custom_field/`).
3. Faire un **PATCH de test** sur une `structure` pour confirmer qu'on peut écrire
   dans un champ personnalisé (étape critique).

Afficher les résultats, puis décider de la suite.
