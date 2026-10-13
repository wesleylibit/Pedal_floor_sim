"""Stage 1: build the panel mid-surface and position the pedal box with PyAnsys Geometry.

Global axes follow the sketch: origin at the middle of the rear edge, X toward the
front edge, Y to the right, Z into the page. The panel reference surface is z = 0,
which is its back (nut-side) face; the laminate builds toward -Z, so the face
against the pedal box is at z = -t_panel.

Contacts between shells here work on the reference surfaces (Mechanical only allows
shell-thickness contact for mid-plane sections, and the ACP layup is offset), so each
body is placed with its reference surface near the interface it touches, but never
in the same plane (surfaces lying in the panel plane leave the panel faces under them
unmeshed):
- the nut-side washers (rough contact, carries shear) have their mid-plane half a
  washer thickness behind the panel's back face;
- the pedal box (frictionless, normal force only) has its surface at its head face,
  z = -(t_panel + pedalbox_thickness), and its thickness builds back up to the panel.
"""

from __future__ import annotations

import collections
import math
import pathlib

from ansys.geometry.core import launch_modeler
from ansys.geometry.core.math import Plane, Point2D, Point3D, UNITVECTOR3D_X, UNITVECTOR3D_Y, UNITVECTOR3D_Z
from ansys.geometry.core.misc import DEFAULT_UNITS, UNITS
from ansys.geometry.core.shapes.curves import Circle
from ansys.geometry.core.sketch import Sketch

from .config import ANSYS_VERSION, INPUTS, half_width, require_ansys, slot_centre_x

DEFAULT_UNITS.LENGTH = UNITS.mm
TOL = 1e-3  # mm

# Named selections carry through Mechanical (loads, contact, bolts, mesh controls)
# and into ACP (element sets for the layup).
NS_PANEL = "NS_PANEL"
NS_CORE = "NS_CORE"
NS_HARDPOINTS = "NS_HARDPOINTS"
NS_EDGES = {"front": "NS_EDGE_FRONT", "rear": "NS_EDGE_REAR", "left": "NS_EDGE_LEFT", "right": "NS_EDGE_RIGHT"}
SUPPORTED_EDGES = ("NS_EDGE_FRONT", "NS_EDGE_LEFT", "NS_EDGE_RIGHT")
NS_SLOTS = ("NS_SLOT_L", "NS_SLOT_R")
NS_PEDALBOX = "NS_PEDALBOX"
NS_PEDALBOX_LOAD = "NS_PEDALBOX_LOAD"  # pedal box faces outside the washer patches
NS_PEDALBOX_OUTSIDE_JOINTS = "NS_PEDALBOX_OUTSIDE_JOINTS"  # pedal box faces outside both joint zones
PANEL_BODY, PEDALBOX_BODY = "panel", "pedalbox"


def ns_panel_washer(i: int) -> str:
    return f"NS_PANEL_WASHER_{i}"


def ns_pedalbox_washer(i: int) -> str:
    return f"NS_PEDALBOX_WASHER_{i}"


def washer_body(i: int) -> str:
    return f"washer_{i}"


def ns_washer(i: int) -> str:
    """All faces of the nut-side washer body of bolt i."""
    return f"NS_WASHER_{i}"


def ns_washer_nut(i: int) -> str:
    """The nut's bearing footprint on washer i (the beam's panel-side end)."""
    return f"NS_WASHER_NUT_{i}"


def ns_joint_zone(i: int) -> str:
    """Pedal box faces within joint_zone_radius of bolt i (washer patch included)."""
    return f"NS_JOINT_ZONE_{i}"


def launch():
    """Start a geometry backend: SpaceClaim, else Discovery, else the Geometry core service."""
    require_ansys()
    errors = []
    for mode, extra in (("spaceclaim", {"hidden": True}), ("discovery", {"hidden": True}), ("core_service", {})):
        try:
            return launch_modeler(mode=mode, version=ANSYS_VERSION, **extra)
        except Exception as exc:
            errors.append(f"{mode}: {exc}")
    raise RuntimeError("Could not start a PyAnsys Geometry backend:\n" + "\n".join(errors))


def _bbox_mm(entity) -> tuple[float, float, float, float]:
    bb = entity.bounding_box
    return (
        bb.min_corner.x.m_as(UNITS.mm), bb.min_corner.y.m_as(UNITS.mm),
        bb.max_corner.x.m_as(UNITS.mm), bb.max_corner.y.m_as(UNITS.mm),
    )


def _within(bbox, x0: float, y0: float, x1: float, y1: float) -> bool:
    return bbox[0] > x0 - TOL and bbox[1] > y0 - TOL and bbox[2] < x1 + TOL and bbox[3] < y1 + TOL


def _within_circle_box(bbox, x: float, y: float, r: float) -> bool:
    return _within(bbox, x - r, y - r, x + r, y + r)


def _on_side(bbox, p: dict, side: int) -> bool:
    """Whether an edge (by its bounding box) lies on the left (side -1) or right (+1) edge
    line of the trapezoid, y = side * half_width(x)."""
    x0, y0, x1, y1 = bbox
    y_at_x0, y_at_x1 = (y0, y1) if (side < 0) == (p["front_width"] <= p["rear_width"]) else (y1, y0)
    return abs(y_at_x0 - side * half_width(p, x0)) < TOL and abs(y_at_x1 - side * half_width(p, x1)) < TOL


def _stadium(sketch: Sketch, p: dict, y: float, radius: float) -> None:
    # Sketch.slot takes the overall tip-to-tip length and the full width.
    sketch.slot(Point2D([slot_centre_x(p), y]), p["slot_length"] + 2 * radius, 2 * radius)


def _hardpoint_outline(p: dict, y: float, r: float) -> Sketch:
    """The hardpoint stadium as its four curves, for imprinting on the panel.

    The imprint keeps only the parts of the curves that lie on the panel, which trims a
    hardpoint that reaches past the panel edges. A curve wholly off the panel makes the
    imprint fail, so those are left out."""
    x0, x1 = p["hole_rear_x"], p["hole_rear_x"] + p["slot_length"]
    t = [i / 100 for i in range(101)]
    curves = [  # (sketch call, points along the curve)
        (lambda s: s.segment(Point2D([x0, y - r]), Point2D([x1, y - r])), [(x0 + (x1 - x0) * u, y - r) for u in t]),
        (lambda s: s.arc(Point2D([x1, y - r]), Point2D([x1, y + r]), Point2D([x1, y])),  # front end
         [(x1 + r * math.sin(math.pi * u), y - r * math.cos(math.pi * u)) for u in t]),
        (lambda s: s.segment(Point2D([x1, y + r]), Point2D([x0, y + r])), [(x1 - (x1 - x0) * u, y + r) for u in t]),
        (lambda s: s.arc(Point2D([x0, y + r]), Point2D([x0, y - r]), Point2D([x0, y])),  # rear end
         [(x0 - r * math.sin(math.pi * u), y + r * math.cos(math.pi * u)) for u in t]),
    ]
    sketch = Sketch()
    for add, points in curves:
        if any(TOL < px < p["panel_length"] - TOL and abs(py) < half_width(p, px) - TOL for px, py in points):
            add(sketch)
    return sketch


def find_holes(modeler, cad_path: pathlib.Path) -> tuple[list[tuple[float, float]], float]:
    """Centres (mm, CAD coordinates, sorted rear to front) and radius of the full circular holes."""
    design = modeler.create_design("pedalbox_holes")
    try:
        body = design.insert_file(cad_path).bodies[0]
        # A hole is a closed circle, usually split into arcs. Group the arcs by their
        # circle and keep the groups whose arcs add up to a full circumference.
        groups = collections.defaultdict(list)
        for edge in body.edges:
            geometry = edge.shape.geometry
            if isinstance(geometry, Circle):
                key = tuple(round(v.m, 9) for v in (geometry.origin.x, geometry.origin.y, geometry.radius))
                groups[key].append(edge)
        holes, radii = [], []
        for edges in groups.values():
            boxes = [_bbox_mm(e) for e in edges]
            x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
            x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
            length = sum(e.length.m_as(UNITS.mm) for e in edges)
            if abs(length - math.pi * (x1 - x0)) < 0.01 * length:
                holes.append(((x0 + x1) / 2, (y0 + y1) / 2))
                radii.append((x1 - x0) / 2)
    finally:
        design.close()
    if len(holes) != 2:
        raise RuntimeError(f"Expected 2 bolt holes in {cad_path.name}, found {len(holes)}")
    holes.sort()
    if abs(holes[0][1] - holes[1][1]) > 0.01:
        raise RuntimeError(f"The pedal box holes are not aligned with X: {holes}")
    return holes, radii[0]


def build_model(modeler, p: dict, offset: float, holes: list[tuple[float, float]], t_panel: float, out_dir: pathlib.Path) -> dict:
    """Create the panel and the positioned pedal box, export them as .pmdb, and return
    the file path, the bolt positions and the panel face areas (all mm)."""
    length, rear, front = p["panel_length"], p["rear_width"], p["front_width"]
    y_left, r_hp = p["hole_rear_y"], p["hardpoint_rad"]
    # Top washer: bolt head on the pedal box. Bottom washer: nut on the panel.
    r_top, r_bottom = p["washer_diameter_top"] / 2, p["washer_diameter_bottom"] / 2
    design = modeler.create_design(f"case_{p['case_id']:03d}_geometry")

    # Panel: trapezoid with both slot holes cut out, then the left hardpoint footprint imprinted.
    outline = Sketch()
    _stadium(outline, p, y_left, p["hole_rad"])
    _stadium(outline, p, -y_left, p["hole_rad"])
    corners = [(0, -rear / 2), (0, rear / 2), (length, front / 2), (length, -front / 2)]
    outline.segment(Point2D(corners[0]), Point2D(corners[1]))
    for corner in corners[2:] + corners[:1]:
        outline.segment_to_point(Point2D(corner))
    panel = design.create_surface(PANEL_BODY, outline, merge_inner_loops=True)
    # Trimmed at the panel edges where it reaches past them.
    panel.imprint_curves(faces=panel.faces, sketch=_hardpoint_outline(p, y_left, r_hp))

    # Pedal box: rear hole onto the rear end of the left slot, moved forward by `offset`,
    # and down to its head face; Mechanical builds its thickness back up to the panel (z = -t_panel).
    z_pb = -(t_panel + p["pedalbox_thickness"])
    pedalbox = design.insert_file(INPUTS / p["pedalbox_cad"]).bodies[0]
    pedalbox.set_name(PEDALBOX_BODY)
    pedalbox.translate(UNITVECTOR3D_X, p["hole_rear_x"] + offset - holes[0][0])
    pedalbox.translate(UNITVECTOR3D_Y, y_left - holes[0][1])
    pedalbox.translate(UNITVECTOR3D_Z, z_pb)
    bolts = [(p["hole_rear_x"] + offset + hx - holes[0][0], y_left + hy - holes[0][1]) for hx, hy in holes]
    x0, y0, x1, y1 = _bbox_mm(pedalbox)
    if x0 <= 0 or x1 >= length or max(abs(y0), abs(y1)) >= min(half_width(p, x0), half_width(p, x1)):
        raise ValueError(f"Pedal box at offset {offset / 25.4:.2f} in sticks out of the panel")

    # Bolt head / nut contact patches on both surfaces, and around them on the pedal box
    # the joint zones whose contact force is added to the bolt force (net joint force).
    r_joint = p["joint_zone_radius"]
    spacing = bolts[1][0] - bolts[0][0]
    if not r_top < r_joint < spacing / 2:
        raise ValueError("joint_zone_radius must be larger than the top washer radius and smaller than half the bolt spacing")
    if 2 * r_bottom >= spacing:
        raise ValueError("washer_diameter_bottom must be smaller than the bolt spacing (the two washers would overlap)")
    on_panel = Sketch()
    on_pedalbox = Sketch(Plane(origin=Point3D([0, 0, z_pb]), direction_x=UNITVECTOR3D_X, direction_y=UNITVECTOR3D_Y))
    for x, y in bolts:
        on_panel.circle(Point2D([x, y]), r_bottom)
        on_pedalbox.circle(Point2D([x, y]), r_top)
        on_pedalbox.circle(Point2D([x, y]), r_joint)
    panel.imprint_curves(faces=panel.faces, sketch=on_panel)
    pedalbox.imprint_curves(faces=pedalbox.faces, sketch=on_pedalbox)

    # Nut-side washers: separate annular bodies, mid-plane at z = t_w / 2 so their
    # thickness sits on the panel's back face (z = 0). The nut bears on the inner
    # footprint, which the beam is tied to; the rest of the washer is free to bend.
    t_w, r_nut, r_bolt = p["washer_thickness"], p["nut_bearing_diameter"] / 2, p["bolt_diameter"] / 2
    washer_plane = Plane(origin=Point3D([0, 0, t_w / 2]), direction_x=UNITVECTOR3D_X, direction_y=UNITVECTOR3D_Y)
    washers, nut_patches = [], []
    for i, (x, y) in enumerate(bolts, 1):
        ring = Sketch(washer_plane)
        ring.circle(Point2D([x, y]), r_bottom)
        ring.circle(Point2D([x, y]), r_bolt)
        washer = design.create_surface(washer_body(i), ring, merge_inner_loops=True)
        if r_nut < r_bottom - TOL:
            nut = Sketch(washer_plane)
            nut.circle(Point2D([x, y]), r_nut)
            washer.imprint_curves(faces=washer.faces, sketch=nut)
        washers.append(washer)
        nut_patches.append([f for f in washer.faces if _within_circle_box(_bbox_mm(f), x, y, min(r_nut, r_bottom))])

    hp_box = (p["hole_rear_x"] - r_hp, y_left - r_hp, p["hole_rear_x"] + p["slot_length"] + r_hp, y_left + r_hp)
    core_faces, hardpoint_faces = [], []
    for face in panel.faces:
        (hardpoint_faces if _within(_bbox_mm(face), *hp_box) else core_faces).append(face)
    panel_washers = [[f for f in panel.faces if _within_circle_box(_bbox_mm(f), x, y, r_bottom)] for x, y in bolts]
    pedalbox_washers = [[f for f in pedalbox.faces if _within_circle_box(_bbox_mm(f), x, y, r_top)] for x, y in bolts]
    joint_zones = [[f for f in pedalbox.faces if _within_circle_box(_bbox_mm(f), x, y, r_joint)] for x, y in bolts]
    zone_ids = {f.id for faces in joint_zones for f in faces}
    outside_zones = [f for f in pedalbox.faces if f.id not in zone_ids]
    if not core_faces or not hardpoint_faces or not all(panel_washers) or not all(pedalbox_washers) or not all(joint_zones) or not outside_zones or not all(nut_patches):
        raise RuntimeError("Could not identify the core, hardpoint, washer, nut or joint-zone faces after imprinting")

    # Outer edges by the line they lie on: a hardpoint trimmed at an edge splits it into pieces.
    edges = {key: [] for key in NS_EDGES} | {name: [] for name in NS_SLOTS}
    for edge in panel.edges:
        bbox = _bbox_mm(edge)
        if bbox[2] < TOL:
            edges["rear"].append(edge)
        elif bbox[0] > length - TOL:
            edges["front"].append(edge)
        elif _on_side(bbox, p, -1):
            edges["left"].append(edge)
        elif _on_side(bbox, p, 1):
            edges["right"].append(edge)
        else:
            for name, y in zip(NS_SLOTS, (y_left, -y_left)):
                if _within(bbox, 0, y - p["hole_rad"], length, y + p["hole_rad"]):
                    edges[name].append(edge)
    for key, found in edges.items():
        if not found:
            raise RuntimeError(f"No edges found for '{key}'")

    design.create_named_selection(NS_PANEL, faces=panel.faces)
    design.create_named_selection(NS_CORE, faces=core_faces)
    design.create_named_selection(NS_HARDPOINTS, faces=hardpoint_faces)
    design.create_named_selection(NS_PEDALBOX, faces=pedalbox.faces)
    washer_ids = {f.id for faces in pedalbox_washers for f in faces}
    design.create_named_selection(NS_PEDALBOX_LOAD, faces=[f for f in pedalbox.faces if f.id not in washer_ids])
    design.create_named_selection(NS_PEDALBOX_OUTSIDE_JOINTS, faces=outside_zones)
    for i, (on_panel_faces, on_pedalbox_faces, zone) in enumerate(zip(panel_washers, pedalbox_washers, joint_zones), 1):
        design.create_named_selection(ns_panel_washer(i), faces=on_panel_faces)
        design.create_named_selection(ns_pedalbox_washer(i), faces=on_pedalbox_faces)
        design.create_named_selection(ns_joint_zone(i), faces=zone)
    for i, (washer, nut_faces) in enumerate(zip(washers, nut_patches), 1):
        design.create_named_selection(ns_washer(i), faces=washer.faces)
        design.create_named_selection(ns_washer_nut(i), faces=nut_faces)
    for key, name in NS_EDGES.items():
        design.create_named_selection(name, edges=edges[key])
    for name in NS_SLOTS:
        design.create_named_selection(name, edges=edges[name])

    out_dir.mkdir(parents=True, exist_ok=True)
    path = design.export_to_pmdb(out_dir)
    result = {
        "geometry_path": pathlib.Path(path),
        "bolts": bolts,
        "pedalbox_centre": ((x0 + x1) / 2, (y0 + y1) / 2),
        "panel_thickness_mm": t_panel,
        "area_core_mm2": sum(f.area.m_as(UNITS.mm**2) for f in core_faces),
        "area_hardpoint_mm2": sum(f.area.m_as(UNITS.mm**2) for f in hardpoint_faces),
        "area_pedalbox_mm2": sum(f.area.m_as(UNITS.mm**2) for f in pedalbox.faces),
        # Lower washer's contact area on the panel per bolt (washer circle minus the slot): the crush area.
        "panel_washer_areas_mm2": [sum(f.area.m_as(UNITS.mm**2) for f in faces) for faces in panel_washers],
    }
    design.close()
    return result
