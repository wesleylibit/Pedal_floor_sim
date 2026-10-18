"""Point-by-point interaction index of the core and of an orthotropic hardpoint.

R = sqrt((txz / S_xz)^2 + (tyz / S_yz)^2 + (sz / S_c)^2) at every node of the core
(or hardpoint) layer at the end of both load steps; the check uses its maximum.

- txz, tyz: transverse shear in the layer, max |value| over its middle, top and bottom
  (Mechanical, mechanical.extract_fields).
- sz: through-thickness compression. Shells carry none, so it is the contact pressure
  pressing on the panel at that point: from the pedal box on one face and the washers on
  the other, whichever is larger, interpolated from the contact mesh onto the panel
  nodes. Load spreading through the face sheets is ignored, which is conservative, and
  the pressure peaks at the plate and washer edges depend on the mesh.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.figure import Figure
from matplotlib.tri import LinearTriInterpolator, Triangulation

FIELDS = "core_fields.json"  # written to each run folder by mechanical.extract_fields
TERMS = {"xz": "shear XZ", "yz": "shear YZ", "crush": "crush"}
_REGION_LABELS = {"core": "Core", "hardpoint": "Hardpoint"}
# Mechanical's legend colours, blue (low) to red (high).
_BANDS = ListedColormap(np.array([(0, 0, 255), (0, 178, 255), (0, 255, 255), (0, 255, 178), (0, 255, 0),
                                  (178, 255, 0), (255, 255, 0), (255, 178, 0), (255, 0, 0)]) / 255)
_UNITS = {"imperial": (1 / 25.4, "in", 1 / 0.006894757293168, "psi"), "metric": (1.0, "mm", 1.0, "MPa")}


def _nodal(values: dict, ids: list[int]) -> np.ndarray:
    """(nodes, 2 steps) array from {node id: [step 1, step 2]}; missing nodes are 0."""
    return np.array([values.get(str(i), (0.0, 0.0)) for i in ids], dtype=float).reshape(len(ids), 2)


def _mesh(raw: dict, triangles: list) -> tuple[list[int], np.ndarray, np.ndarray, np.ndarray]:
    """Node ids, x, y (mm) and triangles (local indices) of a set of elements."""
    ids = sorted({n for t in triangles for n in t})
    local = {n: k for k, n in enumerate(ids)}
    xy = np.array([raw["nodes"][str(n)] for n in ids], dtype=float)
    return ids, xy[:, 0], xy[:, 1], np.array([[local[n] for n in t] for t in triangles])


def load(path: pathlib.Path) -> dict:
    """The fields of one run, with the contact pressure mapped onto the panel as ``crush``."""
    raw = json.loads(pathlib.Path(path).read_text())
    contacts = []
    for data in raw["contacts"].values():
        ids, x, y, tri = _mesh(raw, data["triangles"])
        contacts.append((Triangulation(x, y, tri), _nodal(data["pressure"], ids)))
    regions = {}
    for name, data in raw["regions"].items():
        ids, x, y, tri = _mesh(raw, data["triangles"])
        region = {"x": x, "y": y, "triangles": tri}
        region |= {c: _nodal(data[c], ids) for c in ("xz", "yz", "vm") if c in data}
        crush = np.zeros((len(ids), 2))
        for triangulation, pressure in contacts:
            for step in (0, 1):
                mapped = LinearTriInterpolator(triangulation, pressure[:, step])(x, y)
                crush[:, step] = np.maximum(crush[:, step], np.ma.filled(mapped, 0.0))  # 0 off the contact mesh
        region["crush"] = crush
        regions[name] = region
    return regions


def evaluate(fields: dict, allowables: dict) -> dict:
    """Per region: max of each stress (MPa) and, for orthotropic regions, the index R at its
    worst node and step with the term that governs there, plus R per node for the plot."""
    out = {}
    for region, f in fields.items():
        summary = {f"{c}_max": float(f[c].max()) for c in ("xz", "yz", "crush", "vm") if c in f}
        a = allowables.get(region, {})
        if a.get("kind") == "orthotropic":
            terms = {c: (f[c] / a[c]) ** 2 for c in TERMS if a.get(c)}
            r = np.sqrt(sum(terms.values()))
            node, step = np.unravel_index(np.argmax(r), r.shape)
            summary |= {"index": float(r[node, step]), "index_step": int(step) + 1,
                        "governing": TERMS[max(terms, key=lambda c: terms[c][node, step])],
                        "index_nodes": r.max(axis=1), "index_node": int(node)}
        out[region] = summary
    return out


def plot(fields: dict, summary: dict, allowables: dict, p: dict, layer: int, image_dir: pathlib.Path) -> None:
    """Plan view of R for each orthotropic region (front edge up, +Y right, as the Mechanical
    plots), with the line R = 1; adds them to image_dir/manifest.json for the results viewer."""
    to_len, len_unit, to_stress, stress_unit = _UNITS[p["result_units"]]
    entries = []
    for region, s in summary.items():
        if "index_nodes" not in s:
            continue
        f, a, r = fields[region], allowables[region], s["index_nodes"]
        label = f"{_REGION_LABELS.get(region, region.title())} 3D interaction index"
        fig = Figure(figsize=(16, 9), dpi=100)
        ax = fig.add_axes((0.30, 0.07, 0.62, 0.88))
        for other in fields.values():  # the whole panel in grey for context
            ax.tripcolor(Triangulation(other["y"] * to_len, other["x"] * to_len, other["triangles"]),
                         facecolors=np.zeros(len(other["triangles"])), cmap=ListedColormap(["#dde1e7"]))
        tri = Triangulation(f["y"] * to_len, f["x"] * to_len, f["triangles"])
        levels = np.linspace(r.min(), max(r.max(), r.min() + 1e-9), len(_BANDS.colors) + 1)
        filled = ax.tricontourf(tri, r, levels=levels, cmap=_BANDS, norm=BoundaryNorm(levels, _BANDS.N))
        if r.min() < 1 < r.max():
            ax.tricontour(tri, r, levels=[1.0], colors="black", linewidths=1.6)
        i = s["index_node"]
        ax.plot(f["y"][i] * to_len, f["x"][i] * to_len, marker="v", color="red", markeredgecolor="black", markersize=12)
        ax.annotate("Max", (f["y"][i] * to_len, f["x"][i] * to_len), xytext=(8, 8), textcoords="offset points",
                    color="white", fontsize=12, bbox={"boxstyle": "square", "fc": "red", "ec": "red"})
        ax.set_aspect("equal")
        xs = np.concatenate([other["x"] for other in fields.values()]) * to_len
        ys = np.concatenate([other["y"] for other in fields.values()]) * to_len
        pad = 0.03 * max(xs.max() - xs.min(), ys.max() - ys.min())
        ax.set_xlim(ys.min() - pad, ys.max() + pad)
        ax.set_ylim(xs.min() - pad, xs.max() + pad)
        ax.set_xlabel(f"Y [{len_unit}]")
        ax.set_ylabel(f"X [{len_unit}]")
        cax = fig.add_axes((0.02, 0.22, 0.02, 0.42))
        fig.colorbar(filled, cax=cax, ticks=levels, format="%.3g")
        allowable_text = ", ".join(f"S{name} = {a[c] * to_stress:.0f} {stress_unit}"
                                   for c, name in (("xz", "xz"), ("yz", "yz"), ("crush", "c")) if a.get(c))
        formula = r"$R = \sqrt{(\tau_{xz}/S_{xz})^2 + (\tau_{yz}/S_{yz})^2 + (\sigma_z/S_c)^2}$"
        lines = [(label + f" (layer {layer})", "bold"), (formula, "normal"),
                 (r"$\sigma_z$" + ": contact pressure on the panel", "normal"),
                 (f"{allowable_text} ({p['allowable_basis']})", "normal"),
                 (f"Max R = {s['index']:.3g}, load step {s['index_step']} ({s['governing']} governs)", "normal"),
                 ("Black line: R = 1" if r.min() < 1 < r.max() else "", "normal")]
        for k, (text, weight) in enumerate(lines):
            fig.text(0.02, 0.95 - 0.035 * k, text, fontsize=13 if k == 0 else 11, fontweight=weight, va="top")
        filename = f"{region}_3d_interaction_index.png"
        fig.savefig(image_dir / filename, facecolor="white")
        entries.append({"result": label, "file": filename, "min": float(r.min()), "max": float(r.max()), "unit": "",
                        "capped": False, "source": "pedal_floor"})
    manifest = image_dir / "manifest.json"
    existing = json.loads(manifest.read_text()) if manifest.exists() else []
    names = {e["result"] for e in entries}
    manifest.write_text(json.dumps([e for e in existing if e["result"] not in names] + entries, indent=2))
