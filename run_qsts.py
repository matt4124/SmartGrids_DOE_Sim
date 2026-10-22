"""
run_qsts.py
===========

Quasi-static time series (QSTS) for a PSS-SINCAL V22.5 LV network built by
opendss_to_sincal.py.

* Every Load is assigned one customer CSV (random, repeatable with `seed`).
* The VarShuntElement PV at the same node (named PV_<bus>) uses the SAME CSV,
  so each customer's demand and solar are consistent.
* For every timestamp the P/Q of each Load and the Plf of each PV shunt are
  updated in memory, the unbalanced power flow (LF_USYM) is run, and results
  are collected.

Notebook usage
--------------
    from run_qsts import run_qsts
    res = run_qsts(r"C:\\...\\new_network\\LV1.sin", r"C:\\...\\csvs",
                   seed=1, start="2023-04-04 15:00", max_steps=288)
    res.node_v_pu.plot(legend=False)
    res.summary

    # after editing the file:  import importlib, run_qsts; importlib.reload(run_qsts)

CSV units are assumed to be kW (SINCAL wants MW).  Solar is negative
(generation); it is written straight into the VarShuntElement Plf, which uses
a load convention (negative = generating).
"""

from __future__ import annotations

import glob
import math
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

try:
    import pythoncom
    import win32com.client
except ImportError as exc:                                  # pragma: no cover
    raise ImportError("pywin32 is required:  pip install pywin32") from exc

COM_VER = "28"          # PSS-SINCAL 22.5
STATUS_OK = 1101


@dataclass
class QSTSResult:
    assignment: pd.DataFrame                    # load / pv / node / csv
    node_v_pu: pd.DataFrame                     # time x (node_L1/L2/L3), pu of Un/sqrt3
    line_loading_pct: pd.DataFrame              # time x line, Imax/Ib [%]
    trafo_loading_pct: pd.DataFrame             # time x trafo, Imax/Ib [%]
    trafo_p_mw: pd.DataFrame                    # time x trafo, P at terminal 1 [MW]
    load_p_mw: pd.DataFrame                     # applied demand per load
    pv_p_mw: pd.DataFrame                       # applied PV (negative = generation)
    status: pd.Series                           # SINCAL status per step
    summary: pd.DataFrame = field(default_factory=pd.DataFrame)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _query(db_file: str, sql: str):
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _element_nodes(db_file: str, table: str) -> dict[str, str]:
    """element name -> node name (terminal 1) for a given element table."""
    rows = _query(db_file, f'''
        SELECT e."Name", n."Name" FROM "{table}" x
        JOIN "Element"  e ON e."Element_ID" = x."Element_ID"
        JOIN "Terminal" t ON t."Element_ID" = e."Element_ID" AND t."TerminalNo" = 1
        JOIN "Node"     n ON n."Node_ID"    = t."Node_ID"''')
    return {r[0]: r[1] for r in rows}


def _names(db_file: str, table: str) -> list[str]:
    return [r[0] for r in _query(db_file, f'''
        SELECT e."Name" FROM "{table}" x
        JOIN "Element" e ON e."Element_ID" = x."Element_ID" ORDER BY 1''')]


# FIX: new helper - element name -> Element_ID, for the GetObj numeric fallback
def _elem_ids(db_file: str, table: str) -> dict[str, int]:
    rows = _query(db_file, f'''
        SELECT e."Name", e."Element_ID" FROM "{table}" x
        JOIN "Element" e ON e."Element_ID" = x."Element_ID"''')
    return {r[0]: int(r[1]) for r in rows}


# FIX: new helper - node name -> Node_ID
def _node_ids(db_file: str) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in
            _query(db_file, 'SELECT "Name", "Node_ID" FROM "Node"')}


def _read_profiles(files: list[str], cols: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for f in files:
        df = pd.read_csv(f, parse_dates=["interval_start"], index_col="interval_start")
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"{os.path.basename(f)} is missing columns {missing}")
        df = df[cols].astype(float).sort_index()
        dup = df.index.duplicated(keep="first")
        if dup.any():
            print(f"[note] {os.path.basename(f)}: dropped {int(dup.sum())} duplicate timestamps")
            df = df[~dup]
        out[f] = df
    return out


def _res(obj, table: str, term: int):
    try:
        r = obj.Result(table, term)
        if r is None:
            return None
        try:
            r.MoveFirst()
        except Exception:
            pass
        return r
    except Exception:
        return None


def _item(r, name: str):
    try:
        return float(r.Item(name))
    except Exception:
        return np.nan


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------

def run_qsts(sin_file: str,
             csv_dir: str,
             method: str = "LF_USYM",
             demand_cols: tuple = ("demand",),
             solar_col: str = "solar",
             pf: float = 0.98,
             seed: int | None = None,
             replace: bool | None = None,
             start: str | None = None,
             end: str | None = None,
             max_steps: int | None = None,
             batch_mode: bool = True,
             step_callback=None,
             out_dir: str | None = None,
             verbose: bool = True) -> QSTSResult:
    """
    sin_file      : the .sin created by opendss_to_sincal.py
    csv_dir       : folder of customer CSVs (interval_start, demand, solar, ...)
    demand_cols   : columns summed into load kW. Use ("demand",) if `demand`
                    already includes hot water / air-con, otherwise
                    ("demand", "hot_water", "air_con").
    pf            : load power factor (lagging) used to derive Q from P
    replace       : sample CSVs with replacement. Default: only if fewer CSVs than loads
    step_callback : optional f(t, ctx) called before each power flow, for DOE
                    logic. ctx = dict(sim, loads, pvs, assignment, step).
    """
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass

    sin_file = os.path.abspath(sin_file)
    db_file = sin_file[:-4] + "_files\\database.db"
    if not os.path.exists(db_file):
        raise FileNotFoundError(db_file)
    conn = (f"TYP=NET;MODE=SQLITE;FILE={db_file};USR=Admin;PWD=;SINFILE={sin_file};")

    # ---- 1. topology from the database --------------------------------------
    load_node = _element_nodes(db_file, "Load")
    pv_node = _element_nodes(db_file, "VarShuntElement")
    node_names = [r[0] for r in _query(db_file, 'SELECT "Name" FROM "Node" ORDER BY 1')]
    line_names = _names(db_file, "Line")
    trafo_names = _names(db_file, "TwoWindingTransformer")
    if not load_node:
        raise RuntimeError("no Load elements found in the network")

    # FIX: IDs for the numeric fallback in GetObj
    load_eids = _elem_ids(db_file, "Load")
    pv_eids = _elem_ids(db_file, "VarShuntElement")
    line_eids = _elem_ids(db_file, "Line")
    trafo_eids = _elem_ids(db_file, "TwoWindingTransformer")
    node_eids = _node_ids(db_file)

    # ---- 2. assign CSVs ---------------------------------------------------------
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    if not files:
        raise FileNotFoundError(f"no CSVs in {csv_dir}")
    rng = random.Random(seed)
    loads = sorted(load_node)
    if replace is None:
        replace = len(files) < len(loads)
    picked = ([rng.choice(files) for _ in loads] if replace
              else rng.sample(files, len(loads)))
    if replace and verbose:
        print(f"[note] {len(files)} CSVs for {len(loads)} loads: sampling with replacement")

    csv_of_load = dict(zip(loads, picked))
    node_csv: dict[str, str] = {}
    for ld in loads:
        node_csv.setdefault(load_node[ld], csv_of_load[ld])

    pv_of_node = {}
    for pv, nd in pv_node.items():
        if nd not in node_csv:
            if verbose:
                print(f"[warn] PV '{pv}' at node '{nd}' has no load there; "
                      f"giving it a random CSV")
            node_csv[nd] = rng.choice(files)
        pv_of_node[pv] = node_csv[nd]

    assignment = pd.DataFrame(
        [(ld, load_node[ld], os.path.basename(csv_of_load[ld])) for ld in loads],
        columns=["load", "node", "csv"])
    assignment["pv"] = [next((p for p, n in pv_node.items() if n == nd), None)
                        for nd in assignment["node"]]

    # ---- 3. profiles on a common time axis --------------------------------------
    dcols = list(demand_cols)
    cols = dcols + [solar_col]
    profiles = _read_profiles(sorted(set(csv_of_load.values()) | set(pv_of_node.values())), cols)
    idx = None
    for df in profiles.values():
        idx = df.index if idx is None else idx.intersection(df.index)
    if start:
        idx = idx[idx >= pd.Timestamp(start)]
    if end:
        idx = idx[idx <= pd.Timestamp(end)]
    idx = idx.sort_values()
    if max_steps:
        idx = idx[:max_steps]
    if len(idx) == 0:
        raise RuntimeError("no common timestamps across the assigned CSVs / window")
    if verbose:
        print(f"[qsts] {len(loads)} loads, {len(pv_node)} PV, {len(idx)} steps "
              f"({idx[0]} -> {idx[-1]}), method {method}")

    kva_q = math.tan(math.acos(pf))
    load_kw = {ld: profiles[csv_of_load[ld]][dcols].sum(axis=1).reindex(idx).to_numpy()
               for ld in loads}
    pv_kw = {pv: profiles[f][solar_col].reindex(idx).to_numpy() for pv, f in pv_of_node.items()}

    # ---- 4. simulation object ------------------------------------------------------
    srv = win32com.client.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
    sim = srv.GetSimulation()
    sim.Database(conn)
    sim.SetParameter("IDENTIFICATION", "Name")
    if batch_mode:
        sim.BatchMode(1)            # read real DB, keep results in memory only
    sim.LoadDB(method)

    def dump_messages():
        kinds = {1: "STATUS", 2: "INFO", 3: "WARNING", 4: "ERROR", 6: "TASK"}
        try:
            msgs = sim.Messages
            for i in range(1, msgs.Count + 1):
                m = msgs.Item(i)
                print(f"    [{kinds.get(m.Type, m.Type):>7}] {m.MessageId:>6}  {m.Text}")
        except Exception as exc:
            print(f"    (could not read messages: {exc})")

    print(f"[qsts] LoadDB status: {sim.StatusID} ({sim.StatusText})")
    if sim.StatusID != 1500:
        print("[qsts] LoadDB did not report success (expected 1500). Messages:")
        dump_messages()
        raise RuntimeError("LoadDB failed - see messages above")

    # FIX: warm-up power flow.  GetObj() only hands back real calculation
    # objects after the network has been solved at least once.  This dummy
    # run uses the as-built P/Q from the database; with BatchMode(1) the
    # results go to the virtual DB, so nothing is written to disk and the
    # timestep-1 overwrite below simply replaces them.
    print("[qsts] Warm-up power flow to populate calculation objects...")
    try:
        sim.Start(method)
    except Exception as exc:
        print(f"[qsts] Warm-up Start raised: {exc}")
    if sim.StatusID != STATUS_OK:
        print(f"[qsts] WARNING: warm-up Start not OK "
              f"(status={sim.StatusID}, {sim.StatusText})")
        dump_messages()
        # don't raise - GetObj may still work

    # FIX: get() tries name first, then numeric Element_ID
    def get(kind, name, eid=None):
        last = None
        variants = [(kind.upper(), name), (kind, name)]
        if eid is not None:
            variants += [(kind.upper(), eid), (kind, eid)]
        for k, ident in variants:
            try:
                o = sim.GetObj(k, ident)
                if o is not None:
                    return o
            except Exception as exc:
                last = exc
        print(f"[qsts] GetObj('{kind}', '{name}') failed.")
        if last is not None:
            print(f"       last exception: {last}")
        dump_messages()
        raise RuntimeError(f"{kind} '{name}' not found in the calculation")

    load_obj = {n: get("Load", n, load_eids.get(n)) for n in loads}
    pv_obj = {n: get("VarShuntElement", n, pv_eids.get(n)) for n in pv_node}
    node_obj = {n: get("Node", n, node_eids.get(n)) for n in node_names}
    line_obj = {n: get("Line", n, line_eids.get(n)) for n in line_names}
    trafo_obj = {n: get("TwoWindingTransformer", n, trafo_eids.get(n)) for n in trafo_names}

    def refresh_result_objects():
        """Re-fetch the handles that carry .Result(...) - in case Start() invalidated them."""
        node_obj.update({n: get("Node", n, node_eids.get(n)) for n in node_names})
        line_obj.update({n: get("Line", n, line_eids.get(n)) for n in line_names})
        trafo_obj.update({n: get("TwoWindingTransformer", n, trafo_eids.get(n))
                          for n in trafo_names})

    # ---- 5. time loop ------------------------------------------------------------------
    T = len(idx)
    v_cols = [f"{n}_L{p}" for n in node_names for p in (1, 2, 3)]
    node_v = np.full((T, len(v_cols)), np.nan)
    line_l = np.full((T, len(line_names)), np.nan)
    tr_l = np.full((T, len(trafo_names)), np.nan)
    tr_p = np.full((T, len(trafo_names)), np.nan)
    status = np.zeros(T, dtype=int)
    load_p = np.zeros((T, len(loads)))
    pv_p = np.zeros((T, len(pv_node)))
    pv_list = list(pv_node)

    def collect_step(k: int) -> None:
        for i, n in enumerate(node_names):
            r = _res(node_obj[n], "ULFNodeResult", 0)
            if r is not None:
                for p in (1, 2, 3):
                    node_v[k, 3 * i + p - 1] = _item(r, f"U{p}_Un") / 100.0
        for i, n in enumerate(line_names):
            r = _res(line_obj[n], "ULFBranchResult", 1)
            if r is not None:
                line_l[k, i] = _item(r, "Imax_In")
        for i, n in enumerate(trafo_names):
            r = _res(trafo_obj[n], "ULFBranchResult", 1)
            if r is not None:
                tr_l[k, i] = _item(r, "Imax_In")
                tr_p[k, i] = _item(r, "P")

    t0 = time.time()
    for k, ts in enumerate(idx):
        for j, ld in enumerate(loads):
            p_mw = load_kw[ld][k] / 1000.0
            load_obj[ld].SetItem("P", p_mw)
            load_obj[ld].SetItem("Q", p_mw * kva_q)
            load_p[k, j] = p_mw
        for j, pv in enumerate(pv_list):
            pv_obj[pv].SetItem("Plf", pv_kw[pv][k] / 1000.0)
            pv_obj[pv].SetItem("Qlf", 0.0)

        if step_callback:
            step_callback(ts, dict(sim=sim, loads=load_obj, pvs=pv_obj,
                                   assignment=assignment, step=k))
        for j, pv in enumerate(pv_list):
            pv_p[k, j] = float(pv_obj[pv].Item("Plf"))

        # FIX: wrap Start() so one bad timestep doesn't kill the whole run
        try:
            sim.Start(method)
        except Exception as exc:
            if verbose:
                print(f"[warn] {ts}: Start raised: {exc}")
            status[k] = -1
            continue
        status[k] = sim.StatusID
        if status[k] != STATUS_OK:
            if verbose:
                print(f"[warn] {ts}: status {status[k]} ({sim.StatusText})")
            continue

        collect_step(k)

        # FIX: on iteration 0 only, if nothing came back, refresh handles and retry once.
        if k == 0 and np.all(np.isnan(node_v[0])):
            print("[qsts] No node results on first step - refreshing object handles and retrying...")
            refresh_result_objects()
            collect_step(0)

        if verbose and (k + 1) % 25 == 0:
            print(f"  step {k+1}/{T}  ({(time.time()-t0)/(k+1):.2f} s/step)")

    # ---- 6. package ---------------------------------------------------------------------
    node_v_df = pd.DataFrame(node_v, index=idx, columns=v_cols).replace(0.0, np.nan)
    res = QSTSResult(
        assignment=assignment,
        node_v_pu=node_v_df,
        line_loading_pct=pd.DataFrame(line_l, index=idx, columns=line_names),
        trafo_loading_pct=pd.DataFrame(tr_l, index=idx, columns=trafo_names),
        trafo_p_mw=pd.DataFrame(tr_p, index=idx, columns=trafo_names),
        load_p_mw=pd.DataFrame(load_p, index=idx, columns=loads),
        pv_p_mw=pd.DataFrame(pv_p, index=idx, columns=pv_list),
        status=pd.Series(status, index=idx, name="status"),
    )
    res.summary = pd.DataFrame({
        "v_max_pu": node_v_df.max(axis=1),
        "v_min_pu": node_v_df.min(axis=1),
        "line_max_pct": res.line_loading_pct.max(axis=1),
        "trafo_max_pct": res.trafo_loading_pct.max(axis=1),
        "total_load_mw": res.load_p_mw.sum(axis=1),
        "total_pv_mw": res.pv_p_mw.sum(axis=1),
        "ok": res.status == STATUS_OK,
    })

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for name in ("assignment", "node_v_pu", "line_loading_pct", "trafo_loading_pct",
                     "trafo_p_mw", "load_p_mw", "pv_p_mw", "summary"):
            getattr(res, name).to_csv(os.path.join(out_dir, f"qsts_{name}.csv"))
        if verbose:
            print(f"[qsts] results written to {out_dir}")

    if verbose:
        print(f"[qsts] done in {time.time()-t0:.1f} s; "
              f"{int((status != STATUS_OK).sum())} non-converged steps")

    del load_obj, pv_obj, node_obj, line_obj, trafo_obj, sim, srv
    return res