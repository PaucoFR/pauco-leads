"""
Airtable client module — replaces SQLite for all gestion data.
Auth (users, password_resets) stays in SQLite.
Every record is scoped by Restaurant_ID (Airtable record ID from App Clients).
Cache: Redis (primary) with in-memory fallback.
"""

import os, re, time, functools, threading, json as _json
from datetime import datetime, date
from pyairtable import Api

try:
    import redis as _redis_lib
except ImportError:
    _redis_lib = None

# ── Config ──────────────────────────────────────────────────────────────
# Railway: AIRTABLE_PAT + AIRTABLE_BASE. Fallback: AIRTABLE_API_KEY (same token, legacy name)
AIRTABLE_TOKEN = os.getenv("AIRTABLE_PAT") or os.getenv("AIRTABLE_API_KEY") or ""
AIRTABLE_BASE  = os.getenv("AIRTABLE_BASE", "app37TquPqedRoJ96")
if not AIRTABLE_TOKEN:
    print("[AIRTABLE] WARNING: No AIRTABLE_PAT found — Airtable calls will fail")

# ── Table IDs ───────────────────────────────────────────────────────────
TABLES = {
    "restaurants":        "tblxhvnwlfLU8bLPU",   # App Clients
    "utilisateurs":       "tblyCtPnsNTPCKywK",
    "avis":               "tblCK01PqQtq2c3hr",
    "ca_jour":            "tblxHup3nlZFgkiql",   # CA_Journalier
    "depenses":           "tblpJ670LK1IZC7aN",   # Dépenses (fixes + variables)
    "employes":           "tblZomXIhuKCqAkSk",
    "planning":           "tblw8pyrrQpUCSQFK",
    "fiches":             "tbl0wSokB660jIw51",   # Fiches_Techniques (food+cocktail+bar)
    "settings":           "tble74jYlRv8zI7HT",
    "fournisseurs":       "tbljh8kuiWwg1gOyZ",
    "messages":           "tblveiESceKL4VPJl",
    "suivi_clients":      "tblAz4YQQou8Nqqcf",
    "planning_templates": "tblUdn35CD3xul9JO",
    "conges":             "tblcQQfGOlAoxb5Vz",
    "evenements":         "tblOvdz407kDkLQ40",
    "fermetures":         "tbl3n5FBHWpzuU2xr",
    "shifts":             "tblVLJVXu3pcBe5d8",
    "postes":             "tblQObXvIzgqmkeB7",
    "categories_fiches":  "tblhvRMDOobSALOnZ",
    "ingredients":        "tblNcANI1w3RZ9EvB",   # Fiche_Ingredients
    "historique_fiches":  "tbllGfd2ouhrhHPMC",
    "demo_codes":         "tblFJVbMdcYagBfSB",
    "allergenes":         "tblAeMUNLHlOCg32c",
    "roles":              "tbl7OJLeSokPfuYyM",
    "conformite":         "tblI5rr64z7B3kaX7",
    "origine_viandes":    "tblSnQB3AMNC3mdcm",
    "equipements_froid":  "tblQhG3hLfGl974WB",
    "horaires_travail":   "tblkGdIowW2W88tbb",
    "etiquettes_haccp":   "tblMIEzQIJXNEQr5f",
    "releves_temp":       "tblnqZIcMOUlz2wE2",
    "receptions":         "tblSIKV7cnoR3PcxC",
    "tracabilite_viandes_hygiene": "tblEbQTl6KJzW9rwW",
    "checklists":         "tblwaERBS46afxVhS",
    "checklist_items":    "tblgzFMhDfJx1r2ZO",
    "stats_pub":          "tbl6u5ftQE3uGQHci",
    "stats_reseaux":      "tblR24xoIMfIcN9mU",
    "shooting_medias":    "tblEyjEbsbQhw6zS4",
    "recrutement_candidatures": "tblg9ZYTSDV0eKv18",
    "recrutement_vivier":       "tblocqUWnNAT0rA2l",
    "recrutement_missions":     "tblDEkmhrGgwkP0JD",
    "types_evenements":         "tbltHP5Dw67hCIN3N",
}

# Tables that require a valid Restaurant_ID (must exist in "restaurants" table)
_TABLES_WITH_RESTAURANT_ID = frozenset({
    "employes", "planning", "ca_jour", "depenses", "fiches", "settings",
    "fournisseurs", "messages", "suivi_clients", "planning_templates",
    "conges", "evenements", "fermetures", "shifts", "postes",
    "categories_fiches", "ingredients", "historique_fiches", "avis",
    "conformite", "origine_viandes", "equipements_froid", "horaires_travail",
    "roles",
    "allergenes",
    "etiquettes_haccp", "releves_temp", "receptions", "tracabilite_viandes_hygiene",
    "checklists", "checklist_items",
    "types_evenements",
})

# PROTECTION ABSOLUE: tables Airtable sur lesquelles delete/batch_delete sont interdits
_AIRTABLE_NO_DELETE = frozenset({"utilisateurs", "restaurants"})

# Cache of known valid Restaurant_IDs (avoids repeated Airtable lookups)
_valid_rids_cache = {"_ts": 0}
_VALID_RIDS_TTL = 300  # 5 minutes


def _validate_restaurant_id(rid):
    """Check that a Restaurant_ID exists in the restaurants table. Raises ValueError if not."""
    if not rid:
        raise ValueError("[GUARD] Restaurant_ID is required for data tables")
    now = __import__("time").time()
    if now - _valid_rids_cache.get("_ts", 0) > _VALID_RIDS_TTL:
        try:
            restos = get_all("restaurants")
            _valid_rids_cache.clear()
            _valid_rids_cache["_ts"] = now
            for r in restos:
                slug = r.get("Restaurant_ID", "")
                if slug:
                    _valid_rids_cache[slug] = True
        except Exception:
            pass
    if rid not in _valid_rids_cache:
        raise ValueError(f"[GUARD] Restaurant_ID '{rid}' does not exist in restaurants table")


# ── API singleton ───────────────────────────────────────────────────────
_api = None

def _get_api():
    global _api
    if _api is None:
        _api = Api(AIRTABLE_TOKEN)
    return _api

def _table(name):
    """Return a pyairtable Table object."""
    return _get_api().table(AIRTABLE_BASE, TABLES[name])

# ── Date helpers ────────────────────────────────────────────────────────
def _to_iso(d):
    """Convert various date formats to ISO 'YYYY-MM-DD' for Airtable."""
    if not d:
        return None
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y-%m-%d")
    # Already ISO
    if len(d) == 10 and d[4] == '-':
        return d
    return d

def _from_iso(d):
    """Airtable returns 'YYYY-MM-DD'. Keep as string."""
    return d if d else ""

# ── Record helpers ──────────────────────────────────────────────────────
def _rec_to_dict(rec, include_id=True):
    """Convert Airtable record {id, fields} to flat dict with 'id' field."""
    d = dict(rec["fields"])
    if include_id:
        d["id"] = rec["id"]
    return d

def _recs_to_list(recs):
    return [_rec_to_dict(r) for r in recs]

# ── Rate limiter (5 req/s) ──────────────────────────────────────────────
_last_call = 0.0

def _rate_limit():
    global _last_call
    now = time.time()
    elapsed = now - _last_call
    if elapsed < 0.22:  # ~4.5 req/s to stay safe
        time.sleep(0.22 - elapsed)
    _last_call = time.time()

# ── Cache layer: Redis (primary) + in-memory (fallback) ──────────────
# TTL tiers (seconds)
_CACHE_TTL_SLOW = 300   # 5 min — employes, fournisseurs, fiches, postes, shifts, categories, settings
_CACHE_TTL_DEFAULT = 300 # 5 min — planning, conges, evenements
_CACHE_TTL_FAST = 60    # 1 min — ca_jour, depenses, messages (temps reel)

_SLOW_TABLES = {"employes", "fournisseurs", "fiches", "postes", "shifts",
                "categories_fiches", "settings", "fermetures", "restaurants", "ingredients"}
_FAST_TABLES = {"ca_jour", "depenses", "messages"}

# Redis key format: "pauco:{restaurant_id}:{table_name}:{hash}"
_REDIS_PREFIX = os.getenv("REDIS_PREFIX", "pauco")
_redis = None
_redis_checked = False


def _get_redis():
    """Lazy-connect to Redis. Returns client or None."""
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    if _redis_lib is None:
        print("[CACHE] Redis library not installed — using in-memory fallback")
        return None
    # Railway provides REDIS_URL, REDIS_PRIVATE_URL, or REDIS_PUBLIC_URL
    url = (os.getenv("REDIS_URL") or os.getenv("REDIS_PRIVATE_URL")
           or os.getenv("REDIS_PUBLIC_URL") or "")
    if not url:
        print("[CACHE] No REDIS_URL found — using in-memory fallback")
        return None
    try:
        _redis = _redis_lib.from_url(url, decode_responses=True, socket_timeout=2,
                                     socket_connect_timeout=2, retry_on_timeout=True)
        _redis.ping()
        print(f"[CACHE] Redis connected ({url[:25]}...)")
    except Exception as e:
        print(f"[CACHE] Redis connection failed ({e}) — using in-memory fallback")
        _redis = None
    return _redis


# In-memory fallback (unchanged from before)
_mem_cache = {}
_mem_lock = threading.Lock()


def _cache_ttl(table_name):
    if table_name in _SLOW_TABLES:
        return _CACHE_TTL_SLOW
    if table_name in _FAST_TABLES:
        return _CACHE_TTL_FAST
    return _CACHE_TTL_DEFAULT


def _cache_key(table_name, restaurant_id, extra=""):
    """Build a Redis key or in-memory key."""
    return f"{_REDIS_PREFIX}:{restaurant_id or '_'}:{table_name}:{extra}"


def _cache_get(key):
    """Try Redis first, then in-memory."""
    r = _get_redis()
    if r:
        try:
            raw = r.get(key)
            if raw is not None:
                return _json.loads(raw)
        except Exception:
            pass
    # In-memory fallback
    with _mem_lock:
        entry = _mem_cache.get(key)
        if entry and time.time() < entry[1]:
            return entry[0]
        _mem_cache.pop(key, None)
    return None


def _cache_set(key, data, ttl):
    """Write to Redis and in-memory."""
    r = _get_redis()
    if r:
        try:
            r.setex(key, ttl, _json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            pass
    with _mem_lock:
        _mem_cache[key] = (data, time.time() + ttl)


def invalidate_cache(table_name=None, restaurant_id=None):
    """Invalidate cache entries by table and/or restaurant."""
    r = _get_redis()
    if r:
        try:
            if table_name is None:
                # Flush all pauco keys
                for key in r.scan_iter(f"{_REDIS_PREFIX}:*"):
                    r.delete(key)
            elif restaurant_id:
                for key in r.scan_iter(f"{_REDIS_PREFIX}:{restaurant_id}:{table_name}:*"):
                    r.delete(key)
            else:
                for key in r.scan_iter(f"{_REDIS_PREFIX}:*:{table_name}:*"):
                    r.delete(key)
        except Exception:
            pass
    # Also clear in-memory
    with _mem_lock:
        if table_name is None:
            _mem_cache.clear()
        else:
            keys_to_del = [k for k in _mem_cache
                           if f":{table_name}:" in k and (restaurant_id is None or f":{restaurant_id}:" in k)]
            for k in keys_to_del:
                del _mem_cache[k]


# ══════════════════════════════════════════════════════════════════════════
#  GENERIC CRUD
# ══════════════════════════════════════════════════════════════════════════

def get_all(table_name, restaurant_id=None, formula=None, sort=None):
    """Get all records from a table, optionally filtered by restaurant_id. Cached by TTL tier."""
    key = _cache_key(table_name, restaurant_id, f"all:{formula or ''}:{sort or ''}")
    cached = _cache_get(key)
    if cached is not None:
        return cached

    _rate_limit()
    t = _table(table_name)
    formulas = []
    if restaurant_id:
        formulas.append(f"{{Restaurant_ID}}='{restaurant_id}'")
    if formula:
        formulas.append(formula)

    f = None
    if len(formulas) == 1:
        f = formulas[0]
    elif len(formulas) > 1:
        f = "AND(" + ",".join(formulas) + ")"

    kwargs = {}
    if f:
        kwargs["formula"] = f
    if sort:
        kwargs["sort"] = sort

    recs = t.all(**kwargs)
    result = _recs_to_list(recs)
    _cache_set(key, result, _cache_ttl(table_name))
    return result


def get_one(table_name, record_id):
    """Get a single record by Airtable record ID."""
    _rate_limit()
    t = _table(table_name)
    rec = t.get(record_id)
    return _rec_to_dict(rec)


# ── Phone normalisation ────────────────────────────────────────────────
# Champs Airtable contenant un numéro de téléphone (à normaliser au format FR)
_PHONE_FIELDS = frozenset({
    "Téléphone", "Telephone", "Telephone_commercial", "Phone",
    "Tel", "Tel_commercial", "Téléphone_commercial",
})


def normaliser_telephone(tel):
    """Normalise un numéro français au format 'XX XX XX XX XX'.
    Accepte (078) 347-0657, +33783470657, 0783470657, 07.83.47.06.57, etc."""
    if not tel or not isinstance(tel, str):
        return tel
    chiffres = re.sub(r"\D", "", tel)
    if chiffres.startswith("33") and len(chiffres) == 11:
        chiffres = "0" + chiffres[2:]
    if chiffres.startswith("0033") and len(chiffres) == 13:
        chiffres = "0" + chiffres[4:]
    if len(chiffres) == 9 and not chiffres.startswith("0"):
        chiffres = "0" + chiffres
    if len(chiffres) == 10 and chiffres.startswith("0"):
        return " ".join(chiffres[i:i+2] for i in range(0, 10, 2))
    return tel


def _normalize_phones_in_fields(fields):
    if not isinstance(fields, dict):
        return fields
    for k in list(fields.keys()):
        if k in _PHONE_FIELDS and isinstance(fields[k], str) and fields[k]:
            fields[k] = normaliser_telephone(fields[k])
    return fields


def create(table_name, fields):
    """Create a record. Returns the new record as dict with 'id'.
    Validates Restaurant_ID for data tables."""
    clean = {k: v for k, v in fields.items() if v is not None}
    _normalize_phones_in_fields(clean)
    if table_name in _TABLES_WITH_RESTAURANT_ID:
        _validate_restaurant_id(clean.get("Restaurant_ID", ""))
    _rate_limit()
    t = _table(table_name)
    rec = t.create(clean, typecast=True)
    invalidate_cache(table_name, clean.get("Restaurant_ID"))
    return _rec_to_dict(rec)


def update(table_name, record_id, fields):
    """Update a record by Airtable record ID."""
    _rate_limit()
    t = _table(table_name)
    clean = {k: v for k, v in fields.items() if v is not None}
    _normalize_phones_in_fields(clean)
    rec = t.update(record_id, clean, typecast=True)
    invalidate_cache(table_name)
    return _rec_to_dict(rec)


def delete(table_name, record_id):
    """Delete a record by Airtable record ID. Blocked on protected tables."""
    if table_name in _AIRTABLE_NO_DELETE:
        raise RuntimeError(f"[GUARD] BLOCKED: delete on protected Airtable table '{table_name}'")
    _rate_limit()
    t = _table(table_name)
    t.delete(record_id)
    invalidate_cache(table_name)


def batch_create(table_name, records_fields):
    """Create multiple records (batched by 10). Validates Restaurant_ID for data tables."""
    if table_name in _TABLES_WITH_RESTAURANT_ID and records_fields:
        for f in records_fields:
            _validate_restaurant_id((f or {}).get("Restaurant_ID", ""))
    t = _table(table_name)
    results = []
    for i in range(0, len(records_fields), 10):
        _rate_limit()
        batch = records_fields[i:i+10]
        clean_batch = [{k: v for k, v in f.items() if v is not None} for f in batch]
        recs = t.batch_create(clean_batch, typecast=True)
        results.extend(_recs_to_list(recs))
    invalidate_cache(table_name)
    return results


def batch_delete(table_name, record_ids):
    """Delete multiple records (batched by 10). Blocked on protected tables."""
    if table_name in _AIRTABLE_NO_DELETE:
        raise RuntimeError(f"[GUARD] BLOCKED: batch_delete on protected Airtable table '{table_name}'")
    t = _table(table_name)
    for i in range(0, len(record_ids), 10):
        _rate_limit()
        batch = record_ids[i:i+10]
        t.batch_delete(batch)
    invalidate_cache(table_name)


def find_first(table_name, formula):
    """Find first record matching formula. Returns dict or None. Cached."""
    key = _cache_key(table_name, "", f"first:{formula}")
    cached = _cache_get(key)
    if cached is not None:
        return cached if cached != "__NONE__" else None

    _rate_limit()
    t = _table(table_name)
    recs = t.all(formula=formula, max_records=1)
    result = _rec_to_dict(recs[0]) if recs else None
    _cache_set(key, result if result is not None else "__NONE__", _cache_ttl(table_name))
    return result


def find_first_nocache(table_name, formula):
    """Find first record matching formula. NEVER cached — for auth-critical lookups."""
    _rate_limit()
    t = _table(table_name)
    recs = t.all(formula=formula, max_records=1)
    return _rec_to_dict(recs[0]) if recs else None


def count(table_name, restaurant_id=None, formula=None):
    """Count records matching criteria."""
    recs = get_all(table_name, restaurant_id, formula)
    return len(recs)


# ══════════════════════════════════════════════════════════════════════════
#  RESTAURANTS (App Clients)
# ══════════════════════════════════════════════════════════════════════════

def _resolve_restaurant(restaurant_id):
    """Resolve a friendly Restaurant_ID (e.g. 'bistrot_du_port') to the Airtable record.
    If restaurant_id starts with 'rec', try direct lookup first (legacy support)."""
    if restaurant_id.startswith("rec"):
        try:
            return get_one("restaurants", restaurant_id)
        except Exception:
            pass
    rec = find_first("restaurants", f"{{Restaurant_ID}}='{restaurant_id}'")
    return rec


def get_restaurant(restaurant_id):
    """Get restaurant by Restaurant_ID (friendly slug or legacy Airtable rec ID)."""
    return _resolve_restaurant(restaurant_id)


def _make_restaurant_slug(prenom, ville):
    """Generate a human-readable Restaurant_ID slug: resto_{prenom}_{ville}.
    Accents removed, spaces replaced by dashes, all lowercase."""
    import unicodedata, re
    def _slug(s):
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        s = s.lower().strip()
        s = re.sub(r"[^a-z0-9]+", "_", s)
        return s.strip("_")
    return f"resto_{_slug(prenom)}_{_slug(ville)}"


def create_restaurant(nom, email="", ville="", adresse="", lat=None, lng=None,
                      gerant_nom="", gerant_prenom="", telephone="",
                      restaurant_id=None):
    """Create a restaurant. Geocodes ville if lat/lng not provided."""
    if not restaurant_id and gerant_prenom and ville:
        restaurant_id = _make_restaurant_slug(gerant_prenom, ville)
    # Geocode ville if no coordinates
    if (not lat or not lng) and ville:
        try:
            from modules.meteo import geocode_ville
            coords = geocode_ville(ville)
            if coords:
                lat, lng = coords
        except Exception:
            pass
    if not lat:
        lat = 48.8566
    if not lng:
        lng = 2.3522
    # Normalize phone (format FR — appliqué aussi par create() en filet de sécurité)
    if telephone:
        telephone = normaliser_telephone(telephone)
    fields = {
        "Nom": nom, "Email": email, "Ville": ville, "Adresse": adresse,
        "Lat": lat, "Lng": lng, "Gerant_nom": gerant_nom,
        "Gerant_prenom": gerant_prenom, "Téléphone": telephone,
        "Actif": True
    }
    if restaurant_id:
        fields["Restaurant_ID"] = restaurant_id
    rec = create("restaurants", fields)
    # Force-invalidate the full restaurants list cache (create() only invalidates
    # the restaurant_id-scoped key, but admin dashboard fetches without restaurant_id)
    invalidate_cache("restaurants")
    print(f"[AIRTABLE] Restaurant créé: {rec}")
    if restaurant_id:
        rec["Restaurant_ID"] = restaurant_id
    return rec


_RESTAURANT_FIELD_MAP = {
    "nom": "Nom", "email": "Email", "ville": "Ville", "adresse": "Adresse",
    "lat": "Lat", "lng": "Lng", "gerant_nom": "Gerant_nom",
    "gerant_prenom": "Gerant_prenom", "telephone": "Téléphone",
    "site_web": "Site_web", "type_etablissement": "Type_etablissement",
    "capacite_interieur": "Capacite_interieur", "capacite_terrasse": "Capacite_terrasse",
    "horaires_ouverture": "Horaires_ouverture", "fermeture_hebdo": "Fermeture_hebdo",
    "fermeture_annuelle": "Fermeture_annuelle", "siret": "SIRET",
    "tva_intra": "TVA_intra", "url_google": "URL_Google",
    "url_tripadvisor": "URL_TripAdvisor", "url_booking": "URL_Booking",
}


def update_restaurant(restaurant_id, **fields):
    """Update restaurant by Restaurant_ID (friendly slug or legacy rec ID).
    Creates the record if it doesn't exist yet."""
    resto = _resolve_restaurant(restaurant_id)
    at_fields = {_RESTAURANT_FIELD_MAP.get(k, k): v for k, v in fields.items()}
    if not resto:
        # Restaurant not found — create it
        at_fields["Restaurant_ID"] = restaurant_id
        invalidate_cache("restaurants")
        return create("restaurants", at_fields)
    airtable_id = resto["id"]
    return update("restaurants", airtable_id, at_fields)


# ══════════════════════════════════════════════════════════════════════════
#  CA JOURNALIER
# ══════════════════════════════════════════════════════════════════════════

def get_ca_jour(restaurant_id, mois=None, date_str=None):
    """Get CA records. mois='2026-03', date_str='2026-03-15'."""
    formulas = []
    if date_str:
        formulas.append(f"{{Date}}='{_to_iso(date_str)}'")
    elif mois:
        # Airtable date filter: YEAR + MONTH
        y, m = mois.split("-")
        formulas.append(f"YEAR({{Date}})={y}")
        formulas.append(f"MONTH({{Date}})={int(m)}")

    f = None
    if formulas:
        f = "AND(" + ",".join(formulas) + ")" if len(formulas) > 1 else formulas[0]

    return get_all("ca_jour", restaurant_id, formula=f, sort=["Date"])


def upsert_ca_jour(restaurant_id, date_str, ca, ca_restaurant, ca_bar,
                   couverts_midi, couverts_soir, ticket_moyen, commentaire=""):
    """Insert or update CA for a specific date."""
    # DATESTR() obligatoire : Airtable stocke les Date en ISO datetime
    existing = find_first_nocache("ca_jour",
        f"AND({{Restaurant_ID}}='{restaurant_id}',DATESTR({{Date}})='{_to_iso(date_str)}')")

    fields = {
        "Restaurant_ID": restaurant_id,
        "Date": _to_iso(date_str),
        "CA_Total": float(ca),
        "CA_Restaurant": float(ca_restaurant),
        "CA_Bar": float(ca_bar),
        "Couverts_Midi": int(couverts_midi),
        "Couverts_Soir": int(couverts_soir),
        "Ticket_Moyen": float(ticket_moyen),
        "Commentaire": commentaire or ""
    }

    if existing:
        return update("ca_jour", existing["id"], fields)
    return create("ca_jour", fields)


def get_ca_sum(restaurant_id, mois):
    """Sum CA for a month."""
    rows = get_ca_jour(restaurant_id, mois=mois)
    return sum(r.get("CA_Total", 0) or 0 for r in rows)


def get_ca_record(restaurant_id, mois):
    """Get best day CA for month."""
    rows = get_ca_jour(restaurant_id, mois=mois)
    if not rows:
        return None
    return max(rows, key=lambda r: r.get("CA_Total", 0) or 0)


# ══════════════════════════════════════════════════════════════════════════
#  DEPENSES (fixes + variables combined)
# ══════════════════════════════════════════════════════════════════════════

def get_depenses(restaurant_id, mois=None, type_dep=None):
    """Get expenses. type_dep='Fixe' or 'Variable'."""
    formulas = []
    if mois:
        formulas.append(f"{{Mois}}='{mois}'")
    if type_dep:
        formulas.append(f"{{Type}}='{type_dep}'")
    f = "AND(" + ",".join(formulas) + ")" if len(formulas) > 1 else (formulas[0] if formulas else None)
    return get_all("depenses", restaurant_id, formula=f)


def get_depenses_fixes_recurrentes(restaurant_id):
    """Get all recurring fixed expenses."""
    return get_all("depenses", restaurant_id,
                   formula="AND({Type}='Fixe',{Recurrente}=TRUE())")


def sum_depenses(restaurant_id, mois, type_dep=None, categories=None):
    """Sum expenses for a month, optionally filtered by type/categories."""
    rows = get_depenses(restaurant_id, mois=mois, type_dep=type_dep)
    if categories:
        rows = [r for r in rows if r.get("Catégorie", r.get("Categorie", "")) in categories]
    return sum(r.get("Montant", 0) or 0 for r in rows)


def create_depense_fixe(restaurant_id, categorie, description, montant, mois, recurrente=False):
    return create("depenses", {
        "Restaurant_ID": restaurant_id,
        "Catégorie": categorie,
        "Description": description,
        "Montant": float(montant),
        "Mois": mois,
        "Type": "Fixe",
        "Recurrente": bool(recurrente),
        "Desactivee_mois": ""
    })


def create_depense_variable(restaurant_id, date_str, categorie, description, montant, mois):
    return create("depenses", {
        "Restaurant_ID": restaurant_id,
        "Date": _to_iso(date_str),
        "Catégorie": categorie,
        "Description": description,
        "Montant": float(montant),
        "Mois": mois,
        "Type": "Variable",
    })


def depenses_mois_total(restaurant_id, mois):
    """Calculate total expenses for a month including recurring fixes."""
    # Direct expenses for this month
    rows = get_depenses(restaurant_id, mois=mois)
    total = sum(r.get("Montant", 0) or 0 for r in rows)

    # Add recurring fixed expenses from previous months
    recurrentes = get_depenses_fixes_recurrentes(restaurant_id)
    for r in recurrentes:
        r_mois = r.get("Mois", "")
        if r_mois and r_mois < mois:
            desac = r.get("Desactivee_mois", "") or ""
            if mois not in desac:
                total += r.get("Montant", 0) or 0

    return total


# ══════════════════════════════════════════════════════════════════════════
#  EMPLOYES
# ══════════════════════════════════════════════════════════════════════════

def get_employes(restaurant_id, actif_only=True, include_archived=False):
    """Default: only active (Actif=TRUE) employees.
    Pass include_archived=True ou actif_only=False pour tout récupérer (sync, archives tab)."""
    formulas = []
    if actif_only and not include_archived:
        formulas.append("{Actif}=TRUE()")
    f = "AND(" + ",".join(formulas) + ")" if len(formulas) > 1 else (formulas[0] if formulas else None)
    emps = get_all("employes", restaurant_id, formula=f, sort=["Ordre", "Nom"])
    return emps


def create_employe(restaurant_id, prenom, nom, poste, type_contrat="CDI",
                   date_debut="", salaire_brut=0, heures_semaine=35, phone="",
                   pole="Salle", date_fin=None, date_naissance=None,
                   email="", adresse="", numero_secu="", iban=""):
    fields = {
        "Restaurant_ID": restaurant_id,
        "Prénom": prenom,
        "Nom": nom,
        "Poste": poste,
        "Pôle": pole,
        "Type_contrat": type_contrat,
        "Date_entrée": _to_iso(date_debut) if date_debut else None,
        "Salaire_brut": float(salaire_brut),
        "Heures_semaine": float(heures_semaine),
        "Téléphone": phone,
        "Actif": True,
        "Ordre": 0
    }
    if date_fin:
        fields["Date_fin"] = _to_iso(date_fin)
    if date_naissance:
        fields["Date_naissance"] = _to_iso(date_naissance)
    if email:
        fields["Email"] = email
    if adresse:
        fields["Adresse"] = adresse
    if numero_secu:
        fields["Numero_secu"] = numero_secu
    if iban:
        fields["IBAN"] = iban
    return create("employes", fields)


def find_employe(restaurant_id, prenom, nom):
    """Find Airtable employe record by prenom+nom for a restaurant."""
    from .airtable_client import find_first as _ff  # self-import safe
    return find_first("employes",
        f"AND({{Restaurant_ID}}='{restaurant_id}',{{Prénom}}='{prenom}',{{Nom}}='{nom}')")


def get_employe_documents(restaurant_id, prenom, nom):
    """Read employe documents JSON list from Airtable Documents long-text field.
    Returns [] on missing record / missing field / parse error."""
    try:
        rec = find_employe(restaurant_id, prenom, nom)
        if not rec:
            return []
        raw = rec.get("Documents", "") or ""
        if not raw:
            return []
        data = _json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"[AIRTABLE] get_employe_documents error: {e}")
        return []


def set_employe_documents(restaurant_id, prenom, nom, documents):
    """Persist documents JSON list to Airtable Documents field on employes record.
    Best-effort: silently ignored if the field doesn't exist on the table."""
    try:
        rec = find_employe(restaurant_id, prenom, nom)
        if not rec:
            print(f"[AIRTABLE] set_employe_documents: employe {prenom} {nom} not found")
            return False
        update("employes", rec["id"], {"Documents": _json.dumps(documents, ensure_ascii=False)})
        invalidate_cache("employes")
        return True
    except Exception as e:
        print(f"[AIRTABLE] set_employe_documents error: {e}")
        return False


def update_employe(record_id, **kwargs):
    mapping = {
        "prenom": "Prénom", "nom": "Nom", "poste": "Poste", "pole": "Pôle",
        "type_contrat": "Type_contrat", "date_debut": "Date_entrée",
        "date_fin": "Date_fin", "salaire_brut": "Salaire_brut",
        "heures_semaine": "Heures_semaine", "phone": "Téléphone",
        "statut": "Actif", "ordre": "Ordre", "date_naissance": "Date_naissance",
        "email": "Email", "adresse": "Adresse",
        "numero_secu": "Numero_secu", "iban": "IBAN",
        "cp_acquis": "CP_acquis", "solde_initial": "Solde_Initial",
        "date_debut_compteur": "Date_Debut_Compteur",
    }
    fields = {}
    for k, v in kwargs.items():
        at_key = mapping.get(k, k)
        if k == "statut":
            fields["Actif"] = (v == "Actif")
        elif k in ("date_debut", "date_fin", "date_naissance", "date_debut_compteur"):
            fields[at_key] = _to_iso(v) if v else None
        elif k in ("salaire_brut", "heures_semaine", "cp_acquis", "solde_initial"):
            fields[at_key] = float(v)
        elif k == "ordre":
            fields[at_key] = int(v)
        else:
            fields[at_key] = v
    return update("employes", record_id, fields)


# ══════════════════════════════════════════════════════════════════════════
#  PLANNING
# ══════════════════════════════════════════════════════════════════════════

def get_planning(restaurant_id, date_debut=None, date_fin=None, employe_id=None):
    """Get planning entries for a date range."""
    formulas = []
    if date_debut:
        formulas.append(f"{{Date}}>='{_to_iso(date_debut)}'")
    if date_fin:
        formulas.append(f"{{Date}}<='{_to_iso(date_fin)}'")
    if employe_id:
        formulas.append(f"{{Employé_ID}}='{employe_id}'")
    f = "AND(" + ",".join(formulas) + ")" if len(formulas) > 1 else (formulas[0] if formulas else None)
    return get_all("planning", restaurant_id, formula=f, sort=["Date", "Shift_début"])


def create_planning_entry(restaurant_id, employe_id, date_str, heure_debut, heure_fin,
                          poste="Salle", shift_id="", shift_nom="", shift_couleur="",
                          repas=False, duree_repas=0):
    return create("planning", {
        "Restaurant_ID": restaurant_id,
        "Employé_ID": employe_id,
        "Date": _to_iso(date_str),
        "Shift_début": heure_debut,
        "Shift_fin": heure_fin,
        "Poste": poste,
        "Shift_ID": shift_id or "",
        "Shift_nom": shift_nom or "",
        "Shift_couleur": shift_couleur or "",
        "Repas": bool(repas),
        "Duree Repas": int(duree_repas or 0),
    })


def delete_planning_by_emp_date(restaurant_id, employe_id, date_str):
    """Delete all planning entries for an employee on a specific date."""
    rows = get_all("planning", restaurant_id,
        formula=f"AND({{Employé_ID}}='{employe_id}',{{Date}}='{_to_iso(date_str)}')")
    for r in rows:
        delete("planning", r["id"])


# ══════════════════════════════════════════════════════════════════════════
#  FICHES TECHNIQUES (food + cocktails + bar unified)
# ══════════════════════════════════════════════════════════════════════════

def get_fiches(restaurant_id, fiche_type=None):
    """Get fiches. fiche_type='Food','Cocktail','Boisson'."""
    f = None
    if fiche_type:
        f = f"{{Type}}='{fiche_type}'"
    return get_all("fiches", restaurant_id, formula=f, sort=["Categorie", "Nom"])


def create_fiche(restaurant_id, fiche_type, nom, categorie, prix_vente_ht=0,
                 prix_vente_ttc=0, cout_ht=0, coefficient=0, ratio_mp=0, marge_ht=0,
                 nb_portions=0, temps_prepa=0, volume_cl=0, contenance_cl=0,
                 prix_achat_ht=0, cout_ht_cl=0, perte_casse=0, perte_degustation=0,
                 perte_evaporation=0, cout_ajuste=0, ingredients_json=""):
    fields = {
        "Restaurant_ID": restaurant_id,
        "Nom": nom,
        "Type": fiche_type,
        "Categorie": categorie,
        "Prix_vente_ht": float(prix_vente_ht),
        "Prix_vente_ttc": float(prix_vente_ttc),
        "Cout_ht": float(cout_ht),
        "Coefficient": float(coefficient),
        "Ratio_mp": float(ratio_mp),
        "Marge_ht": float(marge_ht),
        "Statut": "actif"
    }
    if fiche_type == "Food":
        fields["Nb_portions"] = int(nb_portions)
        fields["Temps_prépa"] = int(temps_prepa)
    elif fiche_type == "Cocktail":
        fields["Volume_cl"] = float(volume_cl)
    elif fiche_type == "Boisson":
        fields["Contenance_cl"] = float(contenance_cl)
        fields["Prix_achat_ht"] = float(prix_achat_ht)
        fields["Cout_ht_cl"] = float(cout_ht_cl)
        fields["Perte_casse"] = float(perte_casse)
        fields["Perte_degustation"] = float(perte_degustation)
        fields["Perte_evaporation"] = float(perte_evaporation)
        fields["Cout_ajuste"] = float(cout_ajuste)
    if ingredients_json:
        fields["Ingredients"] = ingredients_json
    return create("fiches", fields)


def upsert_fiche(restaurant_id, fiche_type, nom, **kwargs):
    """Find existing fiche by (Restaurant_ID, Type, Nom). Update if found, else create."""
    nom_esc = (nom or "").replace("'", "\\'")
    rec = find_first("fiches",
        f"AND({{Restaurant_ID}}='{restaurant_id}',{{Type}}='{fiche_type}',{{Nom}}='{nom_esc}')")
    if rec:
        # Build same fields dict as create_fiche
        kwargs.setdefault("categorie", "")
        # Reuse create_fiche logic by calling it on a dummy then extracting? Simpler: inline minimal.
        fields = {
            "Restaurant_ID": restaurant_id, "Nom": nom, "Type": fiche_type,
            "Categorie": kwargs.get("categorie", ""),
            "Prix_vente_ht": float(kwargs.get("prix_vente_ht", 0) or 0),
            "Prix_vente_ttc": float(kwargs.get("prix_vente_ttc", 0) or 0),
            "Cout_ht": float(kwargs.get("cout_ht", 0) or 0),
            "Coefficient": float(kwargs.get("coefficient", 0) or 0),
            "Ratio_mp": float(kwargs.get("ratio_mp", 0) or 0),
            "Marge_ht": float(kwargs.get("marge_ht", 0) or 0),
            "Statut": kwargs.get("statut", "actif"),
        }
        if fiche_type == "Food":
            fields["Nb_portions"] = int(kwargs.get("nb_portions", 0) or 0)
            fields["Temps_prépa"] = int(kwargs.get("temps_prepa", 0) or 0)
        elif fiche_type == "Cocktail":
            fields["Volume_cl"] = float(kwargs.get("volume_cl", 0) or 0)
        elif fiche_type == "Boisson":
            fields["Contenance_cl"] = float(kwargs.get("contenance_cl", 0) or 0)
            fields["Prix_achat_ht"] = float(kwargs.get("prix_achat_ht", 0) or 0)
            fields["Cout_ht_cl"] = float(kwargs.get("cout_ht_cl", 0) or 0)
            fields["Perte_casse"] = float(kwargs.get("perte_casse", 0) or 0)
            fields["Perte_degustation"] = float(kwargs.get("perte_degustation", 0) or 0)
            fields["Perte_evaporation"] = float(kwargs.get("perte_evaporation", 0) or 0)
            fields["Cout_ajuste"] = float(kwargs.get("cout_ajuste", 0) or 0)
        if kwargs.get("ingredients_json"):
            fields["Ingredients"] = kwargs["ingredients_json"]
        update("fiches", rec["id"], fields)
        invalidate_cache("fiches")
        return {"id": rec["id"], "fields": fields}
    return create_fiche(restaurant_id, fiche_type, nom, **kwargs)


def update_fiche(record_id, **kwargs):
    return update("fiches", record_id, kwargs)


# ── Ingredients (shared for food + cocktails) ───────────────────────────

def get_ingredients(fiche_id):
    """Get ingredients for a fiche (by Airtable record ID)."""
    return get_all("ingredients", formula=f"{{Fiche_ID}}='{fiche_id}'")


def set_ingredients(fiche_id, fiche_type, ingredients_list):
    """Replace all ingredients for a fiche."""
    # Delete existing
    existing = get_ingredients(fiche_id)
    if existing:
        batch_delete("ingredients", [r["id"] for r in existing])

    # Create new
    records = []
    for ing in ingredients_list:
        fields = {"Fiche_ID": fiche_id, "Fiche_Type": fiche_type}
        fields["Produit"] = ing.get("produit", "")
        if fiche_type == "Food":
            fields["Prix_kilo"] = float(ing.get("prix_kilo", 0))
            fields["Grammes"] = float(ing.get("grammes", 0))
            fields["Cout_portion"] = float(ing.get("cout_portion", 0))
        else:  # Cocktail
            fields["Cout_ht_unitaire"] = float(ing.get("cout_ht_unitaire", 0))
            fields["Qte_bouteille_cl"] = float(ing.get("qte_bouteille_cl", 0))
            fields["Cout_ht_cl"] = float(ing.get("cout_ht_cl", 0))
            fields["Qte_utilisee_cl"] = float(ing.get("qte_utilisee_cl", 0))
            fields["Cout_ht_verre"] = float(ing.get("cout_ht_verre", 0))
        records.append(fields)

    if records:
        batch_create("ingredients", records)


# ══════════════════════════════════════════════════════════════════════════
#  FOURNISSEURS
# ══════════════════════════════════════════════════════════════════════════

def get_fournisseurs(restaurant_id):
    return get_all("fournisseurs", restaurant_id, sort=["Nom"])


def create_fournisseur(restaurant_id, nom, type_f="Autre", telephone="", email="",
                       nom_commercial="", telephone_commercial="", email_commercial="",
                       site_web="", adresse="", notes=""):
    return create("fournisseurs", {
        "Restaurant_ID": restaurant_id,
        "Nom": nom, "Type": type_f, "Telephone": telephone, "Email": email,
        "Nom_commercial": nom_commercial, "Telephone_commercial": telephone_commercial,
        "Email_commercial": email_commercial, "Site_web": site_web,
        "Adresse": adresse, "Notes": notes
    })


def update_fournisseur(record_id, **kwargs):
    mapping = {
        "nom": "Nom", "type": "Type", "telephone": "Telephone", "email": "Email",
        "nom_commercial": "Nom_commercial", "telephone_commercial": "Telephone_commercial",
        "email_commercial": "Email_commercial", "site_web": "Site_web",
        "adresse": "Adresse", "notes": "Notes"
    }
    fields = {mapping.get(k, k): v for k, v in kwargs.items()}
    return update("fournisseurs", record_id, fields)


# ══════════════════════════════════════════════════════════════════════════
#  CONGES
# ══════════════════════════════════════════════════════════════════════════

def get_conges(restaurant_id, employe_id=None, date_debut=None, date_fin=None):
    formulas = []
    if employe_id:
        formulas.append(f"{{Employe_ID}}='{employe_id}'")
    if date_debut:
        formulas.append(f"{{Date_fin}}>='{_to_iso(date_debut)}'")
    if date_fin:
        formulas.append(f"{{Date_debut}}<='{_to_iso(date_fin)}'")
    f = "AND(" + ",".join(formulas) + ")" if len(formulas) > 1 else (formulas[0] if formulas else None)
    return get_all("conges", restaurant_id, formula=f)


def create_conge(restaurant_id, employe_id, type_c, date_debut, date_fin, commentaire=""):
    return create("conges", {
        "Restaurant_ID": restaurant_id,
        "Employe_ID": employe_id,
        "Type": type_c,
        "Date_debut": _to_iso(date_debut),
        "Date_fin": _to_iso(date_fin),
        "Commentaire": commentaire or ""
    })


def sum_conges_jours(restaurant_id, employe_id):
    """Total leave days for an employee."""
    rows = get_conges(restaurant_id, employe_id=employe_id)
    total = 0
    for r in rows:
        d1 = r.get("Date_debut", "")
        d2 = r.get("Date_fin", "")
        if d1 and d2:
            try:
                dt1 = datetime.strptime(d1, "%Y-%m-%d")
                dt2 = datetime.strptime(d2, "%Y-%m-%d")
                total += (dt2 - dt1).days + 1
            except ValueError:
                pass
    return total


# ══════════════════════════════════════════════════════════════════════════
#  EVENEMENTS
# ══════════════════════════════════════════════════════════════════════════

def get_evenements(restaurant_id, mois=None):
    f = None
    if mois:
        y, m = mois.split("-")
        f = f"AND(YEAR({{Date}})={y},MONTH({{Date}})={int(m)})"
    return get_all("evenements", restaurant_id, formula=f, sort=["Date"])


def create_evenement(restaurant_id, titre, date_str, couleur="#2D6A4A", note="", description=""):
    return create("evenements", {
        "Restaurant_ID": restaurant_id,
        "Titre": titre,
        "Date": _to_iso(date_str),
        "Couleur": couleur,
        "Note": note or "",
        "Description": description or ""
    })


# ══════════════════════════════════════════════════════════════════════════
#  FERMETURES
# ══════════════════════════════════════════════════════════════════════════

def get_fermetures(restaurant_id):
    return get_all("fermetures", restaurant_id)


def is_ferme(restaurant_id, date_str):
    """Check if restaurant is closed on a given date.
    Exceptions (Jour_semaine=-2) override recurring closures for that date."""
    fermetures = get_fermetures(restaurant_id)
    iso = _to_iso(date_str)
    dt = datetime.strptime(iso, "%Y-%m-%d")
    jour_semaine = dt.weekday()  # 0=Monday

    # Check for exception (opens this specific date)
    for f in fermetures:
        if f.get("Date") == iso and f.get("Jour_semaine") == -2:
            return False

    for f in fermetures:
        if f.get("Recurrence") == 1 and f.get("Jour_semaine") == jour_semaine:
            return True
        if f.get("Date") == iso and f.get("Recurrence") == 0 and f.get("Jour_semaine") != -2:
            return True
    return False


def create_fermeture(restaurant_id, recurrence=False, jour_semaine=-1, date_str=""):
    return create("fermetures", {
        "Restaurant_ID": restaurant_id,
        "Recurrence": 1 if recurrence else 0,
        "Jour_semaine": jour_semaine,
        "Date": date_str or ""
    })


# ══════════════════════════════════════════════════════════════════════════
#  SHIFTS
# ══════════════════════════════════════════════════════════════════════════

def get_shifts(restaurant_id):
    return get_all("shifts", restaurant_id, sort=["Pole", "Nom"])


def create_shift(restaurant_id, nom, pole, heure_debut="", heure_fin="", couleur="#2D6A4A"):
    return create("shifts", {
        "Restaurant_ID": restaurant_id,
        "Nom": nom, "Pole": pole,
        "Heure_debut": heure_debut, "Heure_fin": heure_fin,
        "Couleur": couleur
    })


# ══════════════════════════════════════════════════════════════════════════
#  POSTES
# ══════════════════════════════════════════════════════════════════════════

def get_postes(restaurant_id):
    return get_all("postes", restaurant_id, sort=["Pole", "Nom"])


def create_poste(restaurant_id, nom, pole, is_default=0):
    return create("postes", {
        "Restaurant_ID": restaurant_id,
        "Nom": nom, "Pole": pole, "Is_default": int(is_default)
    })


# ══════════════════════════════════════════════════════════════════════════
#  CATEGORIES FICHES
# ══════════════════════════════════════════════════════════════════════════

def get_categories(restaurant_id, type_cat=None):
    f = f"{{Type}}='{type_cat}'" if type_cat else None
    return get_all("categories_fiches", restaurant_id, formula=f)


def create_categorie(restaurant_id, type_cat, nom, ordre=0):
    return create("categories_fiches", {
        "Restaurant_ID": restaurant_id,
        "Type": type_cat, "Nom": nom, "Ordre": ordre
    })


# ══════════════════════════════════════════════════════════════════════════
#  MESSAGES
# ══════════════════════════════════════════════════════════════════════════

def get_messages(restaurant_id, unread_only=False):
    f = "{Lu}=FALSE()" if unread_only else None
    return get_all("messages", restaurant_id, formula=f, sort=["Created_at"])


def count_unread(restaurant_id):
    msgs = get_messages(restaurant_id, unread_only=True)
    return len(msgs)


def create_message(restaurant_id, expediteur, destinataires, objet, message, date_str):
    return create("messages", {
        "Restaurant_ID": restaurant_id,
        "From_user": expediteur,
        "To_user": destinataires,
        "Subject": objet,
        "Body": message,
        "Created_at": _to_iso(date_str),
        "Lu": False
    })


def mark_read(record_id):
    return update("messages", record_id, {"Lu": True})


# ══════════════════════════════════════════════════════════════════════════
#  HISTORIQUE FICHES
# ══════════════════════════════════════════════════════════════════════════

def add_historique(fiche_id, fiche_type, champ, ancienne, nouvelle, date_str):
    return create("historique_fiches", {
        "Fiche_ID": fiche_id,
        "Fiche_Type": fiche_type,
        "Champ": champ,
        "Ancienne_valeur": str(ancienne),
        "Nouvelle_valeur": str(nouvelle),
        "Date_modification": date_str
    })


# ══════════════════════════════════════════════════════════════════════════
#  HELPER: compute depenses breakdown for a month
# ══════════════════════════════════════════════════════════════════════════

def depenses_mois_detail(restaurant_id, mois):
    """
    Returns dict with:
      total, food, bev, ops, personnel, fixes_total, variables_total,
      depenses_fixes (list), depenses_variables (list)
    """
    all_deps = get_depenses(restaurant_id, mois=mois)
    recurrentes = get_depenses_fixes_recurrentes(restaurant_id)

    # Add recurring from earlier months
    for r in recurrentes:
        r_mois = r.get("Mois", "")
        if r_mois and r_mois < mois:
            desac = r.get("Desactivee_mois", "") or ""
            if mois not in desac:
                all_deps.append(r)

    fixes = [d for d in all_deps if d.get("Type") == "Fixe"]
    variables = [d for d in all_deps if d.get("Type") == "Variable"]

    food_cats = {"Achats food", "Achats & Matières"}
    bev_cats = {"Achats boissons", "Achats bar"}
    ops_cats = {"Charges opérationnelles", "Charges fixes"}
    personnel_cats = {"Personnel", "Salaires bruts", "Charges patronales"}

    food = sum(d.get("Montant", 0) or 0 for d in all_deps
               if d.get("Catégorie", d.get("Categorie", "")) in food_cats)
    bev = sum(d.get("Montant", 0) or 0 for d in all_deps
              if d.get("Catégorie", d.get("Categorie", "")) in bev_cats)
    ops = sum(d.get("Montant", 0) or 0 for d in all_deps
              if d.get("Catégorie", d.get("Categorie", "")) in ops_cats)
    personnel = sum(d.get("Montant", 0) or 0 for d in all_deps
                    if d.get("Catégorie", d.get("Categorie", "")) in personnel_cats)

    total = sum(d.get("Montant", 0) or 0 for d in all_deps)

    return {
        "total": total,
        "food": food,
        "bev": bev,
        "ops": ops,
        "personnel": personnel,
        "fixes_total": sum(d.get("Montant", 0) or 0 for d in fixes),
        "variables_total": sum(d.get("Montant", 0) or 0 for d in variables),
        "depenses_fixes": fixes,
        "depenses_variables": variables,
    }


# ══════════════════════════════════════════════════════════════════════════
#  ALLERGENES
# ══════════════════════════════════════════════════════════════════════════

ALLERGENE_FIELDS = [
    "Gluten", "Crustaces", "Oeufs", "Poisson", "Arachides", "Soja", "Lait",
    "Fruits_a_coque", "Celeri", "Moutarde", "Sesame", "Sulfites", "Lupin", "Mollusques",
]


def get_allergenes(restaurant_id):
    return get_all("allergenes", restaurant_id, sort=["Categorie", "Nom_plat"])


def create_allergene(restaurant_id, nom_plat, categorie="Plat"):
    fields = {"Restaurant_ID": restaurant_id, "Nom_plat": nom_plat, "Categorie": categorie}
    for a in ALLERGENE_FIELDS:
        fields[a] = False
    return create("allergenes", fields)


def update_allergene(record_id, fields):
    return update("allergenes", record_id, fields)


def delete_allergene(record_id):
    return delete("allergenes", record_id)


# ══════════════════════════════════════════════════════════════════════════
#  ROLES
# ══════════════════════════════════════════════════════════════════════════

def get_roles(restaurant_id):
    return get_all("roles", restaurant_id, sort=["Nom"])


def create_role(restaurant_id, nom, permissions="", is_default=False):
    return create("roles", {
        "Restaurant_ID": restaurant_id,
        "Nom": nom,
        "Permissions": permissions,
        "Is_default": bool(is_default),
    })


def update_role(record_id, fields):
    return update("roles", record_id, fields)


def delete_role(record_id):
    return delete("roles", record_id)
