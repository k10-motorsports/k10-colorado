"""Solar azimuth/elevation for the track location (lat/lon/timezone from config).

Pure-stdlib NOAA solar-position algorithm (no astral/pvlib/numpy) so it runs on the headless
Mac build host, which deliberately keeps a minimal Python. Accurate to well under 0.5 deg, which is
all the lighting/sun-orientation pass needs. Verified against `astral` (see tests).

Reference: NOAA Solar Calculator spreadsheet (gml.noaa.gov/grad/solcalc/), the standard
low-precision equations good to ~0.01 deg for dates 1901-2099.
"""

from __future__ import annotations

import datetime as _dt
import math
from zoneinfo import ZoneInfo


def _to_utc(when_iso: str, timezone: str) -> _dt.datetime:
    """Parse ``when_iso`` (naive or offset-aware) in ``timezone`` and return a UTC datetime."""
    dt = _dt.datetime.fromisoformat(when_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(timezone))
    return dt.astimezone(_dt.timezone.utc)


def _julian_day(dt_utc: _dt.datetime) -> float:
    """Julian Day (with fractional day) for a UTC datetime."""
    y, m = dt_utc.year, dt_utc.month
    d = (
        dt_utc.day
        + (dt_utc.hour + (dt_utc.minute + dt_utc.second / 60.0) / 60.0) / 24.0
    )
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def solar_position(lat: float, lon: float, when_iso: str, timezone: str) -> tuple[float, float]:
    """Return ``(azimuth_deg, elevation_deg)`` of the sun at ``when_iso`` for the given site.

    Azimuth is degrees clockwise from true north (0=N, 90=E, 180=S, 270=W). Elevation is degrees
    above the horizon (negative below). ``lon`` is signed (east positive, so US longitudes are
    negative). ``when_iso`` may be naive (interpreted in ``timezone``) or carry an offset.
    """
    dt_utc = _to_utc(when_iso, timezone)
    jd = _julian_day(dt_utc)
    jc = (jd - 2451545.0) / 36525.0  # Julian centuries since J2000.0

    # Sun geometric mean longitude / anomaly (deg)
    geom_mean_long = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    geom_mean_anom = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    ecc = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)

    m = math.radians(geom_mean_anom)
    sun_eq_ctr = (
        math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
        + math.sin(2 * m) * (0.019993 - 0.000101 * jc)
        + math.sin(3 * m) * 0.000289
    )
    true_long = geom_mean_long + sun_eq_ctr
    app_long = true_long - 0.00569 - 0.00478 * math.sin(math.radians(125.04 - 1934.136 * jc))

    # Obliquity of the ecliptic (deg), with nutation correction
    mean_obliq = (
        23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813))) / 60.0) / 60.0
    )
    obliq_corr = mean_obliq + 0.00256 * math.cos(math.radians(125.04 - 1934.136 * jc))

    # Declination (deg)
    decl = math.degrees(
        math.asin(math.sin(math.radians(obliq_corr)) * math.sin(math.radians(app_long)))
    )

    # Equation of time (minutes)
    var_y = math.tan(math.radians(obliq_corr / 2.0)) ** 2
    l0 = math.radians(geom_mean_long)
    eot = 4.0 * math.degrees(
        var_y * math.sin(2 * l0)
        - 2 * ecc * math.sin(m)
        + 4 * ecc * var_y * math.sin(m) * math.cos(2 * l0)
        - 0.5 * var_y * var_y * math.sin(4 * l0)
        - 1.25 * ecc * ecc * math.sin(2 * m)
    )

    # True solar time (minutes) -> hour angle (deg). Uses UTC minute-of-day and the real longitude,
    # so no separate timezone-offset term is needed (dt is already UTC).
    minutes_utc = dt_utc.hour * 60.0 + dt_utc.minute + dt_utc.second / 60.0
    true_solar_time = (minutes_utc + eot + 4.0 * lon) % 1440.0
    hour_angle = true_solar_time / 4.0 - 180.0
    if hour_angle < -180.0:
        hour_angle += 360.0

    ha = math.radians(hour_angle)
    la = math.radians(lat)
    de = math.radians(decl)

    zenith = math.degrees(
        math.acos(
            max(
                -1.0,
                min(1.0, math.sin(la) * math.sin(de) + math.cos(la) * math.cos(de) * math.cos(ha)),
            )
        )
    )
    elevation = 90.0 - zenith

    # Azimuth (deg clockwise from north)
    za = math.radians(zenith)
    denom = math.cos(la) * math.sin(za)
    if abs(denom) < 1e-9:
        azimuth = 180.0 if lat > decl else 0.0
    else:
        cos_az = (math.sin(la) * math.cos(za) - math.sin(de)) / denom
        cos_az = max(-1.0, min(1.0, cos_az))
        az = math.degrees(math.acos(cos_az))
        # NOAA convention: morning (hour angle < 0) -> azimuth = 180 - az ... expressed as:
        azimuth = (az + 180.0) % 360.0 if hour_angle > 0.0 else (540.0 - az) % 360.0

    return azimuth, elevation


def sun_world_vector(azimuth_deg: float, elevation_deg: float) -> tuple[float, float, float]:
    """Sun direction as a unit vector in the ENU local frame (X=east, Y=up, Z=north).

    A sunset sun (azimuth ~301 deg, elevation ~0) yields a strongly negative X (west) component --
    the Mac-side oracle used to assert the sun lands on the same (west) side as the mountains
    backdrop after mirror_x. See scripts/lighting/csp_config.py.
    """
    az = math.radians(azimuth_deg)
    el = math.radians(elevation_deg)
    horiz = math.cos(el)
    x = horiz * math.sin(az)   # east
    z = horiz * math.cos(az)   # north
    y = math.sin(el)           # up
    return x, y, z
