"""Strength checks of the core and the hardpoint against datasheet allowables.

The face sheets stay a stiffness model (no check). The check depends on the strengths a
material has in materials.csv:

- Orthotropic (shear_strength_L / shear_strength_W and compressive_strength, e.g.
  honeycomb or balsa): one interaction index, evaluated point by point over the layer and
  both load steps (interaction.py):
  R = sqrt((txz / S_xz)^2 + (tyz / S_yz)^2 + (sz / S_c)^2) <= 1
  S_xz, S_yz: the L or W plate shear strength (L is the material 1-direction, i.e. global
  X at core_angle 0); S_c: the (stabilised) compressive strength; sz: through-thickness
  compression, from the contact pressure on the panel. Without a compressive strength the
  crush term is left out.
- Isotropic (yield_strength, e.g. aluminium): von Mises in the layer against yield.

Each strength can be given as ``_typ`` and/or ``_min``; ``allowable_basis`` picks which,
falling back to the other if only one exists.

Margin of safety: MS = 1 / (safety_factor * R) - 1 for the index, and
allowable / (safety_factor * stress) - 1 for von Mises; a check passes at MS >= 0.
"""

from __future__ import annotations

import math


def _allowable(m: dict, name: str, basis: str) -> float | None:
    for b in (basis, "typ" if basis == "min" else "min"):
        value = m.get(f"{name}_{b}")
        if isinstance(value, float) and value > 0:
            return value
    return None


def _margin(capacity: float, demand: float, sf: float) -> float:
    ms = capacity / (sf * demand) - 1 if demand > 0 else math.inf
    return round(ms, 3) if math.isfinite(ms) else ms


def _shear_map(core_angle: float) -> tuple[str, str]:
    """Which strength (L or W) applies to global XZ and YZ shear."""
    angle = core_angle % 180
    if abs(angle) < 1e-6:
        return "shear_strength_L", "shear_strength_W"
    if abs(angle - 90) < 1e-6:
        return "shear_strength_W", "shear_strength_L"
    return "", ""  # any other angle: check both against the weaker direction


REGIONS = ("core", "hardpoint")


def _region_material(p: dict, region: str) -> str:
    return p["core_material"] if region == "core" else p["hardpoint_material"]


def allowables(p: dict, materials: dict[str, dict]) -> dict[str, dict]:
    """Allowables (MPa) per region: {"kind": "orthotropic", "xz", "yz", "crush"} from the
    L/W shear strengths (mapped to X/Y by core_angle; at any other angle both use the
    weaker) and the compressive strength, or {"kind": "isotropic", "yield"}. Regions whose
    material has none of these strengths are left out."""
    basis = p["allowable_basis"]
    xz_name, yz_name = _shear_map(p["core_angle"])
    out = {}
    for region in REGIONS:
        m = materials[_region_material(p, region)]
        shear = {name: _allowable(m, name, basis) for name in ("shear_strength_L", "shear_strength_W")}
        if any(shear.values()):
            weakest = min(v for v in shear.values() if v)
            out[region] = {"kind": "orthotropic", "xz": shear.get(xz_name) or weakest, "yz": shear.get(yz_name) or weakest,
                           "crush": _allowable(m, "compressive_strength", basis)}
        elif yield_strength := _allowable(m, "yield_strength", basis):
            out[region] = {"kind": "isotropic", "yield": yield_strength}
    return out


def evaluate(p: dict, materials: dict[str, dict], summary: dict) -> dict:
    """Columns for results.csv from interaction.evaluate's ``summary`` (stresses in MPa)."""
    sf = p["safety_factor"]
    out, region_ok = {}, {}
    for region, a in allowables(p, materials).items():
        s = summary.get(region)
        if not s:
            continue
        # Largest value of each stress anywhere in the layer, for information.
        out |= {f"{region}_shear_xz_MPa": s["xz_max"], f"{region}_shear_yz_MPa": s["yz_max"], f"{region}_crush_MPa": s["crush_max"]}
        ok = None
        if a["kind"] == "orthotropic" and "index" in s:
            ms = _margin(1.0, s["index"], sf)
            out |= {f"{region}_{name}_allowable_MPa": a[c]
                    for c, name in (("xz", "shear_xz"), ("yz", "shear_yz"), ("crush", "crush")) if a.get(c)}
            out |= {f"{region}_interaction_index": s["index"], f"{region}_interaction_MS": ms,
                    f"{region}_interaction_ok": ms >= 0, f"{region}_interaction_governing": s["governing"]}
            ok = ms >= 0
        elif a["kind"] == "isotropic" and "vm_max" in s:
            ms = _margin(a["yield"], s["vm_max"], sf)
            out |= {f"{region}_vonmises_MPa": s["vm_max"], f"{region}_vonmises_allowable_MPa": a["yield"],
                    f"{region}_vonmises_MS": ms, f"{region}_vonmises_ok": ms >= 0}
            ok = ms >= 0
        out[f"{region}_ok"] = region_ok[region] = ok
    out["passes"] = all(v is not False for v in region_ok.values())
    return out
