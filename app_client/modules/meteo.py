"""Meteo via Open-Meteo (gratuit, sans cle API)."""

import requests

_WMO_CODES = {
    0: "Ensoleille", 1: "Peu nuageux", 2: "Peu nuageux", 3: "Couvert",
    45: "Brouillard", 48: "Brouillard givrant",
    51: "Bruine legere", 53: "Bruine", 55: "Bruine forte",
    56: "Bruine verglacante", 57: "Bruine verglacante forte",
    61: "Pluie legere", 63: "Pluie", 65: "Pluie forte",
    66: "Pluie verglacante", 67: "Pluie verglacante forte",
    71: "Neige legere", 73: "Neige", 75: "Neige forte", 77: "Grains de neige",
    80: "Averses legeres", 81: "Averses", 82: "Averses fortes",
    85: "Averses de neige", 86: "Averses de neige fortes",
    95: "Orage", 96: "Orage avec grele", 99: "Orage violent",
}

_WMO_ICONS = {
    0: "☀️", 1: "🌤️", 2: "⛅", 3: "☁️",
    45: "🌫️", 48: "🌫️",
    51: "🌦️", 53: "🌧️", 55: "🌧️", 56: "🌧️", 57: "🌧️",
    61: "🌦️", 63: "🌧️", 65: "🌧️", 66: "🌧️", 67: "🌧️",
    71: "🌨️", 73: "❄️", 75: "❄️", 77: "❄️",
    80: "🌦️", 81: "🌧️", 82: "🌧️", 85: "🌨️", 86: "🌨️",
    95: "⛈️", 96: "⛈️", 99: "⛈️",
}

# Default: Paris
DEFAULT_LAT = 48.8566
DEFAULT_LNG = 2.3522


def geocode_ville(ville):
    """Geocode une ville via Open-Meteo Geocoding API. Retourne (lat, lng) ou None."""
    try:
        r = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": ville, "count": 1, "language": "fr"},
            timeout=5,
        )
        if r.status_code == 200:
            results = r.json().get("results")
            if results:
                return results[0]["latitude"], results[0]["longitude"]
    except Exception:
        pass
    return None


def get_meteo(lat=None, lng=None, ville=None):
    """Retourne la meteo de demain via Open-Meteo."""
    if ville and not (lat and lng):
        coords = geocode_ville(ville)
        if coords:
            lat, lng = coords
    lat = lat or DEFAULT_LAT
    lng = lng or DEFAULT_LNG
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lng}"
            f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode"
            f"&timezone=Europe/Paris&forecast_days=2"
        )
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()
        daily = data.get("daily", {})
        if not daily or len(daily.get("time", [])) < 2:
            return None
        # Index 1 = demain
        code = daily["weathercode"][1]
        t_max = round(daily["temperature_2m_max"][1])
        t_min = round(daily["temperature_2m_min"][1])
        precip = daily["precipitation_sum"][1]
        return {
            "temp_max": t_max,
            "temp_min": t_min,
            "temp": t_max,  # compat
            "precipitation": round(precip, 1),
            "description": _WMO_CODES.get(code, "Inconnu"),
            "icon": _WMO_ICONS.get(code, "🌡️"),
            "code": code,
        }
    except Exception:
        return None
