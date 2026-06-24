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

### 5a. Dans l'UI Orfeo (ne peut pas se faire via API)

Créer les statuts manquants dans **Paramètres → Statuts d'opportunités** :
- `En négociation`
- `Option` (si absent)

Déjà présents : `Intérêt`, `Gagnée`, `Perdue`.

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
| 1 | Notification quotidienne des dates dormantes | ★☆☆☆☆ | Élevé | En cours |
| 2 | Enrichissement auto des fiches incomplètes | ★★★☆☆ | Très élevé | À faire |
| 3 | Création auto de tâches selon statut | ★★☆☆☆ | Élevé | À faire |
| 4 | Assistant questions en français sur les données Orfeo | ★★★☆☆ | Moyen-élevé | À faire |
| 5 | Scoring de compatibilité artiste / lieu | ★★★★☆ | Très élevé | À faire |
| 6 | Brouillons de relance | ★★★☆☆ | Moyen | À faire |

Ordre de construction : **1 → 2 → 3 → 4 → 5 → 6** (valeur rapide d'abord,
gros chantiers une fois l'API maîtrisée).

### Détail #1 — Dates dormantes

Chaque matin, lister les dates sans activité depuis X jours (défaut : 7).
Statuts surveillés : `Intérêt`, `En négociation`.

```
GET /api/project/?status__in=<pk_interet>,<pk_en_nego>&update_date__lt=<J-X>
```

→ récap mail (nom du lieu, statut, jours d'inactivité, lien fiche). Cron 1×/jour.

Script : `dormant_dates.py`

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
Étapes : récupérer lieux + champs natifs (Orfeo) → récupérer la **programmation
passée** de chaque salle (**source externe : absente d'Orfeo**, c'est la vraie
difficulté) → API Claude juge la compatibilité (genre, artistes similaires déjà
programmés, capacité, type) → score 0-100 + justification → PATCH dans Orfeo
(`score_compatibilite`, `score_artiste`, `score_date`).

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

```bash
# Variables d'environnement requises
export ORFEO_TOKEN="<ton_token_orfeo>"
export ANTHROPIC_API_KEY="<ta_clé_api_claude>"   # pour scoring / structuration

# Pour l'automation #1 (email)
export EMAIL_FROM="<ton_adresse_gmail>"
export EMAIL_TO="<destinataire>"
export GMAIL_APP_PASSWORD="<mot_de_passe_application_gmail>"

# Optionnel
export SEUIL_JOURS="7"   # jours sans activité avant alerte (défaut : 7)
```

`.gitignore` minimal :
```
.env
__pycache__/
*.pyc
secrets/
```

---

## 9. Scripts disponibles

| Script | Rôle |
|---|---|
| `diagnostic.py` | Vérifie l'auth, liste statuts et champs custom, teste un PATCH |
| `setup_orfeo.py` | Crée les 3 champs de scoring manquants (idempotent) |
| `dormant_dates.py` | Notification quotidienne des dates sans activité *(en cours)* |
