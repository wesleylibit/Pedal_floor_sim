"""Run one case or the whole sweep.

Each row of sweep.csv is run at every pedal box position along the slot
(``pedalbox_offset_step``). SpaceClaim and ACP stay alive between runs. Mechanical
is restarted for every run: within one session it keeps the first model's
material IDs and is unreliable on reuse.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
import traceback

import pandas as pd

from . import checks, config, geometry, layup, mechanical, viewer

log = logging.getLogger("pedal_floor")
log.setLevel(logging.INFO)  # so each run's log.txt gets the stage messages

# Internal units -> result units, keyed by the suffix of the result column.
_RESULT_UNITS = {
    "imperial": {"N": ("lbf", 1 / 4.4482216152605), "mm": ("in", 1 / 25.4), "MPa": ("psi", 1 / 0.006894757293168),
                 "kg": ("lb", 1 / 0.45359237), "kgm2": ("lbft2", 0.3048**2 / 0.45359237)},
    "metric": {"N": ("N", 1.0), "mm": ("mm", 1.0), "MPa": ("MPa", 1.0), "kg": ("kg", 1.0), "kgm2": ("kgm2", 1.0)},
}


@dataclasses.dataclass
class Sessions:
    """SpaceClaim and ACP sessions reused for every run."""

    modeler: object = None
    acp: object = None
    mechanical_ui: bool = False  # show the Mechanical window while each run solves

    @classmethod
    def start(cls, mechanical_ui: bool = False) -> "Sessions":
        s = cls(mechanical_ui=mechanical_ui)
        try:
            log.info("Starting SpaceClaim (geometry)...")
            s.modeler = geometry.launch()
            log.info("Starting ACP...")
            s.acp = layup.launch()
        except Exception:
            s.close()
            raise
        return s

    def close(self) -> None:
        for name, closer in [
            ("modeler", lambda: self.modeler.close()),
            ("acp", lambda: self.acp.stop()),
        ]:
            if getattr(self, name) is not None:
                try:
                    closer()
                except Exception as exc:  # keep closing the others
                    log.warning("Closing %s failed: %s", name, exc)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _to_result_units(values: dict, system: str) -> dict:
    converted = {}
    for key, value in values.items():
        stem, _, suffix = key.rpartition("_")
        if stem and suffix in _RESULT_UNITS[system] and isinstance(value, float):
            unit, factor = _RESULT_UNITS[system][suffix]
            converted[f"{stem}_{unit}"] = value * factor
        else:
            converted[key] = value
    return converted


def run_case(sessions: Sessions, p: dict, materials: dict[str, dict], offset: float, holes: list) -> dict:
    """Run every stage for one parameter set at one pedal box offset; return its results row."""
    out = config.run_dir(p["case_id"], offset)
    out.mkdir(parents=True, exist_ok=True)
    images = out / "images"
    (out / "params.json").write_text(json.dumps({**p, "pedal_offset_mm": offset}, indent=2))
    handler = logging.FileHandler(out / "log.txt", mode="w")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)

    label = f"Case {p['case_id']} @ {offset / 25.4:.2f} in"
    row = {"status": "ok", "error": ""}
    stage = "geometry"
    t0 = time.time()
    mech = None
    try:
        log.info("%s: geometry", label)
        t_panel = config.panel_thickness(p, materials)
        geo = geometry.build_model(sessions.modeler, p, offset, holes, t_panel, out)
        row |= {"panel_thickness_mm": t_panel, **config.masses(p, materials, geo)}

        stage = "mesh"
        log.info("%s: mesh (%s)", label, p["mesh_quality"])
        for attempt in (1, 2):
            mech = mechanical.launch(batch=not sessions.mechanical_ui)
            try:
                mesh_info = mechanical.mesh(mech, p, geo["geometry_path"], out / "model.mechdb", out / "acp_mesh.h5")
                break
            except RuntimeError as exc:
                # A freshly started Mechanical occasionally rejects its first property
                # edits ("This property is parameterized and is read-only"); a new
                # session has always succeeded.
                mech.exit()
                mech = None
                if attempt == 2:
                    raise
                log.warning("%s: %s; retrying in a new Mechanical session", label, exc)

        stage = "layup"
        log.info("%s: ACP layup", label)
        acp_files = layup.build_layup(sessions.acp, p, materials, out / "acp_mesh.h5", out)

        stage = "solve"
        log.info("%s: solve", label)
        solved = mechanical.solve(mech, p, geo, acp_files["materials_xml"], acp_files["composite_definitions"], images,
                                  checks.shear_allowables(p, materials))
        mech.exit()
        mech = None

        stresses, bolt_axial = solved.pop("stresses"), solved.pop("bolt_axial_max_N")
        bearing = max(f / a for f, a in zip(bolt_axial, geo["panel_washer_areas_mm2"]))
        row |= {
            **solved,
            **checks.evaluate(p, materials, stresses, bearing),
            **mesh_info,
        }
        log.info("%s: done in %.0f s", label, time.time() - t0)
    except Exception as exc:
        row |= {"status": f"failed: {stage}", "error": f"{type(exc).__name__}: {exc}"}
        log.error("%s failed at %s:\n%s", label, stage, traceback.format_exc())
    finally:
        if mech is not None:
            try:
                mech.exit()
            except Exception as exc:
                log.warning("Closing Mechanical failed: %s", exc)
        row["runtime_s"] = round(time.time() - t0, 1)
        log.removeHandler(handler)
        handler.close()
    return _to_result_units(row, p["result_units"])


def run_sweep(sessions: Sessions, case_ids: list[int] | None = None, offsets_in: list[float] | None = None) -> pd.DataFrame:
    """Run the cases in sweep.csv (or only ``case_ids``) at every pedal box position
    (or only ``offsets_in``, given in inches) and write outputs/results.csv.

    results.csv (and the results viewer) are rewritten after every run, so finished
    runs survive a crash. Re-running a case at the same offset replaces its row.
    """
    constants, materials = config.load_constants(), config.load_materials()
    rows, raw_rows = config.load_sweep(), config.load_sweep_raw()
    holes, _ = geometry.find_holes(sessions.modeler, config.INPUTS / constants["pedalbox_cad"])
    results_path = config.OUTPUTS / "results.csv"
    config.OUTPUTS.mkdir(exist_ok=True)
    results = pd.read_csv(results_path) if results_path.exists() else pd.DataFrame()
    if not results.empty and not {"case_id", "pedal_offset_in"} <= set(results.columns):
        # Written by an older version of the pipeline: keep it, but start a new table.
        results_path.replace(results_path.with_name(f"results_old_{time.strftime('%Y%m%d_%H%M%S')}.csv"))
        results = pd.DataFrame()

    for row, raw in zip(rows, raw_rows):
        if case_ids is not None and int(row["case_id"]) not in case_ids:
            continue
        try:
            p = config.case_params(constants, row, materials)
            offsets = config.pedal_offsets(p, holes[1][0] - holes[0][0])
        except ValueError as exc:  # invalid inputs: record and move on
            offsets, p = [0.0], None
            error = str(exc)
        if offsets_in is not None:
            offsets = [o for o in offsets if any(abs(o / 25.4 - w) < 1e-3 for w in offsets_in)]
        for offset in offsets:
            if p is None:
                result = {"status": "failed: inputs", "error": error}
            else:
                result = run_case(sessions, p, materials, offset, holes)
            record = {**raw, "pedal_offset_in": round(offset / 25.4, 4), "mesh_quality": p["mesh_quality"] if p else "", **result}
            if not results.empty:
                same = (results["case_id"].astype(int) == int(raw["case_id"])) & (results["pedal_offset_in"].round(4) == record["pedal_offset_in"])
                results = results[~same]
            results = pd.concat([results, pd.DataFrame([record])], ignore_index=True)
            results = results.sort_values(["case_id", "pedal_offset_in"], key=lambda s: s.astype(float))
            if "passes" in results:
                # A configuration passes only if every pedal position run so far passes.
                ok = results["passes"].astype(str).eq("True")
                results["case_passes"] = ok.groupby(results["case_id"].astype(int)).transform("all")
            results.to_csv(results_path, index=False)
            try:
                viewer.build()
            except Exception as exc:  # the viewer is a convenience; keep the sweep going
                log.warning("Updating %s failed: %s", viewer.VIEWER, exc)
            log.info("Case %s @ %.2f in: %s (%.0f s)", raw["case_id"], offset / 25.4, record["status"], record.get("runtime_s", 0))
    return results
