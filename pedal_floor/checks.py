"""Strength checks of the core and the hardpoint against datasheet allowables.

The face sheets stay a stiffness model (no check). Which checks run depends on the
strengths a material has in materials.csv:

- shear_strength_L / shear_strength_W: transverse shear XZ and YZ in the core layer
  (L is the material 1-direction, i.e. global X at core_angle 0), each on its own and
  combined by a quadratic interaction evaluated point by point:
  (txz / S_xz)^2 + (tyz / S_yz)^2 <= 1
- yield_strength: von Mises in the core layer (metal hardpoints), washer bearing, and
  the same shear checks against the shear yield, yield / sqrt(3)
- compressive_strength: washer bearing (crush) of the hardpoint

Each strength can be given as ``_typ`` and/or ``_min``; ``allowable_basis`` picks
which, falling back to the other if only one exists.

Margin of safety: MS = allowable / (safety_factor * stress) - 1; a check passes at MS >= 0.
For the interaction, R = sqrt((txz/S_xz)^2 + (tyz/S_yz)^2) at its worst point and
MS = 1 / (safety_factor * R) - 1.

Washer bearing: shells carry no through-thickness stress, so the hardpoint crush
stress under the nut is bolt force / lower washer contact area on the panel (washer
circle minus the slot). The core is not checked for crush: the hardpoint sits under
the washer.
"""

from __future__ import annotations

import math


def _allowable(m: dict, name: str, basis: str) -> tuple[float | None, str]:
    for b in (basis, "typ" if basis == "min" else "min"):
        value = m.get(f"{name}_{b}")
        if isinstance(value, float) and value > 0:
            return value, b
    return None, ""


def _check(prefix: str, stress: float, allowable: float, sf: float) -> dict:
    ms = allowable / (sf * stress) - 1 if stress > 0 else math.inf
    return {
        f"{prefix}_MPa": stress,
        f"{prefix}_allowable_MPa": allowable,
        f"{prefix}_MS": round(ms, 3) if math.isfinite(ms) else ms,
        f"{prefix}_ok": ms >= 0,
    }


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


def shear_allowables(p: dict, materials: dict[str, dict]) -> dict[str, tuple[float, float]]:
    """Allowable transverse shear (MPa) for global XZ and YZ, per region.

    Core/balsa: the L/W plate shear strengths (mapped to X/Y by core_angle; at any other
    angle both use the weaker). Metal: shear yield, yield / sqrt(3), both ways.
    Regions whose material has none of these strengths are left out.
    """
    basis = p["allowable_basis"]
    xz_name, yz_name = _shear_map(p["core_angle"])
    out = {}
    for region in REGIONS:
        m = materials[_region_material(p, region)]
        strengths = {name: _allowable(m, name, basis)[0] for name in ("shear_strength_L", "shear_strength_W")}
        if any(strengths.values()):
            weakest = min(v for v in strengths.values() if v)
            out[region] = (strengths.get(xz_name) or weakest, strengths.get(yz_name) or weakest)
        else:
            yield_strength, _ = _allowable(m, "yield_strength", basis)
            if yield_strength:
                out[region] = (yield_strength / math.sqrt(3),) * 2
    return out


def interaction_expression(allowables: tuple[float, float]) -> str:
    """Mechanical user-defined result for the quadratic shear interaction index R:
    R = sqrt((SXZ/S_xz)^2 + (SYZ/S_yz)^2), evaluated point by point (MPa)."""
    s_xz, s_yz = allowables
    return f"sqrt((SXZ/{s_xz!r})^2 + (SYZ/{s_yz!r})^2)"


def evaluate(p: dict, materials: dict[str, dict], stresses: dict, bearing: float) -> dict:
    """Columns for results.csv. ``stresses`` has max |value| in MPa for keys
    {core,hardpoint}_{xz,yz}, hardpoint_vm, and the max interaction index R
    (dimensionless) for {core,hardpoint}_interaction."""
    basis, sf = p["allowable_basis"], p["safety_factor"]
    allowables = shear_allowables(p, materials)
    out, region_ok = {}, {}
    for region in REGIONS:
        m = materials[_region_material(p, region)]
        checks = {}
        if region in allowables:
            for comp, allowable in zip(("xz", "yz"), allowables[region]):
                checks |= _check(f"{region}_shear_{comp}", stresses[f"{region}_{comp}"], allowable, sf)
            # Quadratic interaction of XZ and YZ at the same point:
            # (SF*txz/S_xz)^2 + (SF*tyz/S_yz)^2 <= 1, i.e. MS = 1 / (SF * R) - 1.
            r = stresses.get(f"{region}_interaction")
            if r is not None:
                ms = 1 / (sf * r) - 1 if r > 0 else math.inf
                checks |= {
                    f"{region}_shear_interaction_index": r,
                    f"{region}_shear_interaction_MS": round(ms, 3) if math.isfinite(ms) else ms,
                    f"{region}_shear_interaction_ok": ms >= 0,
                }
        yield_strength, _ = _allowable(m, "yield_strength", basis)
        if yield_strength and region == "hardpoint":
            checks |= _check(f"{region}_vonmises", stresses["hardpoint_vm"], yield_strength, sf)
        if region == "hardpoint":
            compressive, _ = _allowable(m, "compressive_strength", basis)
            if compressive or yield_strength:
                checks |= _check(f"{region}_washer_bearing", bearing, compressive or yield_strength, sf)
        out |= checks
        oks = [v for k, v in checks.items() if k.endswith("_ok")]
        region_ok[region] = all(oks) if oks else None  # None: no allowables to check against
        out[f"{region}_ok"] = region_ok[region]
    out["passes"] = all(v is not False for v in region_ok.values())
    return out
