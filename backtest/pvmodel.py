"""Per-string plant model for the main array, ported from the PV reconstruction.

`plant()` is the reconstruction's model verbatim (pvlib: Perez transposition,
physical IAM, Faiman cell temperature, PVWatts DC). The strings, their
orientation and their per-string scale come from PLANT["pv"]["strings"]
(plant.json): on the reference plant the scales were fitted on clean hours
March to September 2026 (0,5286 and 0,8094 at full precision; rounded to
three decimals the model sat up to 7 W off the reconstruction's lane), so a
new plant starts at scale 1,0 per string and fits its own. The site
(latitude, longitude, altitude) is PLANT["site"]. The backtest runs archived
irradiance forecasts through this model so every forecast lane is judged on
the same plant, and the potential builder (frames.potential) lays the
model's intra-hour shape on a suspect hour.

The first string in the table is the one the AC-coupled microinverter's
forecast site shares (PLANT pv.micro_share); replay_inputs reads it as such.
"""
import pandas as pd
import pvlib

from emhasscore.plant import PLANT

LAT, LON, ALT = (float(PLANT["site"]["latitude"]), float(PLANT["site"]["longitude"]),
                 float(PLANT["site"]["altitude_m"]))
STRINGS = {name: dict(tilt=float(s["tilt"]), az=float(s["azimuth"]), kwp=float(s["kwp"]))
           for name, s in PLANT["pv"]["strings"].items()}
SCALE = {name: float(s.get("scale", 1.0)) for name, s in PLANT["pv"]["strings"].items()}
STRING_NAMES = list(STRINGS)
MICRO_STRING = STRING_NAMES[0]                       # the string whose forecast site carries the microinverter
ARRAY_CAP_W = round(sum(SCALE[n] * STRINGS[n]["kwp"] for n in STRINGS) * 1000.0)   # the array's effective capacity


def plant(irr, tilt, az, kwp, times_mid, temp_air, wind):
    sp = pvlib.solarposition.get_solarposition(times_mid, LAT, LON, altitude=ALT)
    sp.index = irr.index
    dni_extra = pd.Series(pvlib.irradiance.get_extra_radiation(times_mid).to_numpy(), index=irr.index)
    am = pvlib.atmosphere.get_relative_airmass(sp["apparent_zenith"])
    poa = pvlib.irradiance.get_total_irradiance(tilt, az, sp["apparent_zenith"], sp["azimuth"],
            dni=irr["dni"], ghi=irr["ghi"], dhi=irr["dhi"], dni_extra=dni_extra, airmass=am,
            albedo=0.2, model="perez")
    aoi = pvlib.irradiance.aoi(tilt, az, sp["apparent_zenith"], sp["azimuth"])
    iam = pvlib.iam.physical(aoi)
    poa_eff = (poa["poa_direct"] * iam + poa["poa_diffuse"]).clip(lower=0).fillna(0)
    tcell = pvlib.temperature.faiman(poa_eff, temp_air, wind)
    pdc = pvlib.pvsystem.pvwatts_dc(poa_eff, tcell, pdc0=kwp * 1000, gamma_pdc=-0.0035)
    return pdc / 1000.0, poa_eff


def string_kw(irr, met, times_mid) -> dict:
    """{string name: calibrated kW series} for an irradiance frame with columns
    ghi, dhi, dni (W/m2) and a met frame with temp_c, wind_ms on the same index."""
    out = {}
    for name, g in STRINGS.items():
        kw, _ = plant(irr, g["tilt"], g["az"], g["kwp"], times_mid, met["temp_c"], met["wind_ms"])
        out[name] = kw * SCALE[name]
    return out


def main_array_kw(irr, met, times_mid) -> "pd.Series":
    """Calibrated main-array output in kW: the sum over the strings."""
    total = 0.0
    for kw in string_kw(irr, met, times_mid).values():
        total = total + kw
    return total
