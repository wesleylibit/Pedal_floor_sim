"""Results viewer: every run's result plots and results.csv in one HTML page.

export_images() opens each saved run in Mechanical and exports a plan-view image of
every result in its Solution tree (new runs do this at the end of their solve).
build() writes results_viewer/results_viewer.html: pick a result type, filter the runs by
their inputs, and see that result for every matching run, plus a table of results.csv.

results_viewer/ keeps its own copy of each run's plots and results row, so runs stay in
the viewer after their folders (or all of outputs/) are deleted. Delete results_viewer/
to start the viewer afresh.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import time

import pandas as pd

from . import config, mechanical

log = logging.getLogger("pedal_floor")

VIEWER = "results_viewer.html"
_TEMPLATE = pathlib.Path(__file__).with_name("viewer_template.html")


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
                outcome[run.name] = ("skipped: project locked (open in Mechanical, or left locked by a Mechanical that did "
                                     "not close cleanly: then delete model_Mech_Files/.mech_lock)")
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


def _save_plots(run: pathlib.Path, saved: pathlib.Path) -> None:
    """Copy a run's plots and manifest.json to ``saved``, unless the copy there is up to date."""
    manifest = run / "images" / "manifest.json"
    copy = saved / "manifest.json"
    if not manifest.exists() or (copy.exists() and copy.stat().st_mtime >= manifest.stat().st_mtime):
        return
    if saved.exists():
        shutil.rmtree(saved)  # re-exported: drop plots of results the run no longer has
    saved.mkdir(parents=True)
    for entry in json.loads(manifest.read_text()):
        if (run / "images" / entry["file"]).exists():
            shutil.copy2(run / "images" / entry["file"], saved / entry["file"])
    shutil.copy2(manifest, copy)  # last (with its timestamp), so an interrupted copy is redone


def _inputs(results: pd.DataFrame) -> list[str]:
    # run_sweep writes the inputs (sweep columns, pedal position, mesh quality) before "status".
    columns = list(results.columns)
    return columns[:columns.index("status")] if "status" in columns else columns[:1]


def _save_results(saved: pathlib.Path) -> tuple[pd.DataFrame, list[str]]:
    """Merge outputs/results.csv into the saved results (a case and position run again
    replaces its row) and write them back; return them with the input columns."""
    frames = [pd.read_csv(p) for p in (saved, config.OUTPUTS / "results.csv") if p.exists()]
    if not frames:
        raise FileNotFoundError("No results yet: run a case first (outputs/results.csv is written after each run).")
    merged = pd.concat(frames, ignore_index=True)
    key = merged["case_id"].astype(int).astype(str) + "@" + merged["pedal_offset_in"].astype(float).round(4).astype(str)
    merged = merged[~key.duplicated(keep="last")]  # outputs/results.csv comes last, so it wins
    merged = merged.sort_values(["case_id", "pedal_offset_in"], key=lambda s: s.astype(float))
    inputs = list(dict.fromkeys(c for f in frames for c in _inputs(f)))
    others = [c for c in dict.fromkeys(c for f in reversed(frames) for c in f.columns) if c not in inputs]
    merged = merged[inputs + others]
    saved.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(saved, index=False)
    return merged, inputs


def _images(saved: pathlib.Path) -> dict[str, dict]:
    """{result type: image info} for one run's saved plots, paths relative to results_viewer/."""
    manifest = saved / "manifest.json"
    images = {}
    for entry in json.loads(manifest.read_text()) if manifest.exists() else []:
        path = saved / entry["file"]
        if path.exists():
            # The file's timestamp makes the browser reload images that were re-exported.
            src = path.relative_to(saved.parents[1]).as_posix()
            images[entry["result"]] = {**entry, "src": f"{src}?v={int(path.stat().st_mtime)}"}
    return images


def build() -> pathlib.Path:
    """Save the plots and results of the runs in outputs/ to results_viewer/, write the
    viewer page there for every run saved so far, and return its path."""
    folder = config.VIEWER_DIR
    for run in sorted(config.OUTPUTS.glob("case_*")):
        _save_plots(run, folder / "plots" / run.name)
    results, inputs = _save_results(folder / "results.csv")
    columns = list(results.columns)
    rows = json.loads(results.to_json(orient="records"))  # NaN -> null
    order = []
    for row in rows:
        run = config.run_dir(int(row["case_id"]), float(row["pedal_offset_in"]) * 25.4).name
        row["_run"] = run
        row["_images"] = _images(folder / "plots" / run)
        order += [name for name in row["_images"] if name not in order]
    data = {
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "columns": columns,
        "inputs": inputs,
        "results": order,  # result types, in Solution tree order
        "rows": rows,
    }
    payload = json.dumps(data).replace("</", "<\\/")  # keep "</script>" out of the script block
    path = folder / VIEWER
    path.write_text(_TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", payload), encoding="utf-8")
    return path
