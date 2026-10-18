# Pedal floor FEA sweep

Parametric stiffness / load-derivation model of the FSAE pedal floor sandwich panel with the pedal box bolted on. For every row of `inputs/sweep.csv` and every pedal box position along the slot, it builds the geometry, lays up the panel in ACP, solves in Mechanical, and writes results and images.

## Requirements

- Windows.
- Ansys **2026 R1** or **2025 R2** with Mechanical, ACP and SpaceClaim (or Discovery), and licences for them.
- Python **3.11–3.14** ([python.org](https://www.python.org/downloads/)); used on 3.13 and 3.14. If none is installed, `setup.bat` offers to install Python 3.13.

## Setup (once)

1. Get the code from GitHub: clone the repository, or use **Code → Download ZIP** and unzip it anywhere.
2. Double-click `setup.bat`. It creates `.venv` and installs the packages in `requirements.txt`.
   - **Python:** it uses an installed Python 3.11–3.14. If there is none, it offers to install Python 3.13 for your Windows user with winget (no admin rights needed). Without winget, it points you to python.org.
   - **`.venv`:** each PC needs its own. A `.venv` copied from another PC points to that PC's Python, so `setup.bat` rebuilds it.
   - **Errors:** if anything fails, the window stays open with the reason. Fix it and run `setup.bat` again.
3. Open the folder in VS Code and select the **.venv** Python kernel for the notebooks.

The newest supported Ansys version installed is used (2026 R1 before 2025 R2). To pick one, set the environment variable `PEDAL_FLOOR_ANSYS_VERSION` to `261` or `252` before starting VS Code.

## Running

- `Pedal_floor_FEA_script.ipynb`: validate the inputs, run one case, then the whole sweep. Its first cell explains the inputs in detail.
- `results_viewer.ipynb`: builds `results_viewer/results_viewer.html` and opens it in your browser. The page shows any result plot for every run, plus a table of `results.csv`, with filters on the inputs. It can also export plots for runs solved before plots were exported automatically.

Rough runtimes: about 80 s per run on the coarse mesh, 170 s on the fine mesh (`mesh_quality` in `inputs/constants.csv`). Each sweep row runs at every pedal box position (9 with the default 1 in step).

## Inputs (`inputs/`)

All values carry units (in, mm, lbf, N, psi, ksi, MPa, Pa, GPa, pcf, kg/m3, deg).

- `constants.csv`: geometry, bolts, loads, mesh sizes and other values shared by every run.
- `sweep.csv`: one row per case. Units go in the header, e.g. `hardpoint_rad [in]`. Any column overrides the constant of the same name.
- `materials.csv`: one property per row, like an Engineering Data card.
- `ch_27_pedal_floor_sim.stp`: the pedal box surface. Its rear hole is at the CAD origin; both holes are found automatically.

## Outputs

- `outputs/results.csv`: one row per case and pedal position. It holds bolt reactions, the net out-of-plane force each joint puts on the panel, contact pressure, deflection, strength checks, weights and runtime.
- Weights: areal weight of the face sheets, core layer and hardpoint layer; mass of the face sheets, core, hardpoint, washers, bolts (shank over the grip only, no head or nut) and pedal box plate. `panel_mass` is face sheets + core + hardpoint; `system_mass` is everything.
- `outputs/case_###_offX.XXin/`: `model.mechdb` (open in Mechanical; ply-by-ply results can be added on the imported plies), `acp_model.acph5` (open in ACP), a log, and `images/`. `images/` has a plan view of every result in the Solution tree at the end of load step 2, in the result units, plus `manifest.json` (each plot's min/max).
- `results_viewer/results_viewer.html`: one page for all runs, rewritten after every run.
  - **Plots**: pick a result, filter the runs by their inputs, group them by an input, and show a `results.csv` value under each plot. Click a plot to enlarge it.
  - **Table**: every `results.csv` column, with the same filters and toggleable column groups.
  - Each plot has its own colour scale, so compare the legends or values, not the colours.
- `results_viewer/` also keeps its own copy of every run's plots (`plots/`) and results rows (`results.csv`). A run stays in the viewer after you delete its folder, or all of `outputs/`, to free space. A case and position run again replaces its saved row and plots. Delete `results_viewer/` to start the viewer afresh.

## Strength checks

The core and hardpoint are checked against datasheet strengths in `materials.csv`; the face sheets are not checked (stiffness only). The check depends on the material:

- **Orthotropic** (honeycomb core, balsa hardpoint): one interaction index, evaluated at every node of the core (or hardpoint) layer at the end of both load steps:
  R = √((τxz/S_xz)² + (τyz/S_yz)² + (σz/S_c)²) ≤ 1
  - S_xz, S_yz: the L or W plate shear strength (core ribbon L along X).
  - S_c: the (stabilised) compressive strength. A material without one gets no crush term.
  - The check uses the largest R anywhere: MS = 1/(`safety_factor` × R) − 1.
- **Isotropic** (aluminium hardpoint): von Mises in the layer against yield, MS = yield/(`safety_factor` × σvm) − 1.

For each run, `results.csv` gives:
- the largest τxz, τyz and σz anywhere in each layer, for information;
- the allowables;
- the index, its MS, pass/fail, and which term governs at its worst point (`_interaction_governing`);
- `core_ok`, `hardpoint_ok`, `passes` (this run) and `case_passes` (every pedal position of that case).

Each run also gets a plot of R for each orthotropic layer, with the line R = 1, in its images and in the results viewer. `allowable_basis` (min or typ) picks the datasheet value.

Where σz comes from:
- Shells carry no through-thickness stress. σz is taken as the contact pressure pressing on the panel at that point: from the pedal box on one face and from the washers on the other, whichever is larger. It is interpolated from the contact mesh onto the panel nodes.
- This ignores load spreading through the face sheets, which is conservative.
- The pressure peaks at the edges and corners of the pedal box plate and washers depend on the mesh, and they often set the maximum R. Check failures there on the fine mesh, and look at the plot to see how much area is above R = 1.
- The core shear peak along the hardpoint edge also depends on the mesh.

To redo the checks of the runs still in `outputs/` without solving again, run `runner.recheck()` (cell in `Pedal_floor_FEA_script.ipynb`). Use it after changing strengths in `materials.csv`, or for runs solved before this check existed; those first get their stress fields from `model.mechdb`. Each run keeps its node-by-node stresses and contact pressures in `core_fields.json`.

## Model notes

- Load step 1 applies the bolt preload. Load step 2 locks the bolts and applies the pedal load.
- Each bolt is a grade 8 beam. Its head end is tied rigidly to a patch on the pedal box. Its nut end is tied to the nut's footprint on a separate 7075 washer body (1/8 in thick) under the panel.
- The pedal box to panel contact is frictionless. The washer to panel contact is rough (no sliding, can separate), so the washers carry the bolt shear into the panel.
- The panel is a layered shell with its reference surface on the back (nut-side) face. The core ribbon (L) runs along X, and the right slot has no hardpoint.
- A hardpoint that reaches past a panel edge is trimmed at that edge, so its area and mass are only what lies on the panel. It may not reach the right slot. The slots and the nut-side washers must still fit inside the panel.
- Contacts act between shell reference surfaces with Adjust to Touch. Mechanical only allows shell-thickness contact for mid-plane sections, and the ACP layup is offset. The washer reference surface sits 1/16 in from the panel's, so its shear acts close to the real interface.
- The peak contact pressure at the washer edges depends on the mesh. Read the pressure distribution, not that peak.
- Developed on Ansys 2025 R2. An earlier version (before the washer bodies, strength checks and results viewer) ran a full sweep on 2026 R1. The parts added since then have only been run on 2025 R2, so run one case first on a 2026 R1 PC.
- On 2026 R1, Mechanical and ACP connect over their default secure local channel. On 2025 R2 without the gRPC service pack they fall back to an insecure channel that only listens on localhost.
- 2026 R1 returns a sketched surface with holes as separate faces, and rebuilding it from its edges hits a curve-scale bug in ansys-geometry-core 0.18. `geometry.py` repairs the curves for that case (`_surface_with_holes`); 2025 R2 does not need it.
