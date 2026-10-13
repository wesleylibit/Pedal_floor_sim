"""Load the input CSVs (with units), merge constants with a sweep row, validate, and parse layups.

Everything is converted to mm, N, MPa, kg/m^3 and degrees on load, so the rest
of the code never deals with inches or psi.
"""

from __future__ import annotations

import dataclasses
import math
import os
import pathlib
import re

import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parents[1]
INPUTS = ROOT / "inputs"
OUTPUTS = ROOT / "outputs"

SUPPORTED_ANSYS_VERSIONS = (261, 252)  # tested on 252; 261 is the preferred target


def _find_ansys() -> tuple[int | None, pathlib.Path | None]:
    """Pick the Ansys installation from its AWP_ROOTxxx variables (set by the installer).

    Set PEDAL_FLOOR_ANSYS_VERSION (e.g. 252 or 261) to choose one explicitly;
    otherwise the newest supported version installed is used.
    """
    installed = {int(k[8:]): pathlib.Path(v) for k, v in os.environ.items() if re.fullmatch(r"AWP_ROOT\d{3}", k)}
    wanted = os.environ.get("PEDAL_FLOOR_ANSYS_VERSION")
    if wanted:
        version = int(wanted)
    else:
        version = next((v for v in SUPPORTED_ANSYS_VERSIONS if v in installed), max(installed, default=None))
    return version, installed.get(version)


ANSYS_VERSION, ANSYS_ROOT = _find_ansys()


def require_ansys() -> None:
    if ANSYS_ROOT is None or not ANSYS_ROOT.exists():
        raise RuntimeError(
            f"No Ansys installation found for version {ANSYS_VERSION}. Install Ansys 2025 R2 or 2026 R1, "
            "or set PEDAL_FLOOR_ANSYS_VERSION to an installed version (AWP_ROOTxxx must exist)."
        )

# Factor that converts a value in the given unit to the internal unit
# (mm, N, MPa, kg/m^3, deg). Unitless entries use "-" or an empty cell.
UNIT_FACTORS = {
    "": 1.0, "-": 1.0, "deg": 1.0,
    "mm": 1.0, "m": 1000.0, "in": 25.4, "ft": 304.8,
    "N": 1.0, "kN": 1000.0, "lbf": 4.4482216152605,
    "MPa": 1.0, "Pa": 1e-6, "GPa": 1000.0, "psi": 0.006894757293168, "ksi": 6.894757293168,
    "kg/m3": 1.0, "g/cm3": 1000.0, "pcf": 16.01846337396, "lb/ft3": 16.01846337396,
}

REQUIRED = [
    "case_id", "panel_length", "rear_width", "front_width",
    "hole_rad", "slot_length", "hole_rear_x", "hole_rear_y", "hardpoint_rad",
    "core_thickness", "core_angle", "layup", "woven_material", "uni_material", "core_material",
    "hardpoint_material", "edge_bc",
    "pedalbox_cad", "pedalbox_thickness", "pedalbox_material", "pedalbox_offset_step",
    "bolt_diameter", "washer_diameter_top", "washer_diameter_bottom", "washer_thickness", "washer_material",
    "nut_bearing_diameter", "joint_zone_radius", "bolt_material", "bolt_preload",
    "load_fx", "load_fy", "load_fz", "load_height", "load_behavior",
    "mesh_quality", "contact_plot_max", "result_units", "allowable_basis", "safety_factor",
]
EDGE_BCS = ("fixed", "simply_supported")
MESH_QUALITIES = ("coarse", "fine")
CORE_TOKEN = "core"
UNI_TOKEN = "u"


def convert(value, unit: str):
    """Return ``value`` in internal units; text values are returned unchanged."""
    unit = str(unit).strip()
    if unit not in UNIT_FACTORS:
        raise ValueError(f"Unknown unit '{unit}'. Known units: {sorted(u for u in UNIT_FACTORS if u)}")
    text = str(value).strip()
    try:
        return float(text) * UNIT_FACTORS[unit]
    except ValueError:
        return text


def _split_header(header: str) -> tuple[str, str]:
    """'hardpoint_rad [in]' -> ('hardpoint_rad', 'in')."""
    match = re.fullmatch(r"\s*([^\[\]]+?)\s*(?:\[(.*)\])?\s*", header)
    return match.group(1), (match.group(2) or "").strip()


def load_constants(path: pathlib.Path = INPUTS / "constants.csv") -> dict:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return {row.parameter.strip(): convert(row.value, row.unit) for row in df.itertuples()}


def load_sweep(path: pathlib.Path = INPUTS / "sweep.csv") -> list[dict]:
    """One dict per row, keyed by parameter name (units stripped from the headers and applied)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    columns = [_split_header(c) for c in df.columns]
    rows = []
    for raw in df.itertuples(index=False):
        rows.append({name: convert(v, unit) for (name, unit), v in zip(columns, raw) if str(v).strip() != ""})
    ids = [int(r["case_id"]) for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate case_id in {path.name}")
    return rows


def load_sweep_raw(path: pathlib.Path = INPUTS / "sweep.csv") -> list[dict]:
    """The sweep rows exactly as written (for results.csv)."""
    return pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records")


def load_materials(path: pathlib.Path = INPUTS / "materials.csv") -> dict[str, dict]:
    """Long format, one property per row: material, property, value, unit, note."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    materials: dict[str, dict] = {}
    for row in df.itertuples():
        materials.setdefault(row.material.strip(), {"name": row.material.strip()})[row.property.strip()] = convert(row.value, row.unit)
    for name, m in materials.items():
        kind = m.get("ply_type")
        needed = ["density", "E", "nu"] if kind == "isotropic" else ["density", "E1", "E2", "E3", "nu12", "nu13", "nu23", "G12", "G13", "G23"]
        missing = [k for k in ["ply_type", *needed] if k not in m]
        if missing:
            raise ValueError(f"materials.csv: '{name}' is missing {missing}")
    return materials


def case_params(constants: dict, row: dict, materials: dict[str, dict]) -> dict:
    """Constants overridden by the sweep row, so any parameter can live in either file."""
    params = {**constants, **row}
    params["case_id"] = int(params["case_id"])
    validate(params, materials)
    return params


@dataclasses.dataclass(frozen=True)
class Ply:
    material: str  # material name, or CORE_TOKEN for the core/hardpoint layer
    angle: float  # degrees from global X


def parse_layup(text: str, p: dict) -> list[Ply]:
    """Parse e.g. '(0 U U 45) Core (45 U U 0)'.

    Tokens are listed in stacking order starting at the pedal box face. ``U`` is a UD ply at
    0 deg (``uni_material``), a number is a woven ply at that angle
    (``woven_material``), and ``Core`` marks the core/hardpoint layer. Parentheses
    are only for readability; tokens may be separated by spaces or '/'.
    """
    plies = []
    for token in re.split(r"[\s/]+", re.sub(r"[()]", " ", str(text)).strip()):
        if token.lower() == CORE_TOKEN:
            plies.append(Ply(CORE_TOKEN, 0.0))
        elif token.lower() == UNI_TOKEN:
            plies.append(Ply(p["uni_material"], 0.0))
        else:
            try:
                plies.append(Ply(p["woven_material"], float(token)))
            except ValueError:
                raise ValueError(f"Layup '{text}': unknown token '{token}' (use an angle, U or Core)") from None
    n_core = sum(ply.material == CORE_TOKEN for ply in plies)
    if n_core != 1:
        raise ValueError(f"Layup '{text}' must contain exactly one Core, found {n_core}.")
    return plies


def core_layer(p: dict) -> int:
    """Shell layer number of the core/hardpoint layer. Layers count along the +Z element
    normal, i.e. from the pedal box face, which is also the order the layup is listed in."""
    plies = parse_layup(p["layup"], p)
    return next(i for i, ply in enumerate(plies, 1) if ply.material == CORE_TOKEN)


def panel_thickness(p: dict, materials: dict[str, dict]) -> float:
    """Total laminate thickness in mm (face plies plus core)."""
    return p["core_thickness"] + sum(
        materials[ply.material]["ply_thickness"] for ply in parse_layup(p["layup"], p) if ply.material != CORE_TOKEN
    )


def half_width(p: dict, x: float) -> float:
    """Half-width of the trapezoid at distance x from the rear edge."""
    return p["rear_width"] / 2 + (p["front_width"] - p["rear_width"]) / 2 * x / p["panel_length"]


def slot_centre_x(p: dict) -> float:
    return p["hole_rear_x"] + p["slot_length"] / 2


def validate(p: dict, materials: dict[str, dict]) -> None:
    cid = p.get("case_id")
    missing = [k for k in REQUIRED if k not in p]
    if missing:
        raise ValueError(f"Case {cid}: missing parameters {missing}")
    for key, allowed in [("edge_bc", EDGE_BCS), ("mesh_quality", MESH_QUALITIES),
                         ("load_behavior", ("deformable", "rigid")), ("result_units", ("imperial", "metric")),
                         ("allowable_basis", ("min", "typ"))]:
        if p[key] not in allowed:
            raise ValueError(f"Case {cid}: {key} must be one of {allowed}, got '{p[key]}'")
    for key in [f"mesh_global_{p['mesh_quality']}", f"mesh_local_{p['mesh_quality']}", f"mesh_pedalbox_{p['mesh_quality']}"]:
        if key not in p:
            raise ValueError(f"Case {cid}: missing mesh size '{key}'")

    plies = parse_layup(p["layup"], p)
    for name in {ply.material for ply in plies if ply.material != CORE_TOKEN}:
        if name not in materials:
            raise ValueError(f"Case {cid}: ply material '{name}' not in materials.csv")
        if materials[name].get("ply_thickness", 0) <= 0:
            raise ValueError(f"Case {cid}: material '{name}' needs a ply_thickness")
    for key in ("core_material", "hardpoint_material", "pedalbox_material", "bolt_material", "washer_material"):
        if p[key] not in materials:
            raise ValueError(f"Case {cid}: {key} '{p[key]}' not in materials.csv")
    if not (INPUTS / p["pedalbox_cad"]).exists():
        raise ValueError(f"Case {cid}: pedal box CAD '{p['pedalbox_cad']}' not found in inputs/")

    # The hardpoint is a stadium of radius hardpoint_rad around the left slot centreline,
    # trimmed where it reaches past the panel edges; the right slot (mirrored in Y) is a plain hole.
    r_hp, y = p["hardpoint_rad"], abs(p["hole_rear_y"])
    if r_hp <= p["hole_rad"] + 0.5:
        raise ValueError(f"Case {cid}: hardpoint_rad ({r_hp:.2f} mm) must exceed hole_rad ({p['hole_rad']:.2f} mm) by > 0.5 mm")
    for key in ("washer_diameter_top", "washer_diameter_bottom", "nut_bearing_diameter"):
        if p[key] <= p["bolt_diameter"]:
            raise ValueError(f"Case {cid}: {key} must be larger than bolt_diameter")
    if p["washer_thickness"] <= 0:
        raise ValueError(f"Case {cid}: washer_thickness must be positive")
    # The slots and the washers on the panel (bottom washers, at any pedal box position) must fit.
    r_out = max(p["hole_rad"], p["washer_diameter_bottom"] / 2)
    x_min, x_max = p["hole_rear_x"] - r_out, p["hole_rear_x"] + p["slot_length"] + r_out
    if p["slot_length"] <= 0 or x_min <= 0 or x_max >= p["panel_length"]:
        raise ValueError(f"Case {cid}: the slot and its washers span x = {x_min:.1f}..{x_max:.1f} mm, outside the panel")
    if y - r_out <= 0:
        raise ValueError(f"Case {cid}: the two slots overlap at the centreline")
    if y + r_out >= min(half_width(p, x_min), half_width(p, x_max)):
        raise ValueError(f"Case {cid}: the slot or its washers reach the side edge of the panel")
    if r_hp >= 2 * y - p["hole_rad"]:
        raise ValueError(f"Case {cid}: the hardpoint (radius {r_hp:.1f} mm) reaches the right slot, which has none")


def pedal_offsets(p: dict, hole_spacing: float) -> list[float]:
    """Forward offsets of the pedal box along the slot, from the rearmost position (0)
    to the foremost one (front bolt at the front end of the slot), in steps of
    ``pedalbox_offset_step``. A step of 0 means only the rearmost position."""
    max_offset = p["slot_length"] - hole_spacing
    if max_offset < 0:
        raise ValueError(f"Pedal box hole spacing ({hole_spacing:.1f} mm) is longer than the slot ({p['slot_length']:.1f} mm)")
    step = p["pedalbox_offset_step"]
    if step <= 0:
        return [0.0]
    offsets = [i * step for i in range(int(max_offset // step + 1e-9) + 1)]
    if max_offset - offsets[-1] > 1e-6:
        offsets.append(max_offset)
    return offsets


def layup_areal_mass(p: dict, materials: dict[str, dict]) -> tuple[float, float, float]:
    """Areal masses in kg/m^2: (face plies, core layer, hardpoint layer)."""
    faces = sum(
        materials[ply.material]["ply_thickness"] * 1e-3 * materials[ply.material]["density"]
        for ply in parse_layup(p["layup"], p)
        if ply.material != CORE_TOKEN
    )
    t_core = p["core_thickness"] * 1e-3
    return faces, t_core * materials[p["core_material"]]["density"], t_core * materials[p["hardpoint_material"]]["density"]


def masses(p: dict, materials: dict[str, dict], geo: dict) -> dict:
    """Weight breakdown from areal weights (panel layers) and densities (everything else).

    Keys end in a unit suffix for runner._to_result_units: areal masses in kg/m^2
    (``_kgm2``), masses in kg (``_kg``). Areas come from the geometry (slot holes and
    pedal box holes excluded). Bolts are the shank over the grip length only (no head or nut).
    """
    faces, core, hardpoint = layup_areal_mass(p, materials)
    area_core, area_hp = geo["area_core_mm2"] * 1e-6, geo["area_hardpoint_mm2"] * 1e-6
    t_panel, t_washer, t_pb = geo["panel_thickness_mm"], p["washer_thickness"], p["pedalbox_thickness"]
    washer_area = math.pi / 4 * (p["washer_diameter_bottom"] ** 2 - p["bolt_diameter"] ** 2) * 1e-6
    bolt_volume = math.pi / 4 * p["bolt_diameter"] ** 2 * (t_panel + t_washer + t_pb) * 1e-9
    parts = {
        "face_sheets": (area_core + area_hp) * faces,
        "core": area_core * core,
        "hardpoint": area_hp * hardpoint,
        "washers": 2 * washer_area * t_washer * 1e-3 * materials[p["washer_material"]]["density"],
        "bolts": 2 * bolt_volume * materials[p["bolt_material"]]["density"],
        "pedalbox_plate": geo["area_pedalbox_mm2"] * 1e-6 * t_pb * 1e-3 * materials[p["pedalbox_material"]]["density"],
    }
    panel = parts["face_sheets"] + parts["core"] + parts["hardpoint"]
    return {
        "face_sheets_areal_mass_kgm2": faces,
        "core_layer_areal_mass_kgm2": core,
        "hardpoint_layer_areal_mass_kgm2": hardpoint,
        **{f"mass_{name}_kg": value for name, value in parts.items()},
        "panel_mass_kg": panel,
        "system_mass_kg": sum(parts.values()),
    }


def run_dir(case_id: int, offset_mm: float) -> pathlib.Path:
    return OUTPUTS / f"case_{case_id:03d}_off{offset_mm / 25.4:05.2f}in"
