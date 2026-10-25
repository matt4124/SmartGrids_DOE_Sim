"""
run_qsts.py
===========

Quasi-static time series (QSTS) runner for PSS-SINCAL networks built by
opendss_to_sincal.py.

Current scope: open a .sin network and list every load.

Jupyter usage
-------------
    from run_qsts import list_loads, print_loads

    SIN = r"C:\\path\\to\\new_network\\LV1_14bus.sin"
    df = list_loads(SIN)        # pandas DataFrame (list of dicts if pandas missing)
    print_loads(SIN)            # formatted print

Command line
------------
    python run_qsts.py "C:\\path\\to\\LV1_14bus.sin"

Loads are read straight from the network's SQLite database
(<name>_files/database.db) in read-only mode, so no COM server or licence
is needed for this step.  The COM interface will be added when the
time-series calculation is implemented.
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys

PHASE_NAMES = {1: "L1", 2: "L2", 3: "L3", 4: "L12", 5: "L23",
               6: "L31", 7: "L123", 8: "N"}

LOAD_SQL = """
SELECT  e.Element_ID                  AS element_id,
        e.Name                        AS name,
        n.Name                        AS node,
        t.Flag_Terminal               AS phase_code,
        e.Flag_State                  AS in_service,
        t.Flag_State                  AS connected,
        l.P                           AS P_MW,
        l.Q                           AS Q_Mvar,
        l.P1, l.P2, l.P3,
        l.P12, l.P23, l.P31,
        l.Flag_Lf                     AS input_format
FROM    Load l
JOIN    Element  e ON e.Element_ID = l.Element_ID
JOIN    Terminal t ON t.Element_ID = e.Element_ID AND t.TerminalNo = 1
JOIN    Node     n ON n.Node_ID    = t.Node_ID
WHERE   l.Flag_Variant = 1
  AND   e.Flag_Variant = 1
  AND   t.Flag_Variant = 1
ORDER BY e.Element_ID
"""


def db_path_from_sin(sin_file: str) -> str:
    """Return the SQLite database path belonging to a .sin file."""
    sin_file = os.path.abspath(sin_file)
    if sin_file.lower().endswith(".db"):
        return sin_file
    db = os.path.splitext(sin_file)[0] + "_files" + os.sep + "database.db"
    if not os.path.exists(db):
        raise FileNotFoundError(f"Database not found: {db}")
    return db


def list_loads(sin_file: str):
    """
    Return every load in the network (active variant).

    Columns: element_id, name, node, phase, kW, kvar, in_service, connected.
    kW / kvar are the total for the load regardless of how the input format
    (single-phase, phase-to-phase, per-phase) stores them.
    Returns a pandas DataFrame if pandas is installed, else a list of dicts.
    """
    db = db_path_from_sin(sin_file)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(LOAD_SQL).fetchall()
    finally:
        con.close()

    out = []
    for r in rows:
        # Per-phase / phase-to-phase formats (Flag_Lf 13/14) keep power in
        # P1..P3 / P12..P31 and leave P at 0, so fall back to their sum.
        p_mw = r["P_MW"] or 0.0
        if p_mw == 0.0:
            p_mw = sum((r[k] or 0.0) for k in ("P1", "P2", "P3", "P12", "P23", "P31"))
        out.append({
            "element_id": r["element_id"],
            "name": r["name"],
            "node": r["node"],
            "phase": PHASE_NAMES.get(r["phase_code"], r["phase_code"]),
            "kW": p_mw * 1000.0,
            "kvar": (r["Q_Mvar"] or 0.0) * 1000.0,
            "in_service": bool(r["in_service"]),
            "connected": bool(r["connected"]),
        })

    try:
        import pandas as pd
        return pd.DataFrame(out)
    except ImportError:
        return out


def print_loads(sin_file: str) -> None:
    """Print every load in the network, plus a per-phase summary."""
    loads = list_loads(sin_file)
    rows = loads.to_dict("records") if hasattr(loads, "to_dict") else loads

    print(f"Network: {os.path.abspath(sin_file)}")
    print(f"Loads  : {len(rows)}\n")
    print(f"{'ID':>5}  {'Name':<20} {'Node':<16} {'Phase':<5} "
          f"{'kW':>9} {'kvar':>9}  State")
    print("-" * 78)
    for r in rows:
        state = "on" if (r["in_service"] and r["connected"]) else "OFF"
        print(f"{r['element_id']:>5}  {r['name']:<20} {r['node']:<16} "
              f"{str(r['phase']):<5} {r['kW']:>9.3f} {r['kvar']:>9.3f}  {state}")

    print("\nPer-phase total (kW):")
    for ph in ("L1", "L2", "L3", "L12", "L23", "L31", "L123"):
        sel = [r for r in rows if r["phase"] == ph]
        if sel:
            print(f"  {ph:<5} {sum(r['kW'] for r in sel):>9.3f} kW  ({len(sel)} loads)")


PV_SQL = """
SELECT  e.Element_ID   AS element_id,
        e.Name         AS name,
        n.Name         AS node,
        t.Flag_Terminal AS phase_code,
        e.Flag_State   AS in_service,
        t.Flag_State   AS connected,
        v.Plf          AS Plf_MW,
        v.Qlf          AS Qlf_Mvar,
        v.Ulf          AS Ulf_kV,
        v.Flag_LF      AS pf_input,
        v.Flag_LoadType AS load_behaviour
FROM    VarShuntElement v
JOIN    Element  e ON e.Element_ID = v.Element_ID
JOIN    Terminal t ON t.Element_ID = e.Element_ID AND t.TerminalNo = 1
JOIN    Node     n ON n.Node_ID    = t.Node_ID
WHERE   v.Flag_Variant = 1
  AND   e.Flag_Variant = 1
  AND   t.Flag_Variant = 1
ORDER BY e.Element_ID
"""


def list_pv(sin_file: str):
    """
    Return every VarShuntElement (PV system) in the network (active variant).

    Columns: element_id, name, node, phase, kW, kvar, Un_kV, in_service,
    connected.  SINCAL uses a load convention for VarShuntElement, so a
    generating PV has *negative* Plf; kW here is shown as generation
    (positive = injecting into the grid).
    Returns a pandas DataFrame if pandas is installed, else a list of dicts.
    """
    db = db_path_from_sin(sin_file)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(PV_SQL).fetchall()
    finally:
        con.close()

    out = []
    for r in rows:
        out.append({
            "element_id": r["element_id"],
            "name": r["name"],
            "node": r["node"],
            "phase": PHASE_NAMES.get(r["phase_code"], r["phase_code"]),
            "kW": -(r["Plf_MW"] or 0.0) * 1000.0,      # generation positive
            "kvar": -(r["Qlf_Mvar"] or 0.0) * 1000.0,
            "Un_kV": r["Ulf_kV"] or 0.0,
            "in_service": bool(r["in_service"]),
            "connected": bool(r["connected"]),
        })
    try:
        import pandas as pd
        return pd.DataFrame(out)
    except ImportError:
        return out


def print_pv(sin_file: str) -> None:
    """Print every VarShuntElement and check each load node has one."""
    pvs = list_pv(sin_file)
    pv_rows = pvs.to_dict("records") if hasattr(pvs, "to_dict") else pvs
    loads = list_loads(sin_file)
    ld_rows = loads.to_dict("records") if hasattr(loads, "to_dict") else loads

    print(f"\nVarShuntElements (PV): {len(pv_rows)}\n")
    print(f"{'ID':>5}  {'Name':<20} {'Node':<16} {'Phase':<5} "
          f"{'kW gen':>9} {'kvar':>9} {'Un kV':>7}  State")
    print("-" * 86)
    for r in pv_rows:
        state = "on" if (r["in_service"] and r["connected"]) else "OFF"
        print(f"{r['element_id']:>5}  {r['name']:<20} {r['node']:<16} "
              f"{str(r['phase']):<5} {r['kW']:>9.3f} {r['kvar']:>9.3f} "
              f"{r['Un_kV']:>7.3f}  {state}")

    # Coverage check: one PV per load node, on the same phase
    pv_by_node = {}
    for r in pv_rows:
        pv_by_node.setdefault(r["node"], []).append(r)
    load_nodes = {r["node"] for r in ld_rows}
    missing = sorted(load_nodes - set(pv_by_node))
    extra = sorted(set(pv_by_node) - load_nodes)
    multi = sorted(n for n, v in pv_by_node.items() if len(v) > 1)
    phase_mismatch = []
    for ld in ld_rows:
        for pv in pv_by_node.get(ld["node"], []):
            if pv["phase"] != ld["phase"]:
                phase_mismatch.append(f"{ld['name']}({ld['phase']}) / "
                                      f"{pv['name']}({pv['phase']})")

    print(f"\nLoad nodes: {len(load_nodes)}   PV nodes: {len(pv_by_node)}")
    print(f"  Load nodes without PV : {len(missing)}"
          + (f"  -> {', '.join(missing)}" if missing else ""))
    print(f"  PV nodes without load : {len(extra)}"
          + (f"  -> {', '.join(extra)}" if extra else ""))
    print(f"  Nodes with >1 PV      : {len(multi)}"
          + (f"  -> {', '.join(multi)}" if multi else ""))
    print(f"  Load/PV phase mismatch: {len(phase_mismatch)}"
          + (f"  -> {'; '.join(phase_mismatch)}" if phase_mismatch else ""))


def print_network(sin_file: str) -> None:
    """Print all loads and all PV elements."""
    print_loads(sin_file)
    print_pv(sin_file)


# ---------------------------------------------------------------------------
# Changing element parameters
#
# SINCAL's COM object lookups (Simulation.GetObj and DB_EL().GetCommonObject
# for "Load"/"VarShuntElement") fault with RPC_E_SERVERFAULT on some installs,
# so parameters are edited directly in the network's SQLite database - the
# same approach opendss_to_sincal.py uses for terminal phases.  COM is only
# used to run the calculation (LoadDB + Start), which is known to work.
# ---------------------------------------------------------------------------

COM_VER = "28"   # PSS-SINCAL 22.5

# kind -> (table, P field, Q field, sign)
# sign converts "natural" kW/kvar to the stored MW/Mvar sign:
#   load : +kW consumed         -> +P
#   pv   : +kW generated        -> -Plf  (VarShuntElement uses a load convention)
_KINDS = {
    "load": ("Load", "P", "Q", +1.0),
    "pv":   ("VarShuntElement", "Plf", "Qlf", -1.0),
}


def _setparam(obj, name, value):
    try:
        obj.SetParameter(name, value)
    except AttributeError:
        obj.Parameter(name, value)


class SincalNetwork:
    """
    Read / change parameters of a load or VarShunt (PV) element, and solve.

    Edits are written to the network database (<name>_files/database.db), so
    they persist - run on a copy of the network if you want the base case
    untouched.  Close the network in the SINCAL GUI first (or Reload it
    afterwards), otherwise the file may be locked or the GUI out of date.

    Example
    -------
        net = SincalNetwork(SIN)
        print(net.get("load", "Ld3313_LOAD_A", "P", "Q"))
        net.set_load("Ld3313_LOAD_A", kW=3.2, kvar=0.5)   # kW consumed
        net.set_pv("PV_B2656", kW=4.1)                    # kW generated
        net.solve()                                       # runs LF_USYM

        # QSTS step: many elements in one transaction
        net.set_powers(loads={"Ld3313_LOAD_A": (1.2, 0.2)},
                       pvs={"PV_B2656": (2.5, 0.0)})

        # raw field names / native units (MW, Mvar, ...):
        net.set("pv", "PV_B2656", Plf=-0.004, Qlf=0.0)
    """

    def __init__(self, sin_file: str, persist=None, method: str = "LF_USYM"):
        # 'persist' is accepted for backwards compatibility and ignored:
        # edits always go to the database now.
        self.sin_file = os.path.abspath(sin_file)
        self.db_file = db_path_from_sin(sin_file)
        self.method = method
        self.conn = (f"TYP=NET;MODE=SQLITE;FILE={self.db_file};"
                     f"USR=Admin;PWD=;SINFILE={self.sin_file};")

    # context-manager support (nothing to open/close any more)
    def open(self) -> "SincalNetwork":
        return self

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    # -- low-level SQL -----------------------------------------------------
    def _connect(self, write: bool = False) -> sqlite3.Connection:
        if write:
            return sqlite3.connect(self.db_file, timeout=10)
        return sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True, timeout=10)

    @staticmethod
    def _columns(con, table: str) -> dict:
        return {r[1].lower(): r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}

    def _name_hint(self, kind: str) -> str:
        try:
            rows = list_loads(self.sin_file) if kind.lower() == "load" \
                else list_pv(self.sin_file)
            rows = rows.to_dict("records") if hasattr(rows, "to_dict") else rows
            return (f"Available {kind} names include: "
                    f"{', '.join(r['name'] for r in rows[:5])} ...")
        except Exception:
            return ""

    def _element_id(self, con, kind: str, name: str) -> int:
        table = _KINDS[kind.lower()][0]
        row = con.execute(
            f'SELECT e.Element_ID FROM Element e JOIN "{table}" x '
            'ON x.Element_ID = e.Element_ID '
            'WHERE e.Name = ? AND e.Flag_Variant = 1 AND x.Flag_Variant = 1',
            (name,)).fetchone()
        if row is None:
            raise KeyError(f"{kind} '{name}' not found. {self._name_hint(kind)}")
        return int(row[0])

    def _real_fields(self, con, table: str, fields) -> dict:
        """Map requested field names to the real column names (case-insensitive)."""
        cols = self._columns(con, table)
        out, bad = {}, []
        for f in fields:
            real = cols.get(f.lower())
            if real is None:
                bad.append(f)
            else:
                out[f] = real
        if bad:
            raise KeyError(f"'{table}' has no field(s) {bad}. Columns: "
                           f"{', '.join(sorted(cols.values()))}")
        return out

    # -- public API --------------------------------------------------------
    def get(self, kind: str, name: str, *fields: str) -> dict:
        """Read raw fields (native units, e.g. P in MW) of a 'load' or 'pv'."""
        table = _KINDS[kind.lower()][0]
        con = self._connect()
        try:
            eid = self._element_id(con, kind, name)
            real = self._real_fields(con, table, fields)
            cols = ", ".join(f'"{real[f]}"' for f in fields)
            row = con.execute(
                f'SELECT {cols} FROM "{table}" WHERE Element_ID = ? AND Flag_Variant = 1',
                (eid,)).fetchone()
        finally:
            con.close()
        return dict(zip(fields, row))

    def set(self, kind: str, name: str, **values) -> dict:
        """Set raw fields (native units) and verify by reading them back."""
        return self._set_many([(kind, name, values)])[0]

    def _set_many(self, updates) -> list:
        """updates: list of (kind, name, {field: value}); one transaction."""
        con = self._connect(write=True)
        results = []
        try:
            for kind, name, values in updates:
                table = _KINDS[kind.lower()][0]
                eid = self._element_id(con, kind, name)
                real = self._real_fields(con, table, values)
                sets = ", ".join(f'"{real[f]}" = ?' for f in values)
                con.execute(
                    f'UPDATE "{table}" SET {sets} WHERE Element_ID = ? AND Flag_Variant = 1',
                    (*values.values(), eid))
                cols = ", ".join(f'"{real[f]}"' for f in values)
                row = con.execute(
                    f'SELECT {cols} FROM "{table}" WHERE Element_ID = ? AND Flag_Variant = 1',
                    (eid,)).fetchone()
                got = dict(zip(values, row))
                for f, want in values.items():
                    if not math.isclose(float(got[f]), float(want),
                                        rel_tol=1e-9, abs_tol=1e-12):
                        raise RuntimeError(f"{kind} '{name}': {f} wrote {want}, "
                                           f"read back {got[f]}")
                results.append(got)
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return results

    def _power_values(self, kind: str, kW, kvar) -> dict:
        _, pf, qf, sign = _KINDS[kind]
        vals = {}
        if kW is not None:
            vals[pf] = sign * kW / 1000.0
        if kvar is not None:
            vals[qf] = sign * kvar / 1000.0
        return vals

    def set_load(self, name: str, kW: float | None = None,
                 kvar: float | None = None) -> dict:
        """Set a load's active/reactive power (kW / kvar consumed)."""
        self._check_load_format(name)
        return self.set("load", name, **self._power_values("load", kW, kvar))

    def set_pv(self, name: str, kW: float | None = None,
               kvar: float | None = None) -> dict:
        """Set a PV VarShunt's output (kW / kvar *generated*, positive = injecting)."""
        return self.set("pv", name, **self._power_values("pv", kW, kvar))

    def set_powers(self, loads: dict | None = None, pvs: dict | None = None) -> None:
        """
        Set many elements in one transaction (one QSTS time step).
        loads: {name: (kW, kvar)}   pvs: {name: (kW generated, kvar generated)}
        """
        ups = []
        for name, (kw, kvar) in (loads or {}).items():
            self._check_load_format(name)
            ups.append(("load", name, self._power_values("load", kw, kvar)))
        for name, (kw, kvar) in (pvs or {}).items():
            ups.append(("pv", name, self._power_values("pv", kw, kvar)))
        self._set_many(ups)

    def _check_load_format(self, name: str) -> None:
        fmt = self.get("load", name, "Flag_Lf")["Flag_Lf"]
        if fmt is not None and int(fmt) != 1:
            raise ValueError(
                f"Load '{name}' uses input format {fmt}, not 'P and Q' (1); "
                "set its per-phase fields with .set('load', name, P1=..., Q1=...)")

    def _msg_objects(self, m) -> str:
        """' -> Name(ID), ...' for the network elements a message refers to."""
        try:
            n = int(m.CountObjectIds)
            if n <= 0:
                return ""
            ids = [int(m.ObjectIdAt(i)) for i in range(1, n + 1)]
        except Exception:
            return ""
        names = {}
        try:
            con = self._connect()
            q = ",".join("?" * len(ids))
            names = dict(con.execute(
                f"SELECT Element_ID, Name FROM Element WHERE Flag_Variant=1 "
                f"AND Element_ID IN ({q})", ids).fetchall())
            con.close()
        except Exception:
            pass
        return "  -> " + ", ".join(f"{names.get(i, '?')}({i})" for i in ids[:12]) \
            + (" ..." if len(ids) > 12 else "")

    def solve(self, method: str | None = None, verbose: bool = True,
              save_results: bool = True, show_messages: bool = False) -> bool:
        """
        Run a calculation on the database in a fresh SINCAL simulation.
        save_results=True runs in batch mode (virtual database) and then calls
        SaveDB so the results are written into the .sin database, where
        read_node_voltages() picks them up.
        """
        try:
            import win32com.client as wc
        except ImportError:
            raise ImportError("pywin32 is required: pip install pywin32")
        method = method or self.method
        srv = sim = None
        try:
            srv = wc.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
            sim = srv.GetSimulation()
            if save_results:
                sim.BatchMode(1)       # load from real db, results to virtual db
            sim.Database(self.conn)
            _setparam(sim, "IDENTIFICATION", "Name")
            sim.LoadDB(method)
            sim.Start(method)
            ok = sim.StatusID == 1101
            if ok and save_results:
                sim.SaveDB(method)     # virtual results -> physical database
                ok = sim.StatusID == 1500
                if not ok and verbose:
                    print(f"SaveDB failed: {sim.StatusID} {sim.StatusText}")
            if (not ok and verbose) or show_messages:
                if not ok:
                    print(f"Calculation failed: {sim.StatusText}")
                kinds = {1: "STATUS", 2: "INFO", 3: "WARNING", 4: "ERROR", 6: "TASK"}
                msgs = sim.Messages
                for i in range(1, msgs.Count + 1):
                    m = msgs.Item(i)
                    print(f"  [{kinds.get(m.Type, m.Type):>7}] {m.MessageId:>6}  {m.Text}"
                          + self._msg_objects(m))
        finally:
            sim = srv = None
        return ok


# ---------------------------------------------------------------------------
# Profiles and the QSTS loop
# ---------------------------------------------------------------------------

def load_profiles(folder: str, include: tuple = ("demand",)) -> dict:
    """
    Read every household CSV in `folder` -> {household: DataFrame[kW_load, kW_pv]}
    indexed by interval_start.  Input columns: interval_start, demand, solar
    (negative = generation), hot_water, air_con (all kW).

    kW_load = sum of the columns named in `include` (default just 'demand'; use
              include=("demand","hot_water","air_con") if those are separate
              from 'demand' in your data).
    kW_pv   = -solar, so generation is positive.
    """
    import glob
    import pandas as pd
    profiles = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.csv"))):
        df = pd.read_csv(path, parse_dates=["interval_start"], index_col="interval_start")
        df = df[~df.index.duplicated()].sort_index()
        profiles[os.path.splitext(os.path.basename(path))[0]] = pd.DataFrame({
            "kW_load": df[list(include)].sum(axis=1, min_count=1),
            "kW_pv": -df["solar"],
        })
    if not profiles:
        raise FileNotFoundError(f"no .csv files in {folder}")
    return profiles


def assign_profiles(sin_file: str, profiles: dict, mapping: dict | None = None) -> dict:
    """
    Pair each load in the network with a household profile.
    Returns {load_name: household}.  `mapping` ({load_name: household}) wins;
    any other load gets the next unused profile, in sorted order.  Each
    household's PV goes to the PV element on that load's node.
    """
    loads = list_loads(sin_file)
    names = [r["name"] for r in (loads.to_dict("records") if hasattr(loads, "to_dict") else loads)]
    out = dict(mapping or {})
    unused = [h for h in sorted(profiles) if h not in out.values()]
    for n in names:
        if n not in out:
            if not unused:
                raise ValueError(f"{len(names)} loads but only {len(profiles)} profiles; "
                                 "supply fewer loads or more CSVs")
            out[n] = unused.pop(0)
    missing = [h for h in out.values() if h not in profiles]
    if missing:
        raise KeyError(f"unknown household(s) in mapping: {missing}")
    return out


def common_index(profiles: dict, households) -> "pd.DatetimeIndex":
    """Timestamps present (non-NaN) in every one of the given households' profiles."""
    idx = None
    for h in households:
        ok = profiles[h].dropna().index
        idx = ok if idx is None else idx.intersection(ok)
    return idx


def read_node_voltages(sin_file: str):
    """
    Latest unbalanced-load-flow node results: DataFrame with node, phase and
    U1/U2/U3 as % of rated (V/Vn) plus kV.  Empty if no results are stored.
    """
    import pandas as pd
    con = sqlite3.connect(f"file:{db_path_from_sin(sin_file)}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT n.Name AS node, r.Flag_Phase AS phase, "
            "r.U1_Un, r.U2_Un, r.U3_Un, r.U1 AS U1_kV, r.U2 AS U2_kV, r.U3 AS U3_kV, "
            "r.Result_ID "
            "FROM ULFNodeResult r JOIN Node n ON n.Node_ID = r.Node_ID "
            "ORDER BY r.Result_ID", con)
        # Result_ID is a per-row key, not a per-run id: keep the newest row
        # for each node/phase in case old results are not overwritten.
        return df.drop_duplicates(["node", "phase"], keep="last").reset_index(drop=True)
    finally:
        con.close()


def run_qsts(sin_file: str, profile_folder: str, start=None, end=None, step: int = 1,
             mapping: dict | None = None, include: tuple = ("demand",),
             power_factor: float = 1.0, verbose: bool = True):
    """
    Quasi-static time series: for each time step set every load and PV from the
    profiles, solve, and record node voltages.

    start/end : optional timestamps (strings ok) clipping the common period
    step      : use every n-th profile interval (5-min data, step=6 -> 30 min)
    mapping   : {load_name: household} overrides; others are auto-assigned
    power_factor : loads and PV kvar = kW*tan(acos(pf)); 1.0 -> kvar 0 (the CSVs have kW only)

    Returns a long DataFrame: time, node, phase, U1_Un, U2_Un, U3_Un, ...
    Only intervals where every assigned household has data are run.
    """
    import pandas as pd
    net = SincalNetwork(sin_file)
    profiles = load_profiles(profile_folder, include)
    assign = assign_profiles(sin_file, profiles, mapping)

    loads = list_loads(sin_file)
    loads = loads.to_dict("records") if hasattr(loads, "to_dict") else loads
    pvs = list_pv(sin_file)
    pvs = pvs.to_dict("records") if hasattr(pvs, "to_dict") else pvs
    pv_at_node = {p["node"]: p["name"] for p in pvs}
    pairs = [(l["name"], pv_at_node.get(l["node"]), assign[l["name"]]) for l in loads]

    idx = common_index(profiles, {h for _, _, h in pairs})
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    idx = idx[::step]
    if len(idx) == 0:
        raise ValueError("no common timestamps across the assigned households")
    if verbose:
        print(f"{len(pairs)} households, {len(idx)} steps: {idx[0]} -> {idx[-1]}")

    tanphi = math.tan(math.acos(power_factor))
    frames = []
    for i, t in enumerate(idx):
        ld, pv = {}, {}
        for lname, pname, h in pairs:
            row = profiles[h].loc[t]
            ld[lname] = (row.kW_load, row.kW_load * tanphi)
            if pname:
                pv[pname] = (max(row.kW_pv, 0.0), max(row.kW_pv, 0.0) * tanphi)
        net.set_powers(loads=ld, pvs=pv)
        if not net.solve(verbose=verbose):
            print(f"  step {i} ({t}): no convergence / calc failed, skipped")
            continue
        v = read_node_voltages(sin_file)
        if v.empty:
            raise RuntimeError("solve succeeded but ULFNodeResult is empty - "
                               "results are not stored in the database")
        v.insert(0, "time", t)
        frames.append(v.drop(columns="Result_ID"))
        if verbose and (i + 1) % 10 == 0:
            print(f"  {i + 1}/{len(idx)}")
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python run_qsts.py <network.sin>")
    print_network(sys.argv[1])