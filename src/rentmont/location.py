"""Approximate straight-line distances from Sreality coordinates."""
import math

# Approximate centre of náměstí Svobody, Brno (WGS84).
BRNO_CENTER_LAT = 49.1951
BRNO_CENTER_LON = 16.6080
EARTH_RADIUS_KM = 6371.0088


def distance_to_center_km(locality: dict | None) -> float | None:
    if not isinstance(locality, dict):
        return None
    lat, lon = locality.get("gps_lat"), locality.get("gps_lon")
    for value in (lat, lon):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value):
            return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or (lat == 0 and lon == 0):
        return None
    latitude, center_latitude = math.radians(lat), math.radians(BRNO_CENTER_LAT)
    delta_lat = latitude - center_latitude
    delta_lon = math.radians(lon - BRNO_CENTER_LON)
    haversine = (math.sin(delta_lat / 2) ** 2
                 + math.cos(latitude) * math.cos(center_latitude)
                 * math.sin(delta_lon / 2) ** 2)
    # Clamp floating-point error before taking the square root/asin.
    distance = 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))
    return round(distance, 1)
