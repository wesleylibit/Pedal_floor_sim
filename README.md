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
- `results_viewer.ipynb`: builds `outputs/results_viewer.html` and opens it in your browser. The page shows any result plot for every run, plus a table of `results.csv`, with filters on the inputs. It can also export plots for runs solved before plots were exported automatically.
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
- `outputs/results_viewer.html`: one page for all runs, rewritten after every run (keep it in `outputs/`; it loads the images from the run folders).
  - **Plots**: pick a result, filter the runs by their inputs, group them by an input, and show a `results.csv` value under each plot. Click a plot to enlarge it.
  - **Table**: every `results.csv` column, with the same filters and toggleable column groups.
  - Each plot has its own colour scale, so compare the legends or values, not the colours.

## Strength checks

The core and hardpoint are checked against datasheet strengths in `materials.csv`; the face sheets are not checked (stiffness only). For each run, `results.csv` gives the stress, allowable, margin (MS) and pass/fail for each check, plus `core_ok`, `hardpoint_ok`, `passes` (this run) and `case_passes` (every pedal position of that case).

- Core shear XZ/YZ in the core layer against the L/W plate shear strength (core ribbon along X), each on its own and combined: (τxz/S_L)² + (τyz/S_W)² ≤ 1, evaluated point by point. The interaction index R = √((τxz/S_L)² + (τyz/S_W)²) gives MS = 1/(safety_factor × R) − 1.
- Balsa hardpoint: the same shear checks against its shear strength, and crush under the nut washer.
- Metal hardpoint: the same shear checks against shear yield (yield/√3), von Mises and washer bearing against yield.
- The core is not checked for crush; the hardpoint sits under the washer.

Details:
- MS = allowable / (`safety_factor` × stress) − 1. `allowable_basis` (min or typ) picks the datasheet value.
- Stresses are the maximum over both load steps.
- Shells carry no through-thickness stress, so hardpoint crush is bolt force divided by the lower washer's contact area on the panel (washer circle minus the slot).
- The core shear peak sits in the row of core elements along the hardpoint edge, where the stiffness jumps. That value depends on the mesh. Check failures there on the fine mesh before trusting them.

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
