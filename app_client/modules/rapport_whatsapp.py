"""Rapport WhatsApp quotidien — envoyé chaque matin à 7h30 via Twilio.
Pour chaque restaurant actif (non démo) avec un numéro WhatsApp configuré.
"""

import os
import requests
from datetime import date, timedelta, datetime

from . import airtable_client as at
from .meteo import get_meteo

TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

MOIS_NOMS = ["janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
JOURS_NOMS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

# Vacances scolaires 2026 (métropole, dates approximatives)
VACANCES_2026 = [
    ("A", "2026-02-14", "2026-03-02"),
    ("B", "2026-02-07", "2026-02-23"),
    ("C", "2026-02-21", "2026-03-09"),
    ("A", "2026-04-11", "2026-04-27"),
    ("B", "2026-04-04", "2026-04-20"),
    ("C", "2026-04-18", "2026-05-04"),
]

JOURS_FERIES_2026 = [
    "2026-01-01", "2026-04-06", "2026-05-01", "2026-05-08",
    "2026-05-14", "2026-05-25", "2026-07-14", "2026-08-15",
    "2026-11-01", "2026-11-11", "2026-12-25",
]


def _check_vacances(d):
    """Retourne la zone de vacances si applicable, sinon None."""
    ds = d.isoformat()
    for zone, debut, fin in VACANCES_2026:
        if debut <= ds <= fin:
            return zone
    return None


def _check_ferie(d):
    return d.isoformat() in JOURS_FERIES_2026


def _format_date_fr(d):
    return f"{JOURS_NOMS[d.weekday()]} {d.day} {MOIS_NOMS[d.month - 1]}"


def _get_meteo_today(lat, lng):
    """Météo d'aujourd'hui via Open-Meteo."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lng}"
            f"&daily=temperature_2m_max,temperature_2m_min,weathercode"
            f"&timezone=Europe/Paris&forecast_days=1"
        )
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        daily = data.get("daily", {})
        if not daily or not daily.get("time"):
            return None
        from .meteo import _WMO_CODES, _WMO_ICONS
        code = daily["weathercode"][0]
        t_max = round(daily["temperature_2m_max"][0])
        return {
            "temp": t_max,
            "description": _WMO_CODES.get(code, "Inconnu"),
            "icon": _WMO_ICONS.get(code, "🌡️"),
        }
    except Exception:
        return None


def _get_planning_day(restaurant_id, date_iso):
    """Get planning for a single day using IS_SAME (Airtable date comparison)."""
    try:
        return at.get_all("planning", restaurant_id,
                          formula=f"IS_SAME({{Date}},'{date_iso}','day')",
                          sort=["Shift_début"])
    except Exception:
        return []


def _get_ca_day(restaurant_id, date_iso):
    """Get CA for a single day using IS_SAME."""
    try:
        recs = at.get_all("ca_jour", restaurant_id,
                          formula=f"IS_SAME({{Date}},'{date_iso}','day')")
        return recs[0] if recs else None
    except Exception:
        return None


def build_rapport(restaurant_id):
    """Construit le message WhatsApp pour un restaurant donné.
    Returns (message_text, error_or_none)."""
    try:
        resto = at.get_restaurant(restaurant_id)
    except Exception as e:
        return "", f"Restaurant introuvable: {e}"
    if not resto:
        return "", "Restaurant introuvable"

    gerant_prenom = resto.get("Gerant_prenom", "") or "Chef"
    lat = resto.get("Lat", 48.8566) or 48.8566
    lng = resto.get("Lng", 2.3522) or 2.3522

    today = date.today()
    hier = today - timedelta(days=1)
    mois_str = today.strftime("%Y-%m")

    lines = []

    # --- Header ---
    meteo = _get_meteo_today(lat, lng)
    meteo_icon = meteo["icon"] if meteo else "☀️"
    lines.append(f"Bonjour {gerant_prenom} {meteo_icon}")
    lines.append(f"{_format_date_fr(today)}")
    lines.append("")

    # --- Employés (chargés une seule fois) ---
    try:
        employes = at.get_employes(restaurant_id, actif_only=True)
        emp_map = {e["id"]: e for e in employes}
    except Exception:
        employes = []
        emp_map = {}

    # ═══════════════════════════════════════════════════════
    #  1. HIER — CA + couverts + TM + bar
    # ═══════════════════════════════════════════════════════
    rec_hier = _get_ca_day(restaurant_id, hier.isoformat())
    if rec_hier:
        ca_total = rec_hier.get("CA_Total", 0) or 0
        ca_bar = rec_hier.get("CA_Bar", 0) or 0
        couverts = (rec_hier.get("Couverts_Midi", 0) or 0) + (rec_hier.get("Couverts_Soir", 0) or 0)
        tm = round(ca_total / couverts, 1) if couverts > 0 else 0

        # Qualifier le service vs moyenne du mois
        try:
            ca_mois = at.get_ca_jour(restaurant_id, mois=hier.strftime("%Y-%m"))
            avg = sum((r.get("CA_Total", 0) or 0) for r in ca_mois) / max(len(ca_mois), 1)
            qualif = "Belle journée 👌" if ca_total > avg else "Service calme"
        except Exception:
            qualif = ""

        lines.append(f"*Hier — {_format_date_fr(hier)}*")
        if qualif:
            lines.append(qualif)
        lines.append(f"CA : {ca_total:,.0f}€ · {couverts} couverts · TM : {tm}€".replace(",", " "))
        if ca_bar > 0:
            lines.append(f"Bar : {ca_bar:,.0f}€".replace(",", " "))
        lines.append("")

    # ═══════════════════════════════════════════════════════
    #  AUJOURD'HUI — Météo + vacances + férié
    # ═══════════════════════════════════════════════════════
    lines.append("*Aujourd'hui*")
    if meteo:
        lines.append(f"{meteo['icon']} {meteo['temp']}°C — {meteo['description']}")

    zone = _check_vacances(today)
    if zone:
        lines.append(f"🏖️ Vacances Zone {zone} cette semaine — anticipez l'affluence")

    if _check_ferie(today):
        lines.append("🇫🇷 Jour férié")
    lines.append("")

    # ═══════════════════════════════════════════════════════
    #  2. ÉQUIPE DU JOUR — Planning Airtable
    # ═══════════════════════════════════════════════════════
    planning = _get_planning_day(restaurant_id, today.isoformat())

    if planning:
        midi_poles = {}   # pole -> [prenom, ...]
        soir_poles = {}
        earliest_prenom = ""
        earliest_time = "99:99"
        earliest_pole = ""
        midi_start = "99:99"
        soir_start = "99:99"

        for p in planning:
            hd = p.get("Shift_début", p.get("Shift_debut", ""))
            if not hd or hd in ("R", "CP"):
                continue
            emp_id = p.get("Employé_ID", p.get("Employe_ID", ""))
            emp = emp_map.get(emp_id, {})
            prenom = emp.get("Prénom", emp.get("Prenom", "?"))
            pole = emp.get("Pôle", emp.get("Pole", "Salle"))

            if hd < earliest_time:
                earliest_time = hd
                earliest_prenom = prenom
                earliest_pole = pole

            if hd < "16:00":
                midi_poles.setdefault(pole, []).append(prenom)
                if hd < midi_start:
                    midi_start = hd
            else:
                soir_poles.setdefault(pole, []).append(prenom)
                if hd < soir_start:
                    soir_start = hd

        lines.append("*Votre équipe aujourd'hui*")
        if earliest_prenom:
            lines.append(f"{earliest_prenom} ouvre {earliest_pole} à {earliest_time}")
        lines.append("")

        if midi_poles:
            lines.append(f"*Midi — {midi_start}*")
            for pole in ["Cuisine", "Salle", "Bar"]:
                prenoms = midi_poles.get(pole)
                if prenoms:
                    lines.append(f"{pole} : {', '.join(prenoms)} ({len(prenoms)})")
            lines.append("")

        if soir_poles:
            lines.append(f"*Soir — {soir_start}*")
            for pole in ["Cuisine", "Salle", "Bar"]:
                prenoms = soir_poles.get(pole)
                if prenoms:
                    lines.append(f"{pole} : {', '.join(prenoms)} ({len(prenoms)})")
            lines.append("")

    # ═══════════════════════════════════════════════════════
    #  3. ÉVÉNEMENTS DU JOUR
    # ═══════════════════════════════════════════════════════
    try:
        evenements = at.get_evenements(restaurant_id, mois=mois_str)
        today_evts = [e for e in evenements if (e.get("Date", "") or "")[:10] == today.isoformat()]
    except Exception:
        today_evts = []

    if today_evts:
        lines.append("*Au programme*")
        for evt in today_evts:
            lines.append(f"📌 {evt.get('Titre', '')}")
        lines.append("Pensez à briefer l'équipe avant le service.")
        lines.append("")

    # ═══════════════════════════════════════════════════════
    #  CUMUL DU MOIS
    # ═══════════════════════════════════════════════════════
    try:
        ca_mois_all = at.get_ca_jour(restaurant_id, mois=mois_str)
        ca_cumul = sum((r.get("CA_Total", 0) or 0) for r in ca_mois_all)
    except Exception:
        ca_cumul = 0

    import calendar
    nb_jours_mois = calendar.monthrange(today.year, today.month)[1]
    jours_restants = nb_jours_mois - today.day

    lines.append(f"*Le mois de {MOIS_NOMS[today.month - 1]}*")
    lines.append(f"Il vous reste {jours_restants} jours de service pour finir le mois.")
    lines.append(f"CA cumulé : {ca_cumul:,.0f}€".replace(",", " "))
    lines.append("")

    # ═══════════════════════════════════════════════════════
    #  4. ANNIVERSAIRES
    # ═══════════════════════════════════════════════════════
    for emp in emp_map.values():
        dn = emp.get("Date_naissance", "")
        if dn and len(str(dn)) >= 10:
            try:
                ds = str(dn)[:10]
                if int(ds[5:7]) == today.month and int(ds[8:10]) == today.day:
                    age = today.year - int(ds[:4])
                    prenom = emp.get("Prénom", emp.get("Prenom", ""))
                    lines.append(f"🎂 Aujourd'hui c'est l'anniversaire de {prenom} — {age} ans")
                    lines.append("")
            except (ValueError, IndexError):
                pass

    # --- Footer ---
    lines.append("Bonne journée à vous et à toute l'équipe,")
    lines.append("*Pauco*")

    return "\n".join(lines), None


def send_whatsapp(to_number, message):
    """Envoie un message WhatsApp via Twilio. Retourne (success, error)."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM]):
        return False, "Twilio non configuré (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM manquants)"

    # Normaliser le numéro
    to_clean = to_number.strip().replace(" ", "")
    if to_clean.startswith("0"):
        to_clean = "+33" + to_clean[1:]
    if not to_clean.startswith("+"):
        to_clean = "+" + to_clean

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    try:
        resp = requests.post(url, auth=(TWILIO_SID, TWILIO_TOKEN), data={
            "From": f"whatsapp:{TWILIO_FROM}",
            "To": f"whatsapp:{to_clean}",
            "Body": message,
        }, timeout=15)
        if resp.status_code in (200, 201):
            return True, None
        return False, f"Twilio HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return False, str(e)


def send_all_rapports():
    """Envoie le rapport matinal à tous les restaurants actifs non-démo avec WhatsApp configuré."""
    print(f"[RAPPORT] Début envoi rapports WhatsApp — {datetime.now().isoformat()}")

    try:
        restos = at.get_all("restaurants")
    except Exception as e:
        print(f"[RAPPORT] Erreur get_all restaurants: {e}")
        return

    sent = 0
    for resto in restos:
        rid = resto.get("Restaurant_ID", "")
        if not rid:
            continue

        # Skip demo restaurants
        try:
            settings = at.get_all("settings", restaurant_id=rid)
            is_demo = any(s.get("demo_mode") for s in settings)
        except Exception:
            is_demo = False
        if is_demo:
            continue

        # Get WhatsApp number from settings
        whatsapp_num = ""
        try:
            for s in settings:
                if s.get("Telephone_WhatsApp"):
                    whatsapp_num = s["Telephone_WhatsApp"]
                    break
        except Exception:
            pass

        if not whatsapp_num:
            continue

        # Build and send
        message, err = build_rapport(rid)
        if err:
            print(f"[RAPPORT] {rid}: erreur build — {err}")
            continue

        ok, send_err = send_whatsapp(whatsapp_num, message)
        if ok:
            sent += 1
            print(f"[RAPPORT] {rid}: envoyé à {whatsapp_num}")
            # Log in settings
            try:
                for s in settings:
                    at.update("settings", s["id"], {"dernier_rapport_envoye": datetime.now().isoformat()})
                    break
            except Exception:
                pass
        else:
            print(f"[RAPPORT] {rid}: échec envoi — {send_err}")

    print(f"[RAPPORT] Terminé — {sent} rapports envoyés")
