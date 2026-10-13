"""Results viewer: every run's result plots and results.csv in one HTML page.

export_images() opens each saved run in Mechanical and exports a plan-view image of
every result in its Solution tree (new runs do this at the end of their solve).
build() writes outputs/results_viewer.html: pick a result type, filter the runs by their
inputs, and see that result for every matching run, plus a table of results.csv.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time

import pandas as pd

from . import config, mechanical

log = logging.getLogger("pedal_floor")

VIEWER = "results_viewer.html"
_TEMPLATE = pathlib.Path(__file__).with_name("viewer_template.html")
# Images written before every result was exported (kept so older runs still show something).
_LEGACY_IMAGES = {"total_deformation.png": "Total Deformation", "deformation_z.png": "Directional Deformation Z",
                  "contact_pressure.png": "Contact tool, pedal box: Pressure"}


def _saved_runs() -> list[pathlib.Path]:
    return sorted(d for d in config.OUTPUTS.glob("case_*") if (d / "model.mechdb").exists())


def export_images(overwrite: bool = False, mechanical_ui: bool = False) -> dict[str, str]:
    """Export the result images of every saved run that has none yet (every run with
    ``overwrite``). One Mechanical session opens the runs in turn and never saves them.

    Runs open in another Mechanical (it leaves a lock file) are skipped; close them and
    call again. Returns {run folder: outcome}."""
    todo = [d for d in _saved_runs() if overwrite or not (d / "images" / "manifest.json").exists()]
    outcome, mech = {}, None
    try:
        for n, run in enumerate(todo, 1):
            t0 = time.time()
            if (run / "model_Mech_Files" / ".mech_lock").exists():
                outcome[run.name] = "skipped: open in Mechanical (model_Mech_Files/.mech_lock)"
            else:
                p = json.loads((run / "params.json").read_text())
                for attempt in (1, 2):
                    try:
                        if mech is None:
                            mech = mechanical.launch(batch=not mechanical_ui)
                        mechanical.open_project(mech, run / "model.mechdb")
                        images = mechanical.export_result_images(mech, run / "images", p["contact_plot_max"], p["result_units"])
                        outcome[run.name] = f"{len(images)} images"
                        break
                    except Exception as exc:
                        # Retry once in a new session (see the cold-start note in runner.run_case).
                        outcome[run.name] = f"failed: {exc}"
                        if mech is not None:
                            try:
                                mech.exit()
                            except Exception:
                                pass
                        mech = None
            log.info("[%d/%d] %s: %s (%.0f s)", n, len(todo), run.name, outcome[run.name], time.time() - t0)
    finally:
        if mech is not None:
            mech.exit()
    return outcome


def _images(run: pathlib.Path) -> dict[str, dict]:
    """{result type: image info} for one run, paths relative to outputs/."""
    folder = run / "images"
    manifest = folder / "manifest.json"
    if manifest.exists():
        entries = json.loads(manifest.read_text())
    else:
        entries = [{"result": name, "file": file} for file, name in _LEGACY_IMAGES.items() if (folder / file).exists()]
    images = {}
    for entry in entries:
        path = folder / entry["file"]
        if path.exists():
            # The file's timestamp makes the browser reload images that were re-exported.
            images[entry["result"]] = {**entry, "src": f"{run.name}/images/{entry['file']}?v={int(path.stat().st_mtime)}"}
    return images


def build(path: pathlib.Path | None = None) -> pathlib.Path:
    """Write the viewer for the current outputs/results.csv and return its path.

    The page refers to the images in the run folders, so it stays in outputs/."""
    path = path or config.OUTPUTS / VIEWER
    results = pd.read_csv(config.OUTPUTS / "results.csv")
    columns = list(results.columns)
    # run_sweep writes the inputs (sweep columns, pedal position, mesh quality) before "status".
    inputs = columns[:columns.index("status")] if "status" in columns else columns[:1]
    rows = json.loads(results.to_json(orient="records"))  # NaN -> null
    order = []
    for row in rows:
        run = config.run_dir(int(row["case_id"]), float(row["pedal_offset_in"]) * 25.4)
        row["_run"] = run.name
        row["_images"] = _images(run)
        order += [name for name in row["_images"] if name not in order]
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "columns": columns,
        "inputs": inputs,
        "results": order,  # result types, in Solution tree order
        "rows": rows,
    }
    payload = json.dumps(data).replace("</", "<\\/")  # keep "</script>" out of the script block
    path.write_text(_TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    return path
