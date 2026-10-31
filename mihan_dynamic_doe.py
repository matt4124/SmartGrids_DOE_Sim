"""EEE40016 - Mihan's rule-based phase-aware dynamic DOE (PSS-SINCAL).

Place this file NEXT TO Matthew's run_qsts.py in SmartGrids_DOE_Test.
No changes to run_qsts.py or run_static_doe.py are required.

Usage (from the SmartGrids_DOE_Test folder):
    python mihan_dynamic_doe.py

Uses copies of sincal_networks/LV1.sin and LV1_files under
mihan_doe_outputs/network_dynamic; writes only to those copies.
Three five-minute time steps by default. Extend END after testing.

This is a coordinated, phase-specific PV dispatch heuristic, NOT an
optimal-hosting-capacity solver. It accepts changes only after a real SINCAL
unbalanced load flow. Missing line ratings mean line loading is NOT checked.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil
import math

import numpy as np
import pandas as pd

from run_qsts import (
    SincalNetwork, list_loads, list_pv, load_profiles, assign_profiles,
    common_index, read_node_voltages, read_branch_flows,
)

ROOT = Path(__file__).resolve().parent
SOURCE_SIN = ROOT / "sincal_networks" / "LV1.sin"
PROFILE_FOLDER = ROOT / "profiles"
OUT = ROOT / "mihan_doe_outputs"
START = "2023-03-28 14:35"
END = "2023-03-28 16:45"  # expand AFTER first successful run
STEP = 1                 # 5-minute rows; set 3 for 15-minute sampling
MAX_PV_KW = 5.0          # match Matthew's preliminary static test
V_MIN_PU = 0.94          # match Matthew's preliminary static test
V_MAX_PU = 1.10
VUF_MAX_PCT = 2.0
# Line current ratings are absent in this converted feeder: intentionally
# NOT asserted/checked in this preliminary demonstration.
MAX_TRAFO_LOADING_PCT = 100.0
SEARCH_TOL_KW = 0.05
MAX_ROUNDS = 2


def copy_network_to_output() -> Path:
    """Fresh disposable copy so calculations cannot alter Matthew's models."""
    if not SOURCE_SIN.is_file():
        raise FileNotFoundError(SOURCE_SIN)
    companion = SOURCE_SIN.with_name(SOURCE_SIN.stem + "_files")
    if not (companion / "database.db").exists():
        raise FileNotFoundError(companion / "database.db")
    dest = OUT / "network_dynamic"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copy2(SOURCE_SIN, dest / SOURCE_SIN.name)
    shutil.copytree(companion, dest / companion.name,
                    ignore=shutil.ignore_patterns("DBG", "LOG", "~*"))
    return dest / SOURCE_SIN.name


@dataclass
class Metrics:
    converged: bool
    min_v_pu: float = float("nan")
    max_v_pu: float = float("nan")
    max_vuf_pct: float = float("nan")
    max_trafo_loading_pct: float = float("nan")
    trafo_checked: bool = False

    def feasible(self) -> bool:
        if not self.converged:
            return False
        if not (V_MIN_PU <= self.min_v_pu <= self.max_v_pu <= V_MAX_PU):
            return False
        if not (math.isfinite(self.max_vuf_pct) and self.max_vuf_pct <= VUF_MAX_PCT):
            return False
        if self.trafo_checked and self.max_trafo_loading_pct > MAX_TRAFO_LOADING_PCT:
            return False
        return True


def evaluate_results(volts: pd.DataFrame, flows: pd.DataFrame) -> Metrics:
    if volts.empty:
        return Metrics(False)
    # SINCAL's U*_Un columns are percentages of each node's nominal
    # phase-to-neutral voltage, unlike U*_kV which mix MV and LV values.
    u = volts[["U1_Un", "U2_Un", "U3_Un"]].apply(pd.to_numeric, errors="coerce")
    x = u.to_numpy(dtype=float).reshape(-1) / 100.0
    x = x[np.isfinite(x) & (x > 0)]
    if len(x) == 0 or "VUF_sincal_pct" not in volts:
        return Metrics(False)
    vuf = pd.to_numeric(volts["VUF_sincal_pct"], errors="coerce")
    if vuf.isna().any():
        return Metrics(False)

    # Name of transformer in this specific LV1 model is Tx3170.
    # Only transformer results are checked for thermal loading; line
    # ratings are missing, so do not imply overall thermal compliance.
    trans = flows.loc[flows["element"].astype(str).str.match(
        r"(?i)^(tx|trafo|transformer)"), "loading_pct"] if not flows.empty else pd.Series(dtype=float)
    trans = pd.to_numeric(trans, errors="coerce").dropna()
    return Metrics(True, float(x.min()), float(x.max()), float(vuf.max()),
                   float(trans.max()) if len(trans) else float("nan"), bool(len(trans)))


def solve_candidate(net: SincalNetwork, sin: Path, load_kw: dict,
                    dispatch: dict) -> Metrics:
    loads = {name: (kw, 0.0) for name, kw in load_kw.items()}
    pvs = {name: (kw, 0.0) for name, kw in dispatch.items()}
    net.set_powers(loads=loads, pvs=pvs)
    if not net.solve(verbose=False):
        return Metrics(False)
    return evaluate_results(read_node_voltages(str(sin)), read_branch_flows(str(sin)))


def choose_dispatch(net: SincalNetwork, sin: Path, load_kw: dict,
                    available_kw: dict, pv_phase: dict) -> tuple[dict, Metrics, int]:
    """Round-robin, individual-PV bisection; each trial verified by SINCAL.

    This is phase-aware because each named PV has its own phase connection in
    the unbalanced feeder; feasibility comes from full unbalanced power flow.
    No mathematical maximum or fairness optimum is claimed.
    """
    current = {n: 0.0 for n in available_kw}
    calls = 0

    def check(candidate):
        nonlocal calls
        calls += 1
        return solve_candidate(net, sin, load_kw, candidate)

    # Fast path: if all available PV is safe, there is nothing to curtail.
    full = check(available_kw)
    if full.feasible():
        return dict(available_kw), full, calls

    zero = check(current)
    if not zero.feasible():
        raise RuntimeError("Zero-PV operating point is infeasible (or required measurements are unavailable). "
                           "Cannot claim valid DOE export limits for this timestamp.")
    accepted = zero
    # Order is stable and interleaves phases to avoid privileging one phase.
    ordered = sorted(available_kw, key=lambda n: (pv_phase[n], n))
    for _ in range(MAX_ROUNDS):
        any_change = False
        for name in ordered:
            cap = available_kw[name]
            low, high = current[name], cap
            if high - low <= SEARCH_TOL_KW:
                continue
            best = low
            best_measure = accepted
            while high - low > SEARCH_TOL_KW:
                mid = (low + high) / 2
                candidate = dict(current)
                candidate[name] = mid
                m = check(candidate)
                if m.feasible():
                    low, best, best_measure = mid, mid, m
                else:
                    high = mid
            if best > current[name] + 1e-8:
                current[name] = best
                accepted = best_measure
                any_change = True
        if not any_change:
            break
    # A rejected trial may be the last model state: restore final accepted dispatch.
    final = check(current)
    if not final.feasible():
        raise RuntimeError("Final DOE dispatch failed SINCAL revalidation")
    return current, final, calls


def run() -> None:
    print("Mihan phase-aware dynamic DOE - real PSS-SINCAL calculations")
    print("All model changes stay in:", OUT)
    sin = copy_network_to_output()
    profiles = load_profiles(str(PROFILE_FOLDER), ("demand",))
    assignment = assign_profiles(str(sin), profiles)
    loads_df = list_loads(str(sin))
    pv_df = list_pv(str(sin))
    loads = loads_df.to_dict("records")
    pvs = pv_df.to_dict("records")
    pvs_by_node = {p["node"]: p for p in pvs}
    pv_phase = {p["name"]: p["phase"] for p in pvs}
    pairs = []
    for ld in loads:
        pv = pvs_by_node.get(ld["node"])
        if pv is None:
            raise RuntimeError(f"No PV at load node {ld['node']}")
        if ld["phase"] != pv["phase"]:
            raise RuntimeError(f"Phase mismatch at {ld['node']}")
        pairs.append((ld["name"], pv["name"], assignment[ld["name"]]))
    idx = common_index(profiles, set(assignment.values()))
    idx = idx[(idx >= pd.Timestamp(START)) & (idx <= pd.Timestamp(END))]
    idx = idx[::STEP]
    if len(idx) == 0:
        raise ValueError("No common household profile timestamps in requested range")

    net = SincalNetwork(str(sin))
    dispatch_rows, metrics_rows, limit_rows = [], [], []
    print(f"Households: {len(pairs)} | timestamps: {len(idx)} | starting {idx[0]}")
    print("Constraints: voltage 0.94-1.10 pu; VUF <=2%; transformer <=100% when identified")
    print("CAUTION: line thermal ratings are NOT checked; preliminary study only")
    if any(p["phase"] not in ("L1", "L2", "L3") for p in pvs):
        raise RuntimeError("Controller requires single-phase PV connections on L1/L2/L3")
    for t in idx:
        ld_kw, available_kw, customer_by_pv = {}, {}, {}
        for lname, pname, household in pairs:
            row = profiles[household].loc[t]
            demand = max(0.0, float(row.kW_load))
            pv_avail = min(MAX_PV_KW, max(0.0, float(row.kW_pv)))
            ld_kw[lname] = demand
            available_kw[pname] = pv_avail
            customer_by_pv[pname] = (lname, household, demand)
        limits, met, calls = choose_dispatch(net, sin, ld_kw, available_kw, pv_phase)
        for pname, pv_kw in limits.items():
            lname, household, demand = customer_by_pv[pname]
            avail = available_kw[pname]
            # Net-export headroom = controlled gross PV - coincident load.
            net_export = max(0.0, pv_kw - demand)
            dispatch_rows.append(dict(time=t, pv=pname, phase=pv_phase[pname],
                                      household=household, load_kW=demand,
                                      pv_available_kW=avail, pv_dispatched_kW=pv_kw,
                                      net_export_kW=net_export,
                                      curtailed_kW=avail-pv_kw,
                                      dynamic_export_limit_kW=net_export))
            limit_rows.append(dict(time=t, pv=pname, phase=pv_phase[pname],
                                   export_limit_kW=net_export))
        metrics_rows.append(dict(time=t, converged=met.converged,
                                 min_v_pu=met.min_v_pu, max_v_pu=met.max_v_pu,
                                 max_vuf_pct=met.max_vuf_pct,
                                 max_trafo_loading_pct=met.max_trafo_loading_pct,
                                 trafo_checked=met.trafo_checked,
                                 power_flow_calls=calls))
        print(f"{t}: " + ", ".join(f"{p}={x:.2f}kW" for p, x in limits.items())
              + f" | max voltage {met.max_v_pu:.4f} pu; VUF {met.max_vuf_pct:.3f}%"
              + f" | {calls} power flows")

    OUT.mkdir(exist_ok=True)
    d = pd.DataFrame(dispatch_rows)
    m = pd.DataFrame(metrics_rows)
    l = pd.DataFrame(limit_rows)
    d.to_csv(OUT / "dynamic_dispatch.csv", index=False)
    l.to_csv(OUT / "dynamic_limits.csv", index=False)
    m.to_csv(OUT / "dynamic_metrics.csv", index=False)
    # Energy integral only valid for contiguous constant-cadence samples.
    times = pd.DatetimeIndex(idx)
    if len(times) > 1:
        delta = times.to_series().diff().dropna()
        if delta.nunique() == 1:
            interval_h = delta.iloc[0].total_seconds() / 3600
            print(f"Preliminary total exported energy: {(d.net_export_kW.sum()*interval_h):.4f} kWh "
                  f"({interval_h*60:g}-min intervals)")
        else:
            print("Nonuniform timestamps: energy integration omitted")
    print("Saved:", OUT / "dynamic_dispatch.csv")
    print("Saved:", OUT / "dynamic_limits.csv")
    print("Saved:", OUT / "dynamic_metrics.csv")


if __name__ == "__main__":
    run()
