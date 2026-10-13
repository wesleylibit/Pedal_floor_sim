"""Stage 3: build the sandwich layup in ACP on the mesh exported from Mechanical.

This is a stiffness model, so materials carry elastic constants and density only.
"""

from __future__ import annotations

import logging
import pathlib

import ansys.acp.core as pyacp
from ansys.acp.core import material_property_sets as mps

from .config import ANSYS_ROOT, CORE_TOKEN, parse_layup, require_ansys
from .geometry import NS_CORE, NS_HARDPOINTS, NS_PANEL

log = logging.getLogger("pedal_floor")

# Factors from internal units (MPa, kg/m^3, mm) to the ACP model's unit system.
_UNIT_SCALE = {
    "si": (1e6, 1.0, 1e-3),
    "mks": (1e6, 1.0, 1e-3),
    "mpa": (1.0, 1e-12, 1.0),
}


def launch():
    require_ansys()
    binary = str(ANSYS_ROOT / "ACP" / "acp_grpcserver.exe")
    try:
        return pyacp.launch_acp(config=pyacp.DirectLaunchConfig(binary_path=binary), launch_mode=pyacp.LaunchMode.DIRECT)
    except RuntimeError as exc:
        if "patched version" not in str(exc):
            raise
        # Like Mechanical, ACP 2025 R2 before its gRPC service pack only supports the
        # unauthenticated transport; the server binds to localhost only.
        log.warning("ACP has no secure gRPC; using the local insecure transport.")
        config = pyacp.DirectLaunchConfig(binary_path=binary, transport_mode="insecure")
        return pyacp.launch_acp(config=config, launch_mode=pyacp.LaunchMode.DIRECT)


def _create_material(model, m: dict, stress: float, density: float):
    if m["ply_type"] == "isotropic":
        constants = mps.ConstantEngineeringConstants.from_isotropic_constants(E=m["E"] * stress, nu=m["nu"])
    else:
        constants = mps.ConstantEngineeringConstants.from_orthotropic_constants(
            E1=m["E1"] * stress, E2=m["E2"] * stress, E3=m["E3"] * stress,
            nu12=m["nu12"], nu13=m["nu13"], nu23=m["nu23"],
            G12=m["G12"] * stress, G23=m["G23"] * stress, G31=m["G13"] * stress,
        )
    return model.create_material(
        name=m["name"],
        ply_type=m["ply_type"],
        density=mps.ConstantDensity(rho=m["density"] * density),
        engineering_constants=constants,
    )


def build_layup(acp, p: dict, materials: dict[str, dict], mesh_path: pathlib.Path, out_dir: pathlib.Path) -> dict:
    """Define the layup on the panel elements and export the files Mechanical needs.

    The pedal box, washer and bolt materials are created too (without plies) so they
    reach Mechanical through the same materials file.
    """
    acp.clear()
    model = acp.import_model(path=mesh_path, format="ansys:h5", name=f"case_{p['case_id']:03d}")
    stress, density, length = _UNIT_SCALE[model.unit_system.value]
    plies = parse_layup(p["layup"], p)

    face_names = sorted({ply.material for ply in plies if ply.material != CORE_TOKEN})
    other_names = {p["core_material"], p["hardpoint_material"], p["pedalbox_material"], p["bolt_material"], p["washer_material"]}
    acp_materials = {
        name: _create_material(model, materials[name], stress, density)
        for name in face_names + sorted(other_names - set(face_names))
    }
    fabrics = {
        name: model.create_fabric(name=name, material=acp_materials[name], thickness=materials[name]["ply_thickness"] * length)
        for name in face_names
    }
    # Core and hardpoint share one layer of the stack, so they get the same thickness.
    core_fabric = model.create_fabric(
        name="core", material=acp_materials[p["core_material"]], thickness=p["core_thickness"] * length
    )
    hardpoint_fabric = model.create_fabric(
        name="hardpoint", material=acp_materials[p["hardpoint_material"]], thickness=p["core_thickness"] * length
    )

    # 0 deg is global X.
    rosette = model.create_rosette(name="global_x", origin=(0.0, 0.0, 0.0), dir1=(1.0, 0.0, 0.0), dir2=(0.0, 1.0, 0.0))
    direction = (0.0, 0.0, -1.0)  # from the back face (z = 0) toward the pedal box
    # Any point on the panel core; it sits on the centreline between the two slots.
    point = (p["panel_length"] / 2 * length, 0.0, 0.0)

    def oriented_set(name, element_set_name):
        return model.create_oriented_selection_set(
            name=name,
            element_sets=[model.element_sets[element_set_name]],
            orientation_point=point,
            orientation_direction=direction,
            rosettes=[rosette],
        )

    oss_panel = oriented_set("oss_panel", NS_PANEL)
    oss_core = oriented_set("oss_core", NS_CORE)
    oss_hardpoint = oriented_set("oss_hardpoint", NS_HARDPOINTS)

    # ACP stacks plies from the reference surface (the panel's back face) toward -Z, while
    # the layup is listed from the pedal box face, so create them in reverse. Ply names
    # keep the listed position (P01 = against the pedal box).
    group = model.create_modeling_group(name="sandwich")
    for i, ply in reversed(list(enumerate(plies, 1))):
        if ply.material == CORE_TOKEN:
            group.create_modeling_ply(
                name=f"P{i:02d}_core", ply_material=core_fabric,
                oriented_selection_sets=[oss_core], ply_angle=p["core_angle"],
            )
            group.create_modeling_ply(
                name=f"P{i:02d}_hardpoint", ply_material=hardpoint_fabric,
                oriented_selection_sets=[oss_hardpoint], ply_angle=p["core_angle"],
            )
        else:
            group.create_modeling_ply(
                name=f"P{i:02d}_{ply.material}_{ply.angle:g}", ply_material=fabrics[ply.material],
                oriented_selection_sets=[oss_panel], ply_angle=ply.angle,
            )
    model.update()

    files = {
        "acp_model": out_dir / "acp_model.acph5",
        "composite_definitions": out_dir / "ACPCompositeDefinitions.h5",
        "materials_xml": out_dir / "materials.xml",
    }
    model.save(files["acp_model"])
    model.export_shell_composite_definitions(files["composite_definitions"])
    model.export_materials(files["materials_xml"])
    return files
