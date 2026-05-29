# Module Gestion — Prototype Pauco

Prototype standalone de gestion financiere pour restaurateurs.

## Lancer

```bash
cd gestion_module
pip install flask
python app.py
```

Ouvrir http://localhost:5001

## Pages

- `/` — Tableau de bord mensuel (CA, ratios, courbe 12 mois)
- `/ca-jour` — Saisie du soir (CA, couverts, depenses) + 30 derniers jours
- `/depenses` — Depenses fixes (loyer, salaires...) + variables (matieres, fournitures...)
- `/ratios` — Ratios calcules automatiquement avec indicateurs couleur
- `/ratios/exemples` — References secteur (creperie, pizzeria, brasserie, gastro...)

## Stack

- Flask + SQLite (fichier `gestion.db` cree automatiquement)
- Chart.js (CDN) pour les graphiques
- Design identique a app.paucoandco.com
