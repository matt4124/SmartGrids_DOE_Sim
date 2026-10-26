"""EEE40016 preliminary fair static-vs-dynamic check using identical inputs.

Put beside run_qsts.py and mihan_dynamic_doe.py inside SmartGrids_DOE_Test.
It runs an independent PSS-SINCAL static QSTS on exactly the loads, PV
availabilities and timestamps recorded by Mihan's dynamic dispatch CSV.
Matthew's original files are never edited.
"""
from pathlib import Path
import shutil
import pandas as pd

from run_qsts import SincalNetwork, list_loads, list_pv
from mihan_dynamic_doe import solve_candidate, SOURCE_SIN, OUT

ROOT = Path(__file__).resolve().parent
STATIC_LIMIT_KW = 5.0  # Matthew's reported preliminary fixed NET export ceiling per PV
CSV = OUT / "dynamic_dispatch.csv"
DEST = ROOT / "comparison_results" / "network_static_corrected"
RESULTS = ROOT / "comparison_results"


def prepare_sin():
    original_companion = SOURCE_SIN.with_name(SOURCE_SIN.stem + "_files")
    DEST.parent.mkdir(parents=True, exist_ok=True)
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir()
    shutil.copy2(SOURCE_SIN, DEST / SOURCE_SIN.name)
    shutil.copytree(original_companion, DEST / original_companion.name,
                    ignore=shutil.ignore_patterns("DBG", "LOG", "~*"))
    return DEST / SOURCE_SIN.name


def main():
    d = pd.read_csv(CSV, parse_dates=["time"])
    needed = ["time", "pv", "load_kW", "pv_available_kW", "pv_dispatched_kW", "net_export_kW", "curtailed_kW"]
    missing = [x for x in needed if x not in d.columns]
    if missing:
        raise ValueError(f"Dynamic CSV missing columns: {missing}")
    if d.duplicated(["time", "pv"]).any():
        raise ValueError("Duplicate time/PV pairs in dynamic dispatch")
    times = sorted(d.time.unique())
    if len(times) < 2:
        raise ValueError("Need at least two time steps")
    increments = pd.Series(times).diff().dropna()
    if increments.nunique() != 1:
        raise ValueError("Nonuniform timestamps: cannot assume constant-step energy integration")
    hours = increments.iloc[0].total_seconds() / 3600

    sin = prepare_sin()
    all_loads = list_loads(str(sin)).to_dict("records")
    all_pvs = list_pv(str(sin)).to_dict("records")
    pv_node = {p["name"]: p["node"] for p in all_pvs}
    load_by_node = {l["node"]: l["name"] for l in all_loads}
    names = set(d.pv.unique())
    if names != set(pv_node):
        raise ValueError(f"PV names don't match model: CSV={names}; model={set(pv_node)}")
    if not all(pv_node[p] in load_by_node for p in names):
        raise ValueError("At least one PV is not colocated with a load")
    net = SincalNetwork(str(sin))
    rows, met_rows = [], []
    print(f"Running corrected static QSTS: {len(times)} timestamps, {len(names)} PVs")
    print("Same loads, PV availability, network and timestamps as dynamic output")
    for t, frame in d.groupby("time", sort=True):
        loads = {}
        dispatch = {}
        for r in frame.itertuples(index=False):
            demand = float(r.load_kW)
            available = float(r.pv_available_kW)
            if demand < 0 or available < 0:
                raise ValueError(f"Bad demand/PV availability for {r.pv} at {t}")
            loads[load_by_node[pv_node[r.pv]]] = demand
            # Fixed NET-export cap, applied to the SAME available PV as dynamic.
            # Both cases therefore have the same 5-kW gross-PV availability ceiling.
            p = min(available, max(0.0, STATIC_LIMIT_KW + demand))
            dispatch[r.pv] = p
            rows.append(dict(time=t, pv=r.pv, load_kW=demand,
                             pv_available_kW=available, pv_dispatched_kW=p,
                             net_export_kW=max(0.0, p - demand),
                             curtailed_kW=available - p,
                             static_export_limit_kW=STATIC_LIMIT_KW))
        # Real PSS-SINCAL unbalanced power flow on isolated network copy.
        m = solve_candidate(net, sin, loads, dispatch)
        met_rows.append(dict(time=t, converged=m.converged,
                             min_v_pu=m.min_v_pu, max_v_pu=m.max_v_pu,
                             max_vuf_pct=m.max_vuf_pct,
                             max_trafo_loading_pct=m.max_trafo_loading_pct,
                             trafo_checked=m.trafo_checked,
                             feasible_enabled_constraints=m.feasible()))
        if not m.converged:
            raise RuntimeError(f"SINCAL did not converge at {t}")
        print(f"{t}: static PV output " + ", ".join(f"{p}={v:.2f}kW" for p, v in dispatch.items()) +
              f" | max V={m.max_v_pu:.4f} pu, VUF={m.max_vuf_pct:.3f}%")

    s = pd.DataFrame(rows)
    m = pd.DataFrame(met_rows)
    s.to_csv(RESULTS / "static_dispatch_corrected.csv", index=False)
    m.to_csv(RESULTS / "static_metrics_corrected.csv", index=False)
    summary = []
    for name, frame in [("Matthew static (corrected inputs)", s), ("Mihan dynamic", d)]:
        summary.append(dict(controller=name,
                            gross_PV_kWh=frame.pv_dispatched_kW.sum() * hours,
                            net_export_kWh=frame.net_export_kW.sum() * hours,
                            curtailed_kWh=frame.curtailed_kW.sum() * hours))
    report = pd.DataFrame(summary)
    report.to_csv(RESULTS / "energy_comparison_corrected.csv", index=False)
    print("\n=== FAIR COMPARISON ===")
    print(report.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\nCorrected static max voltage:", round(m.max_v_pu.max(), 5), "pu")
    print("Corrected static max VUF:", round(m.max_vuf_pct.max(), 5), "%")
    print("Corrected static steps meeting enabled constraints:", int(m.feasible_enabled_constraints.sum()), "/", len(m))
    print("Reminder: line thermal ratings are missing; not a full thermal-compliance claim")
    print("Saved to:", RESULTS)


if __name__ == "__main__":
    main()
