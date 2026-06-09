# -*- coding: utf-8 -*-
"""Couche base de données SQLite — connexion, guard, initialisation et migrations."""

import os
import sqlite3
from flask import g
from modules.config import app

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "gestion.db")

_PROTECTED_TABLES = {"restaurants", "password_resets"}


class GuardedDB:
    """Wrapper that blocks unscoped DELETE on protected tables."""
    __slots__ = ("_conn",)

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        sql_upper = sql.strip().upper()
        if sql_upper.startswith("DELETE FROM"):
            for table in _PROTECTED_TABLES:
                if table.upper() in sql_upper and "WHERE" not in sql_upper:
                    msg = f"[GUARD] BLOCKED: unscoped DELETE on '{table}': {sql}"
                    print(msg)
                    raise RuntimeError(msg)
        if params is not None:
            return self._conn.execute(sql, params)
        return self._conn.execute(sql)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_db():
    """Return a GuardedDB connection for the current request (scoped to Flask g)."""
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = GuardedDB(conn)
    return g.db


@app.teardown_appcontext
def close_db(exc):
    """Close the DB connection at the end of each request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create all tables, run migrations, and seed default data."""
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS ca_jour (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            ca REAL NOT NULL DEFAULT 0,
            ca_restaurant REAL NOT NULL DEFAULT 0,
            ca_bar REAL NOT NULL DEFAULT 0,
            couverts_midi INTEGER NOT NULL DEFAULT 0,
            couverts_soir INTEGER NOT NULL DEFAULT 0,
            tickets_bar INTEGER NOT NULL DEFAULT 0,
            ticket_moyen REAL NOT NULL DEFAULT 0,
            ticket_moyen_bar REAL NOT NULL DEFAULT 0,
            commentaire TEXT DEFAULT '',
            ca_detail TEXT DEFAULT '',
            restaurant_id TEXT DEFAULT '',
            UNIQUE(restaurant_id, date)
        );
        CREATE TABLE IF NOT EXISTS centres_revenus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS depenses_fixes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            categorie TEXT NOT NULL,
            description TEXT DEFAULT '',
            montant REAL NOT NULL DEFAULT 0,
            mois TEXT NOT NULL,
            recurrente INTEGER NOT NULL DEFAULT 0,
            desactivee_mois TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS depenses_variables (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            categorie TEXT NOT NULL,
            description TEXT DEFAULT '',
            montant REAL NOT NULL DEFAULT 0,
            type TEXT NOT NULL DEFAULT 'variable',
            mois TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fournisseurs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'Autre',
            telephone TEXT DEFAULT '',
            email TEXT DEFAULT '',
            nom_commercial TEXT DEFAULT '',
            telephone_commercial TEXT DEFAULT '',
            email_commercial TEXT DEFAULT '',
            site_web TEXT DEFAULT '',
            adresse TEXT DEFAULT '',
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS employes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prenom TEXT NOT NULL,
            nom TEXT NOT NULL,
            poste TEXT NOT NULL DEFAULT 'Salle',
            type_contrat TEXT NOT NULL DEFAULT 'CDI',
            date_debut TEXT NOT NULL,
            date_fin TEXT DEFAULT '',
            salaire_brut REAL NOT NULL DEFAULT 0,
            heures_semaine REAL NOT NULL DEFAULT 35,
            statut TEXT NOT NULL DEFAULT 'Actif',
            phone TEXT DEFAULT '',
            date_naissance TEXT DEFAULT '',
            email TEXT DEFAULT '',
            photo_url TEXT DEFAULT '',
            ordre INTEGER NOT NULL DEFAULT 0,
            cp_acquis REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS planning (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employe_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            heure_debut TEXT NOT NULL,
            heure_fin TEXT NOT NULL,
            poste TEXT NOT NULL DEFAULT 'Salle',
            shift_id INTEGER NOT NULL DEFAULT 0,
            shift_nom TEXT DEFAULT '',
            shift_couleur TEXT DEFAULT '',
            FOREIGN KEY (employe_id) REFERENCES employes(id)
        );
        CREATE TABLE IF NOT EXISTS fiches_techniques (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            categorie TEXT NOT NULL DEFAULT 'Plats',
            prix_vente_ht REAL NOT NULL DEFAULT 0,
            prix_vente_ttc REAL NOT NULL DEFAULT 0,
            cout_ht REAL NOT NULL DEFAULT 0,
            coefficient REAL NOT NULL DEFAULT 0,
            ratio_mp REAL NOT NULL DEFAULT 0,
            marge_ht REAL NOT NULL DEFAULT 0,
            statut TEXT NOT NULL DEFAULT 'actif',
            nb_portions INTEGER NOT NULL DEFAULT 0,
            temps_preparation INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS fiche_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fiche_id INTEGER NOT NULL,
            produit TEXT NOT NULL,
            prix_kilo REAL NOT NULL DEFAULT 0,
            grammes REAL NOT NULL DEFAULT 0,
            cout_portion REAL NOT NULL DEFAULT 0,
            unite TEXT NOT NULL DEFAULT 'g',
            quantite REAL NOT NULL DEFAULT 0,
            prix_litre REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (fiche_id) REFERENCES fiches_techniques(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS fiches_cocktails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            categorie TEXT NOT NULL DEFAULT 'Creations',
            volume_cl REAL NOT NULL DEFAULT 0,
            prix_vente_ht REAL NOT NULL DEFAULT 0,
            prix_vente_ttc REAL NOT NULL DEFAULT 0,
            cout_ht REAL NOT NULL DEFAULT 0,
            coefficient REAL NOT NULL DEFAULT 0,
            ratio_mp REAL NOT NULL DEFAULT 0,
            marge_ht REAL NOT NULL DEFAULT 0,
            statut TEXT NOT NULL DEFAULT 'actif'
        );
        CREATE TABLE IF NOT EXISTS cocktail_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cocktail_id INTEGER NOT NULL,
            produit TEXT NOT NULL,
            cout_ht_unitaire REAL NOT NULL DEFAULT 0,
            qte_bouteille_cl REAL NOT NULL DEFAULT 0,
            cout_ht_cl REAL NOT NULL DEFAULT 0,
            qte_utilisee_cl REAL NOT NULL DEFAULT 0,
            cout_ht_verre REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (cocktail_id) REFERENCES fiches_cocktails(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS boissons_bar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            categorie TEXT NOT NULL DEFAULT 'Autres spiritueux',
            contenance_cl REAL NOT NULL DEFAULT 0,
            prix_achat_ht REAL NOT NULL DEFAULT 0,
            prix_vente_ttc REAL NOT NULL DEFAULT 0,
            prix_vente_ht REAL NOT NULL DEFAULT 0,
            cout_ht_cl REAL NOT NULL DEFAULT 0,
            perte_casse REAL NOT NULL DEFAULT 0,
            perte_degustation REAL NOT NULL DEFAULT 0,
            perte_evaporation REAL NOT NULL DEFAULT 0,
            cout_ajuste REAL NOT NULL DEFAULT 0,
            coefficient REAL NOT NULL DEFAULT 0,
            ratio_mp REAL NOT NULL DEFAULT 0,
            marge_ht REAL NOT NULL DEFAULT 0,
            statut TEXT NOT NULL DEFAULT 'actif'
        );
        CREATE TABLE IF NOT EXISTS conges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employe_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'Congés payés',
            date_debut TEXT NOT NULL,
            date_fin TEXT NOT NULL,
            commentaire TEXT DEFAULT '',
            FOREIGN KEY (employe_id) REFERENCES employes(id)
        );
        CREATE TABLE IF NOT EXISTS historique_fiches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fiche_id INTEGER NOT NULL,
            fiche_type TEXT NOT NULL,
            champ TEXT NOT NULL,
            ancienne_valeur TEXT DEFAULT '',
            nouvelle_valeur TEXT DEFAULT '',
            date_modification TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            pole TEXT NOT NULL DEFAULT 'Salle',
            heure_debut TEXT DEFAULT '',
            heure_fin TEXT DEFAULT '',
            couleur TEXT NOT NULL DEFAULT '#2D6A4A'
        );
        CREATE TABLE IF NOT EXISTS employe_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employe_id INTEGER NOT NULL,
            nom TEXT NOT NULL,
            r2_key TEXT NOT NULL,
            uploaded_at TEXT NOT NULL,
            FOREIGN KEY (employe_id) REFERENCES employes(id)
        );
        CREATE TABLE IF NOT EXISTS absence_types (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            nom TEXT NOT NULL,
            couleur TEXT NOT NULL DEFAULT '#6B7280',
            remuneree INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS postes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            pole TEXT NOT NULL DEFAULT 'Salle',
            is_default INTEGER NOT NULL DEFAULT 0,
            UNIQUE(nom, pole)
        );
        CREATE TABLE IF NOT EXISTS categories_fiches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL DEFAULT 'food',
            nom TEXT NOT NULL,
            ordre INTEGER NOT NULL DEFAULT 0,
            restaurant_id TEXT DEFAULT '',
            UNIQUE(restaurant_id, type, nom)
        );
        CREATE TABLE IF NOT EXISTS fermetures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT DEFAULT '',
            recurrence INTEGER NOT NULL DEFAULT 0,
            jour_semaine INTEGER DEFAULT -1
        );
        CREATE TABLE IF NOT EXISTS evenements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            titre TEXT NOT NULL,
            date TEXT NOT NULL,
            description TEXT DEFAULT '',
            couleur TEXT DEFAULT '#2D6A4A',
            note TEXT DEFAULT '',
            type_evt TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS allergenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom_plat TEXT NOT NULL,
            categorie TEXT NOT NULL DEFAULT 'Plat',
            gluten INTEGER NOT NULL DEFAULT 0,
            crustaces INTEGER NOT NULL DEFAULT 0,
            oeufs INTEGER NOT NULL DEFAULT 0,
            poisson INTEGER NOT NULL DEFAULT 0,
            arachides INTEGER NOT NULL DEFAULT 0,
            soja INTEGER NOT NULL DEFAULT 0,
            lait INTEGER NOT NULL DEFAULT 0,
            fruits_a_coque INTEGER NOT NULL DEFAULT 0,
            celeri INTEGER NOT NULL DEFAULT 0,
            moutarde INTEGER NOT NULL DEFAULT 0,
            sesame INTEGER NOT NULL DEFAULT 0,
            sulfites INTEGER NOT NULL DEFAULT 0,
            lupin INTEGER NOT NULL DEFAULT 0,
            mollusques INTEGER NOT NULL DEFAULT 0,
            autres TEXT DEFAULT '',
            airtable_id TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS event_types_custom (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nom TEXT NOT NULL,
            couleur TEXT NOT NULL DEFAULT '#6B7280'
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expediteur TEXT NOT NULL DEFAULT 'Gerant',
            destinataires TEXT NOT NULL DEFAULT 'tous',
            objet TEXT NOT NULL,
            message TEXT NOT NULL,
            date TEXT NOT NULL,
            lu INTEGER NOT NULL DEFAULT 0
        );
    """)
    # Migrations
    for migration in [
        "ALTER TABLE depenses_fixes ADD COLUMN description TEXT DEFAULT ''",
        "ALTER TABLE fiches_techniques ADD COLUMN statut TEXT NOT NULL DEFAULT 'actif'",
        "ALTER TABLE fournisseurs ADD COLUMN type TEXT NOT NULL DEFAULT 'Autre'",
        "ALTER TABLE fournisseurs ADD COLUMN telephone TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN email TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN nom_commercial TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN telephone_commercial TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN email_commercial TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN site_web TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN adresse TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN notes TEXT DEFAULT ''",
        "ALTER TABLE ca_jour ADD COLUMN ca_detail TEXT DEFAULT ''",
        "ALTER TABLE ca_jour ADD COLUMN ca_restaurant REAL NOT NULL DEFAULT 0",
        "ALTER TABLE ca_jour ADD COLUMN ca_bar REAL NOT NULL DEFAULT 0",
        "ALTER TABLE ca_jour ADD COLUMN tickets_bar INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ca_jour ADD COLUMN ticket_moyen_bar REAL NOT NULL DEFAULT 0",
        "ALTER TABLE employes ADD COLUMN ordre INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE planning ADD COLUMN shift_id INTEGER DEFAULT 0",
        "ALTER TABLE planning ADD COLUMN shift_nom TEXT DEFAULT ''",
        "ALTER TABLE planning ADD COLUMN shift_couleur TEXT DEFAULT ''",
        "ALTER TABLE fiches_techniques ADD COLUMN nb_portions INTEGER DEFAULT 0",
        "ALTER TABLE fiches_techniques ADD COLUMN temps_preparation INTEGER DEFAULT 0",
        "ALTER TABLE employes ADD COLUMN phone TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN date_naissance TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN email TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN adresse TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN numero_secu TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN iban TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN cp_acquis REAL DEFAULT 0",
        "ALTER TABLE employes ADD COLUMN photo_url TEXT DEFAULT ''",
        "ALTER TABLE depenses_fixes ADD COLUMN recurrente INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE depenses_fixes ADD COLUMN desactivee_mois TEXT DEFAULT ''",
        "ALTER TABLE depenses_variables ADD COLUMN recurrente INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE depenses_variables ADD COLUMN frequence TEXT DEFAULT 'mensuelle'",
        """CREATE TABLE IF NOT EXISTS documents_temperatures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            periode TEXT NOT NULL,
            type_doc TEXT NOT NULL DEFAULT 'Releve hebdomadaire',
            filename TEXT NOT NULL,
            r2_key TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        )""",
        "ALTER TABLE evenements ADD COLUMN couleur TEXT DEFAULT '#2D6A4A'",
        "ALTER TABLE evenements ADD COLUMN note TEXT DEFAULT ''",
        "ALTER TABLE evenements ADD COLUMN type_evt TEXT DEFAULT ''",
        "ALTER TABLE categories_fiches ADD COLUMN ordre INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE employes ADD COLUMN solde_initial REAL NOT NULL DEFAULT 0",
        "ALTER TABLE employes ADD COLUMN date_debut_compteur TEXT DEFAULT ''",
        # Multi-tenant: restaurant_id on all data tables
        "ALTER TABLE ca_jour ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE depenses_fixes ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE depenses_variables ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE fournisseurs ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE employes ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE planning ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE fiches_techniques ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE fiche_ingredients ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE fiches_cocktails ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE cocktail_ingredients ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE boissons_bar ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE conges ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE shifts ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE postes ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE categories_fiches ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE fermetures ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE evenements ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE allergenes ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE messages ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE historique_fiches ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE event_types_custom ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE absence_types ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE centres_revenus ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE documents_temperatures ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE employe_documents ADD COLUMN restaurant_id TEXT DEFAULT ''",
        "ALTER TABLE employe_documents ADD COLUMN emp_key TEXT DEFAULT ''",
        "ALTER TABLE planning ADD COLUMN repas INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE planning ADD COLUMN duree_repas INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE fiche_ingredients ADD COLUMN unite TEXT NOT NULL DEFAULT 'g'",
        "ALTER TABLE fiche_ingredients ADD COLUMN quantite REAL NOT NULL DEFAULT 0",
        "ALTER TABLE fiche_ingredients ADD COLUMN prix_litre REAL NOT NULL DEFAULT 0",
    ]:
        try:
            db.execute(migration)
        except sqlite3.OperationalError:
            pass
    # Migrate ca_jour UNIQUE constraint: date → (restaurant_id, date)
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='ca_jour'").fetchone()
        if row and "UNIQUE(restaurant_id" not in (row[0] or ""):
            db.execute("""CREATE TABLE IF NOT EXISTS ca_jour_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ca REAL NOT NULL DEFAULT 0,
                ca_restaurant REAL NOT NULL DEFAULT 0,
                ca_bar REAL NOT NULL DEFAULT 0,
                couverts_midi INTEGER NOT NULL DEFAULT 0,
                couverts_soir INTEGER NOT NULL DEFAULT 0,
                tickets_bar INTEGER NOT NULL DEFAULT 0,
                ticket_moyen REAL NOT NULL DEFAULT 0,
                ticket_moyen_bar REAL NOT NULL DEFAULT 0,
                commentaire TEXT DEFAULT '',
                ca_detail TEXT DEFAULT '',
                restaurant_id TEXT DEFAULT '',
                UNIQUE(restaurant_id, date)
            )""")
            db.execute("""INSERT OR IGNORE INTO ca_jour_new
                (id,date,ca,ca_restaurant,ca_bar,couverts_midi,couverts_soir,tickets_bar,ticket_moyen,ticket_moyen_bar,commentaire,ca_detail,restaurant_id)
                SELECT id,date,ca,ca_restaurant,ca_bar,couverts_midi,couverts_soir,tickets_bar,ticket_moyen,ticket_moyen_bar,commentaire,ca_detail,COALESCE(restaurant_id,'')
                FROM ca_jour""")
            db.execute("DROP TABLE ca_jour")
            db.execute("ALTER TABLE ca_jour_new RENAME TO ca_jour")
    except Exception:
        pass
    # Migrate fournisseurs: remove UNIQUE(nom) for multi-tenant
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='fournisseurs'").fetchone()
        if row and "UNIQUE" in (row[0] or "") and "restaurant_id" not in (row[0] or ""):
            db.execute("""CREATE TABLE IF NOT EXISTS fournisseurs_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'Autre',
                telephone TEXT DEFAULT '',
                email TEXT DEFAULT '',
                nom_commercial TEXT DEFAULT '',
                telephone_commercial TEXT DEFAULT '',
                email_commercial TEXT DEFAULT '',
                site_web TEXT DEFAULT '',
                adresse TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                restaurant_id TEXT DEFAULT ''
            )""")
            db.execute("""INSERT OR IGNORE INTO fournisseurs_new
                (id,nom,type,telephone,email,nom_commercial,telephone_commercial,email_commercial,site_web,adresse,notes,restaurant_id)
                SELECT id,nom,type,telephone,email,nom_commercial,telephone_commercial,email_commercial,site_web,adresse,notes,COALESCE(restaurant_id,'')
                FROM fournisseurs""")
            db.execute("DROP TABLE fournisseurs")
            db.execute("ALTER TABLE fournisseurs_new RENAME TO fournisseurs")
    except Exception:
        pass
    # Migrate categories_fiches UNIQUE constraint to include restaurant_id
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='categories_fiches'").fetchone()
        if row and "UNIQUE(restaurant_id" not in (row[0] or ""):
            db.execute("""CREATE TABLE IF NOT EXISTS categories_fiches_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL DEFAULT 'food',
                nom TEXT NOT NULL,
                ordre INTEGER NOT NULL DEFAULT 0,
                restaurant_id TEXT DEFAULT '',
                UNIQUE(restaurant_id, type, nom)
            )""")
            db.execute("INSERT OR IGNORE INTO categories_fiches_new (id, type, nom, ordre, restaurant_id) SELECT id, type, nom, ordre, COALESCE(restaurant_id, '') FROM categories_fiches")
            db.execute("DROP TABLE categories_fiches")
            db.execute("ALTER TABLE categories_fiches_new RENAME TO categories_fiches")
    except Exception:
        pass
    # Migrate postes UNIQUE constraint to include restaurant_id
    try:
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='postes'").fetchone()
        if row and "UNIQUE(restaurant_id" not in (row[0] or ""):
            db.execute("""CREATE TABLE IF NOT EXISTS postes_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nom TEXT NOT NULL,
                pole TEXT NOT NULL DEFAULT 'Salle',
                is_default INTEGER NOT NULL DEFAULT 0,
                restaurant_id TEXT DEFAULT '',
                UNIQUE(restaurant_id, nom, pole)
            )""")
            db.execute("INSERT OR IGNORE INTO postes_new (id, nom, pole, is_default, restaurant_id) SELECT id, nom, pole, is_default, COALESCE(restaurant_id, '') FROM postes")
            db.execute("DROP TABLE postes")
            db.execute("ALTER TABLE postes_new RENAME TO postes")
    except Exception as e:
        print(f"[MIGRATION postes] {e}")
    # Seed demo shifts if empty
    if db.execute("SELECT COUNT(*) FROM shifts").fetchone()[0] == 0:
        demo_shifts = [
            ("Ouverture", "Salle", "09:00", "15:00", "#2D6A4A"),
            ("Fermeture", "Salle", "15:00", "23:00", "#2D6A4A"),
            ("Coupure", "Salle", "11:00", "23:00", "#2D6A4A"),
            ("Ouverture Bar", "Bar", "17:00", "23:00", "#1D4ED8"),
            ("Fermeture Bar", "Bar", "19:00", "02:00", "#1D4ED8"),
            ("Service Midi", "Cuisine", "09:00", "15:30", "#D97706"),
            ("Service Soir", "Cuisine", "15:00", "23:00", "#D97706"),
            ("Coupure Desserts", "Cuisine", "10:00", "14:00", "#D97706"),
        ]
        for nom, pole, hd, hf, coul in demo_shifts:
            db.execute("INSERT INTO shifts (nom, pole, heure_debut, heure_fin, couleur) VALUES (?,?,?,?,?)", (nom, pole, hd, hf, coul))
    # Seed default absence types if empty
    if db.execute("SELECT COUNT(*) FROM absence_types").fetchone()[0] == 0:
        for code, nom, couleur, rem in [
            ("R", "Repos", "#9CA3AF", 0),
            ("CP", "Conges payes", "#059669", 1),
            ("AM", "Arret maladie", "#D97706", 1),
            ("AI", "Absence injustifiee", "#DC2626", 0),
            ("FOR", "Formation", "#1D4ED8", 1),
            ("EF", "Evenement familial", "#7C3AED", 1),
        ]:
            db.execute("INSERT INTO absence_types (code, nom, couleur, remuneree) VALUES (?,?,?,?)", (code, nom, couleur, rem))
    db.commit()
    db.close()
