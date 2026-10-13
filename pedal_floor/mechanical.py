"""Stages 2 and 4: mesh in Mechanical, then import the ACP layup, connect the pedal box, and solve.

Mechanical runs as a separate process; each script below is IronPython executed
inside it. Everything is scoped to named selections created in geometry.py.

Load steps: 1 = bolt pretension, 2 = bolts locked and the pedal load applied.
"""

from __future__ import annotations

import json
import logging
import pathlib
import textwrap

from ansys.acp.core.mechanical_integration_helpers import export_mesh_for_acp, import_acp_composite_definitions
from ansys.mechanical.core import launch_mechanical

from . import checks
from .config import ANSYS_VERSION, core_layer, require_ansys
from .geometry import (
    NS_CORE, NS_HARDPOINTS, NS_PANEL, NS_PEDALBOX, NS_PEDALBOX_LOAD, NS_PEDALBOX_OUTSIDE_JOINTS, NS_SLOTS, PANEL_BODY, PEDALBOX_BODY,
    SUPPORTED_EDGES, ns_joint_zone, ns_panel_washer, ns_pedalbox_washer, ns_washer, ns_washer_nut, washer_body,
)

log = logging.getLogger("pedal_floor")


def launch(batch: bool = True):
    require_ansys()
    try:
        return launch_mechanical(batch=batch, version=ANSYS_VERSION, cleanup_on_exit=True)
    except Exception as exc:
        if "secure transport" not in str(exc):
            raise
        # 2025 R2 before SP03 has no secure gRPC transport; this session only listens on localhost.
        log.warning("Mechanical %s has no secure gRPC; using the local insecure transport.", ANSYS_VERSION)
        return launch_mechanical(batch=batch, version=ANSYS_VERSION, cleanup_on_exit=True, transport_mode="insecure")


def _run(mech, step: str, script: str) -> str:
    """Run one named step; variables persist in Mechanical between steps."""
    try:
        return mech.run_python_script(textwrap.dedent(script))
    except Exception as exc:
        details = exc.details() if callable(getattr(exc, "details", None)) else str(exc)
        raise RuntimeError(f"Mechanical step '{step}' failed: {details}") from None


def _messages(mech) -> str:
    """Mechanical's error and warning messages, newest last."""
    return _run(mech, "messages", """
        "\\n".join("%s: %s" % (m.Severity, m.DisplayString) for m in ExtAPI.Application.Messages if str(m.Severity) in ("Error", "Warning"))
    """)


def mesh(mech, p: dict, geometry_path: pathlib.Path, mechdb_path: pathlib.Path, acp_mesh_path: pathlib.Path) -> dict:
    """Import and mesh the model in a freshly launched Mechanical, save it, and export the mesh for ACP."""
    quality = p["mesh_quality"]
    local = [NS_HARDPOINTS, *NS_SLOTS] + [f(i) for i in (1, 2) for f in (ns_panel_washer, ns_pedalbox_washer, ns_washer)]
    _run(mech, "import geometry", f"""
        ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardNMM
        geometry_import = Model.GeometryImportGroup.AddGeometryImport()
        prefs = Ansys.ACT.Mechanical.Utilities.GeometryImportPreferences()
        prefs.ProcessNamedSelections = True
        prefs.NamedSelectionKey = ""
        geometry_import.Import({str(geometry_path)!r}, Ansys.Mechanical.DataModel.Enums.GeometryImportPreference.Format.Automatic, prefs)
    """)
    _run(mech, "add analysis", "analysis = Model.AddStaticStructuralAnalysis()")
    # The pedal box surface is its head face (z = -(t_panel + thickness), normal +Z); a
    # Bottom offset builds the plate up to the panel's pedal box face at z = -t_panel.
    _run(mech, "pedal box thickness", f"""
        pedalbox = [b for b in Model.Geometry.GetChildren(DataModelObjectCategory.Body, True) if b.Name.endswith({PEDALBOX_BODY!r})][0]
        pedalbox.Thickness = Quantity({float(p['pedalbox_thickness'])!r}, "mm")
        pedalbox.ShellOffsetType = ShellOffsetType.Bottom
    """)
    # Nut-side washers: mid-surface shells (required for shell-thickness contact).
    _run(mech, "washer thickness", f"""
        washers = [b for b in Model.Geometry.GetChildren(DataModelObjectCategory.Body, True)
                   if b.Name.endswith({washer_body(1)!r}) or b.Name.endswith({washer_body(2)!r})]
        for washer in washers:
            washer.Thickness = Quantity({float(p['washer_thickness'])!r}, "mm")
            washer.ShellOffsetType = ShellOffsetType.Middle
        len(washers)
    """)
    _run(mech, "mesh element order", "Model.Mesh.ElementOrder = ElementOrder.Linear")
    _run(mech, "mesh element size", f'Model.Mesh.ElementSize = Quantity({float(p["mesh_global_" + quality])!r}, "mm")')
    _run(mech, "local sizing", f"""
        ns = dict((n.Name, n) for n in Model.NamedSelections.Children)
        sizing = Model.Mesh.AddSizing()
        sizing.Location = ns[{NS_PEDALBOX!r}]
        sizing.ElementSize = Quantity({float(p['mesh_pedalbox_' + quality])!r}, "mm")
        for name in {local!r}:
            sizing = Model.Mesh.AddSizing()
            sizing.Location = ns[name]
            sizing.ElementSize = Quantity({float(p['mesh_local_' + quality])!r}, "mm")
    """)
    _run(mech, "generate mesh", "Model.Mesh.GenerateMesh()")
    # Saving first moves the project directory next to the .mechdb, so the
    # ACP files imported later are stored with this case.
    _run(mech, "save", f"ExtAPI.DataModel.Project.Save({str(mechdb_path)!r})")
    out = _run(mech, "mesh size", '"%d,%d" % (Model.Mesh.Nodes, Model.Mesh.Elements)')
    n_nodes, n_elements = (int(v) for v in out.split(","))
    export_mesh_for_acp(mechanical=mech, path=acp_mesh_path)
    return {"n_nodes": n_nodes, "n_elements": n_elements}


def solve(mech, p: dict, geo: dict, materials_xml: pathlib.Path, composite_definitions: pathlib.Path, image_dir: pathlib.Path,
          allowables: dict[str, tuple[float, float]]) -> dict:
    """Import the layup, connect the pedal box, apply supports and loads, solve, and read results.

    ``allowables`` holds the XZ/YZ shear allowables per region (checks.shear_allowables)
    for the shear interaction results."""
    _run(mech, "import materials", f"Model.Materials.Import({str(materials_xml)!r})")
    import_acp_composite_definitions(mechanical=mech, path=composite_definitions)
    _run(mech, "body materials", f"""
        pedalbox.Material = {p['pedalbox_material']!r}
        for washer in washers:
            washer.Material = {p['washer_material']!r}
    """)

    # Contacts work between the shell reference surfaces (Mechanical only allows
    # shell-thickness contact for mid-plane sections, and the ACP layup is offset), with
    # Adjust to Touch closing the offset between them. Here the pedal box surface (head
    # face) is the panel plus plate thickness away from the panel's reference surface
    # (back face). Frictionless contact carries only Z forces, so that offset adds no moment.
    # Both normals are +Z: the pedal box contacts with its top side, the panel with its bottom.
    # The pedal box side is split into the two joint zones and the rest, so the contact
    # force near each bolt can be added to the bolt force (net joint force on the panel).
    zones = [ns_joint_zone(1), ns_joint_zone(2), NS_PEDALBOX_OUTSIDE_JOINTS]
    _run(mech, "contact", f"""
        connections = Model.Connections if Model.Connections else Model.AddConnections()
        for group in list(connections.Children):
            group.Delete()  # Mechanical auto-detects a bonded contact between the two surfaces
        group = connections.AddConnectionGroup()
        contacts = []
        for source, label in zip({zones!r}, ["joint 1 zone", "joint 2 zone", "outside joints"]):
            contact = group.AddContactRegion()
            contact.Name = "Pedal box to panel, " + label + " (frictionless)"
            contact.SourceLocation = ns[source]
            contact.TargetLocation = ns[{NS_PANEL!r}]
            contact.ContactType = ContactType.Frictionless
            contact.Behavior = ContactBehavior.Asymmetric
            contact.ContactShellFace = ShellFaceType.Top
            contact.TargetShellFace = ShellFaceType.Bottom
            contact.PinballRegion = ContactPinballType.Radius
            contact.PinballRadius = Quantity({1.5 * (geo['panel_thickness_mm'] + p['pedalbox_thickness'])!r}, "mm")
            contact.InterfaceTreatment = ContactInitialEffect.AdjustToTouch
            contacts.append(contact)
    """)
    # Nut-side washers: rough contact (no sliding, can separate) with their footprint on
    # the panel's back face. Rough contact also carries shear; the washer mid-plane is only
    # half a washer thickness from the panel's reference surface (its back face), so the
    # shear acts close to the real interface. Washer (normal +Z) touches with its bottom
    # side, the panel with its top side.
    pairs = [(ns_washer(i), ns_panel_washer(i)) for i in (1, 2)]
    _run(mech, "washer contact", f"""
        washer_contacts = []
        for i, (source, target) in enumerate({pairs!r}, 1):
            contact = group.AddContactRegion()
            contact.Name = "Washer %d to panel (rough)" % i
            contact.SourceLocation = ns[source]
            contact.TargetLocation = ns[target]
            contact.ContactType = ContactType.Rough
            contact.Behavior = ContactBehavior.Asymmetric
            contact.ContactShellFace = ShellFaceType.Bottom
            contact.TargetShellFace = ShellFaceType.Top
            contact.PinballRegion = ContactPinballType.Radius
            contact.PinballRadius = Quantity({2.0 * p['washer_thickness']!r}, "mm")
            contact.InterfaceTreatment = ContactInitialEffect.AdjustToTouch
            washer_contacts.append(contact)
    """)

    # Bolt ends at the nut face (back of the washer, +Z) and the head face (the pedal box
    # surface), so the beam has the real grip length. Rigid spiders tie them to the nut's
    # footprint on the washer and the head's patch on the pedal box.
    z_nut, z_head = p["washer_thickness"], -(geo["panel_thickness_mm"] + p["pedalbox_thickness"])
    for i, (x, y) in enumerate(geo["bolts"], 1):
        _run(mech, f"bolt {i}", f"""
            beam = connections.AddBeam()
            beam.Name = "Bolt {i}"
            beam.ReferenceLocation = ns[{ns_washer_nut(i)!r}]
            beam.MobileLocation = ns[{ns_pedalbox_washer(i)!r}]
            beam.ReferenceBehavior = LoadBehavior.Rigid
            beam.MobileBehavior = LoadBehavior.Rigid
            beam.Radius = Quantity({p['bolt_diameter'] / 2!r}, "mm")
            beam.Material = {p['bolt_material']!r}
            # Ends on the bolt axis (the head patch is cut by the slot, so its centroid is off-axis).
            beam.ReferenceXCoordinate = Quantity({x!r}, "mm")
            beam.ReferenceYCoordinate = Quantity({y!r}, "mm")
            beam.ReferenceZCoordinate = Quantity({z_nut!r}, "mm")
            beam.MobileXCoordinate = Quantity({x!r}, "mm")
            beam.MobileYCoordinate = Quantity({y!r}, "mm")
            beam.MobileZCoordinate = Quantity({z_head!r}, "mm")
            bolt_{i} = beam
        """)

    support = "AddFixedSupport" if p["edge_bc"] == "fixed" else "AddSimplySupported"
    _run(mech, "supports", f"""
        for name in {list(SUPPORTED_EDGES)!r}:
            bc = analysis.{support}()
            bc.Location = ns[name]
            bc.Name = name.replace("NS_EDGE_", "") + " {p['edge_bc']}"
    """)
    _run(mech, "analysis settings", """
        analysis.AnalysisSettings.NumberOfSteps = 2
        analysis.AnalysisSettings.NodalForces = OutputControlsNodalForcesType.Yes  # needed for the bolt reaction probes
        analysis.AnalysisSettings.ContactMiscellaneous = True  # contact element forces for the joint-zone reactions
    """)
    _run(mech, "pretension", f"""
        for beam in [bolt_1, bolt_2]:
            pretension = analysis.AddBoltPretension()
            # 2025 R2 has no public API for scoping a pretension to a beam connection,
            # so set the "Scoping Method: Beam Connection" through the internal object.
            pretension.InternalObject.GeometryDefineBy = int(GeometryDefineByType.BeamConnection)
            pretension.InternalObject.BeamConnectionSelection = beam.ObjectId
            pretension.Name = beam.Name + " pretension"
            pretension.SetDefineBy(1, BoltLoadDefineBy.Load)
            pretension.Preload.Output.SetDiscreteValue(0, Quantity({float(p['bolt_preload'])!r}, "N"))
            pretension.SetDefineBy(2, BoltLoadDefineBy.Lock)
    """)
    x_load, y_load = geo["bolts"][0]
    behavior = "Rigid" if p["load_behavior"] == "rigid" else "Deformable"
    _run(mech, "pedal load", f"""
        force = analysis.AddRemoteForce()
        force.Name = "Pedal load"
        # Outside the washer patches, which already carry the bolts' rigid spiders.
        force.Location = ns[{NS_PEDALBOX_LOAD!r}]
        force.Behavior = LoadBehavior.{behavior}
        force.XCoordinate = Quantity({x_load!r}, "mm")
        force.YCoordinate = Quantity({y_load!r}, "mm")
        force.ZCoordinate = Quantity({-(geo['panel_thickness_mm'] + p['load_height'])!r}, "mm")  # above the pedal box mounting face
        force.DefineBy = LoadDefineBy.Components
        times = [Quantity(0, "s"), Quantity(1, "s"), Quantity(2, "s")]
        for component, value in [(force.XComponent, {float(p['load_fx'])!r}), (force.YComponent, {float(p['load_fy'])!r}), (force.ZComponent, {float(p['load_fz'])!r})]:
            component.Inputs[0].DiscreteValues = times
            component.Output.DiscreteValues = [Quantity(0, "N"), Quantity(0, "N"), Quantity(value, "N")]
    """)
    washer_faces = [ns_washer(1), ns_washer(2)]
    status = _run(mech, "solve", f"""
        solution = analysis.Solution
        total = solution.AddTotalDeformation()
        uz = solution.AddDirectionalDeformation()
        uz.NormalOrientation = NormalOrientationType.ZAxis
        uz.Name = "Directional Deformation Z"

        def contact_pressure(name, ns_names):
            # A contact tool scoped by geometry covers the contact regions on those faces.
            selection = ExtAPI.SelectionManager.CreateSelectionInfo(SelectionTypeEnum.GeometryEntities)
            selection.Ids = [i for n in ns_names for i in ns[n].Location.Ids]
            tool = solution.AddContactTool()
            tool.Name = name
            tool.ScopingMethod = GeometryDefineByType.Geometry
            tool.Location = selection
            return tool.AddPressure()

        pressure = contact_pressure("Contact tool, pedal box", [{NS_PEDALBOX!r}])
        washer_pressure = contact_pressure("Contact tool, washers", {washer_faces!r})
        reactions, probes = [], []
        for beam in [bolt_1, bolt_2]:
            reaction = solution.AddForceReaction()
            reaction.LocationMethod = LocationDefinitionMethod.Beam
            reaction.BeamSelection = beam
            reaction.Name = beam.Name + " reaction"
            reactions.append(reaction)
            probe = solution.AddBeamProbe()
            probe.BoundaryConditionSelection = beam
            probe.Name = beam.Name + " beam probe"
            probes.append(probe)
        contact_reactions = []
        for contact in contacts:
            reaction = solution.AddForceReaction()
            reaction.LocationMethod = LocationDefinitionMethod.ContactRegion
            reaction.ContactRegionSelection = contact
            # Sum the contact forces themselves, not the underlying elements' nodal forces.
            reaction.Extraction = ProbeExtractionType.ContactElements
            reaction.Name = contact.Name.replace(" (frictionless)", "") + " reaction"
            contact_reactions.append(reaction)
        analysis.Solve(True)
        ExtAPI.DataModel.Project.Save()  # keep the solver files even if the solve failed
        str(solution.Status)
    """)
    if status != "Done":
        raise RuntimeError(f"Mechanical solution status is '{status}'. Messages:\n{_messages(mech)}")

    out = _run(mech, "read results", """
        values = [total.Maximum.Value, uz.Minimum.Value, uz.Maximum.Value, pressure.Maximum.Value]
        for reaction, probe in zip(reactions, probes):
            values += [reaction.XAxis.Value, reaction.YAxis.Value, reaction.ZAxis.Value, reaction.Total.Value, probe.AxialForce.Value]
        values += [reaction.ZAxis.Value for reaction in contact_reactions]
        # Z forces at the end of step 1 (preload only), as a check of the joint split.
        z_reactions = reactions + contact_reactions
        for reaction in z_reactions:
            reaction.DisplayTime = Quantity(1, "s")
        solution.EvaluateAllResults()
        values += [reaction.ZAxis.Value for reaction in z_reactions]
        for reaction in z_reactions:
            reaction.DisplayTime = Quantity(2, "s")
        solution.EvaluateAllResults()
        values.append(washer_pressure.Maximum.Value)
        ",".join(str(v) for v in values)
    """)
    # Stresses in the core/hardpoint layer for the strength checks (checks.py): max |value|
    # over both load steps and over the layer's middle and top/bottom. The result objects
    # stay in the tree (at the final step) so they can be viewed in Mechanical.
    layer = core_layer(p)
    # Quadratic XZ/YZ shear interaction, evaluated point by point as a user-defined result.
    interactions = [
        (region, ns_name, label, checks.interaction_expression(allowables[region]))
        for region, ns_name, label in (("core", NS_CORE, "Core"), ("hardpoint", NS_HARDPOINTS, "Hardpoint"))
        if region in allowables
    ]
    stress_out = _run(mech, "core layer stresses", f"""
        def layer_result(kind, region, label, orientation=None):
            result = solution.AddShearStress() if kind == "shear" else solution.AddEquivalentStress()
            if orientation is not None:
                result.ShearOrientation = orientation
            result.Location = ns[region]
            result.Layer = {layer}
            result.Name = label + " (layer {layer})"
            return result

        def interaction_result(region, label, expression):
            result = solution.AddUserDefinedResult()
            result.Location = ns[region]
            result.Expression = expression
            result.Layer = {layer}
            result.Name = label + " shear interaction index (layer {layer})"
            return result

        def value(quantity):
            return quantity.Value if hasattr(quantity, "Value") else float(quantity)

        def max_abs(result):
            values = []
            for time in (1, 2):
                result.DisplayTime = Quantity(time, "s")
                for position in (ShellFaceType.Middle, ShellFaceType.TopAndBottom):
                    result.Position = position
                    result.EvaluateAllResults()
                    values += [abs(value(result.Minimum)), abs(value(result.Maximum))]
            result.DisplayTime = Quantity(2, "s")
            result.Position = ShellFaceType.Middle
            result.EvaluateAllResults()
            return max(values)

        stress_results = [
            layer_result("shear", {NS_CORE!r}, "Core shear XZ", ShearOrientationType.XZPlane),
            layer_result("shear", {NS_CORE!r}, "Core shear YZ", ShearOrientationType.YZPlane),
            layer_result("shear", {NS_HARDPOINTS!r}, "Hardpoint shear XZ", ShearOrientationType.XZPlane),
            layer_result("shear", {NS_HARDPOINTS!r}, "Hardpoint shear YZ", ShearOrientationType.YZPlane),
            layer_result("vm", {NS_HARDPOINTS!r}, "Hardpoint von Mises"),
        ]
        for ns_name, label, expression in {[i[1:] for i in interactions]!r}:
            stress_results.append(interaction_result(ns_name, label, expression))
        ",".join(str(max_abs(r)) for r in stress_results)
    """)
    export_result_images(mech, image_dir, p["contact_plot_max"], p["result_units"])
    _run(mech, "save", "ExtAPI.DataModel.Project.Save()")
    v = [float(x) for x in out.split(",")]
    result = {
        "max_total_def_mm": v[0],
        "max_abs_uz_mm": max(abs(v[1]), abs(v[2])),
        "max_contact_pressure_MPa": v[3],
        "max_washer_contact_pressure_MPa": v[22],
    }
    for i in (1, 2):
        fx, fy, fz, ftot, axial = v[4 + 5 * (i - 1): 9 + 5 * (i - 1)]
        result |= {f"bolt{i}_Fx_N": fx, f"bolt{i}_Fy_N": fy, f"bolt{i}_Fz_N": fz, f"bolt{i}_Ftotal_N": ftot, f"bolt{i}_axial_N": axial}
    # Net out-of-plane force each joint puts on the panel: bolt force plus the contact
    # force inside that joint's zone. Bolt reactions are forces on the panel; contact
    # reactions come with the opposite sign, hence the minus. Sign: +Z pushes the panel
    # away from the pedal box (bearing), -Z pulls it toward the pedal box.
    # Values: step 2 bolt Fz at v[6], v[11] and zone contact Fz at v[14:17];
    # step 1 (preload only) bolt Fz at v[17:19] and zone contact Fz at v[19:22].
    for suffix, bolt_z, contact_z in (("", (v[6], v[11]), v[14:17]), ("_preload", v[17:19], v[19:22])):
        result |= {
            f"joint1_net_Fz{suffix}_N": bolt_z[0] - contact_z[0],
            f"joint2_net_Fz{suffix}_N": bolt_z[1] - contact_z[1],
            f"outside_joints_contact_Fz{suffix}_N": -contact_z[2],
        }
    # Should equal the applied Z load (load_fz); a check of the split.
    result["Fz_balance_N"] = result["joint1_net_Fz_N"] + result["joint2_net_Fz_N"] + result["outside_joints_contact_Fz_N"]
    # Bolt axial force for the washer bearing check: preload (step 1) or loaded (step 2), whichever is larger.
    result["bolt_axial_max_N"] = [max(abs(v[17]), v[8]), max(abs(v[18]), v[13])]
    keys = ["core_xz", "core_yz", "hardpoint_xz", "hardpoint_yz", "hardpoint_vm"] + [f"{i[0]}_interaction" for i in interactions]
    result["stresses"] = dict(zip(keys, (float(x) for x in stress_out.split(","))))
    return result


def open_project(mech, mechdb_path: pathlib.Path) -> None:
    """Open a saved run (read only: nothing here saves it)."""
    _run(mech, "open project", f"ExtAPI.DataModel.Project.Open({str(mechdb_path)!r})")


def export_result_images(mech, image_dir: pathlib.Path, contact_plot_max: float, result_units: str) -> list[dict]:
    """Export a plan-view image of every contour result in the Solution tree, as Mechanical
    shows it (last load step; layer results at the layer middle), and write
    image_dir/manifest.json for the results viewer.

    Legends use the result units (in/psi for imperial). Each plot is framed on the bodies
    it shows: the panel, or for contact results the bodies of the contact tool's faces,
    with the panel hidden. Contact pressure legends are capped at ``contact_plot_max``
    (MPa) so the washer-edge peak does not flatten the rest of the distribution."""
    image_dir.mkdir(parents=True, exist_ok=True)
    units = "StandardBIN" if result_units == "imperial" else "StandardNMM"
    out = _run(mech, "result images", f"""
        import os, re
        ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.{units}
        settings = Ansys.Mechanical.Graphics.GraphicsImageExportSettings()
        settings.CurrentGraphicsDisplay = False
        settings.Resolution = GraphicsResolutionType.EnhancedResolution
        settings.Background = GraphicsBackgroundType.White
        settings.Width = 1600
        settings.Height = 900
        geo_unit = ExtAPI.DataModel.GeoData.Unit

        def plan_view(geo_bodies):
            # As in the sketch: front edge (+X) up, +Y right, looking along +Z.
            # (Setting the camera vectors directly fails depending on the previous view.)
            Graphics.Camera.SetSpecificViewOrientation(ViewOrientationType.Back)
            Graphics.Camera.Rotate(-90, CameraAxisType.ScreenZ)
            Graphics.Camera.SetFit()  # zooms to the visible bodies
            # SetFit keeps the previous focal point (e.g. the saved contact view), so centre
            # the view on the bodies shown.
            boxes = [list(b.GetBoundingBox()) for b in geo_bodies]  # [xmin, ymin, zmin, xmax, ymax, zmax]
            x0, x1 = min(b[0] for b in boxes), max(b[3] for b in boxes)
            y0, y1 = min(b[1] for b in boxes), max(b[4] for b in boxes)
            Graphics.Camera.FocalPoint = Point([(x0 + x1) / 2, (y0 + y1) / 2, 0.0], geo_unit)

        panel = [b for b in Model.Geometry.GetChildren(DataModelObjectCategory.Body, True) if b.Name.endswith({PANEL_BODY!r})][0]

        def contour_results(parent, tool):
            found = []
            for obj in parent.Children:
                if str(obj.DataModelObjectCategory) == "PostContactTool":
                    found += contour_results(obj, obj)
                elif hasattr(obj, "Maximum") and str(obj.ObjectState) == "Solved":
                    found.append((obj, tool))
            return found

        def number(q):
            return q.Value if hasattr(q, "Value") else float(q)

        lines = []
        for obj, tool in contour_results(Model.Analyses[0].Solution, None):
            name = obj.Name if tool is None else tool.Name + ": " + obj.Name
            name = re.sub(r"\\s*\\(layer \\d+\\)", "", name)  # same result type across layups
            filename = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + ".png"
            if tool is None:
                shown = [panel.GetGeoBody()]
            else:
                shown = dict((f.Body.Id, f.Body) for f in [ExtAPI.DataModel.GeoData.GeoEntityById(i) for i in tool.Location.Ids]).values()
            Tree.Activate([obj])
            legend = Ansys.Mechanical.Graphics.Tools.CurrentLegendSettings()
            capped = (str(obj.DataModelObjectCategory) == "ContactPressure" and {contact_plot_max!r} > 0
                      and obj.Maximum.ConvertUnit("MPa").Value > {contact_plot_max!r})
            if capped:
                n = legend.NumberOfBands
                for i in range(n - 1):
                    legend.SetUpperBound(i, Quantity({contact_plot_max!r} * (i + 1) / (n - 1), "MPa"))
            panel.Visible = tool is None
            plan_view(shown)
            Graphics.ExportImage(os.path.join({str(image_dir)!r}, filename), GraphicsImageExportFormat.PNG, settings)
            panel.Visible = True
            if capped:
                legend.Reset()
            if str(obj.DataModelObjectCategory) == "ContactStatus":
                low = high = unit = ""  # status codes, not a range
            else:
                low, high, unit = repr(number(obj.Minimum)), repr(number(obj.Maximum)), getattr(obj.Maximum, "Unit", None) or ""
            lines.append("\\t".join([name, filename, low, high, unit, str(capped)]))
        ExtAPI.Application.ActiveUnitSystem = MechanicalUnitSystem.StandardNMM
        "\\n".join(lines)
    """)
    images = []
    for line in out.splitlines():
        name, filename, low, high, unit, capped = line.split("\t")
        images.append({"result": name, "file": filename, "min": float(low) if low else None, "max": float(high) if high else None,
                       "unit": unit, "capped": capped == "True"})
    (image_dir / "manifest.json").write_text(json.dumps(images, indent=2))
    return images
