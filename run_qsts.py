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
# Changing element parameters (needs PSS-SINCAL + pywin32)
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
    Read / change parameters of a load or VarShunt (PV) element.

    Two modes
    ---------
    persist=False (default): changes are made to SINCAL's in-memory
        calculation objects after LoadDB().  Nothing is written to the
        file, so this is the mode for QSTS (change values, solve, repeat).
        Changes are lost on close().
    persist=True: changes are written to the network database.  Close the
        network in the SINCAL GUI first (or Reload it afterwards).

    Example
    -------
        with SincalNetwork(SIN) as net:
            print(net.get("load", "LOAD_1", "P", "Q"))
            net.set_load("LOAD_1", kW=3.2, kvar=0.5)
            net.set_pv("PV_bus5", kW=4.1)          # 4.1 kW generation
            net.solve()

        # raw field names / native units (MW, Mvar, ...):
        net.set("pv", "PV_bus5", Plf=-0.004, Qlf=0.0)
    """

    def __init__(self, sin_file: str, persist: bool = False,
                 method: str = "LF_USYM"):
        self.sin_file = os.path.abspath(sin_file)
        self.db_file = db_path_from_sin(sin_file)
        self.persist = persist
        self.method = method
        self.conn = (f"TYP=NET;MODE=SQLITE;FILE={self.db_file};"
                     f"USR=Admin;PWD=;SINFILE={self.sin_file};")
        self._srv = self._sim = self._db = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> "SincalNetwork":
        try:
            import win32com.client as wc
        except ImportError:
            raise ImportError("pywin32 is required: pip install pywin32")
        self._srv = wc.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
        self._sim = self._srv.GetSimulation()
        self._sim.Database(self.conn)
        _setparam(self._sim, "IDENTIFICATION", "Name")
        if self.persist:
            self._db = self._sim.DB_EL()
            _setparam(self._db, "DBSYS_MODE", 8)             # SQLite
            _setparam(self._db, "DBSYS_DATAFILE", self.db_file)
            _setparam(self._db, "DBSYS_USER", "Admin")
            _setparam(self._db, "DBSYS_PASSWORD", "")
            _setparam(self._db, "NameIdentification", "Name")
            if self._db.OpenEx() != 0:
                raise RuntimeError("Could not open the network database")
        else:
            self._sim.LoadDB(self.method)
            if self._sim.StatusID != 1500:
                raise RuntimeError(f"LoadDB failed: {self._sim.StatusText}")
        return self

    def close(self) -> None:
        try:
            if self._db:
                self._db.Close()
        except Exception:
            pass
        self._db = self._sim = self._srv = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # -- low-level access --------------------------------------------------
    def _obj(self, kind: str, name: str):
        if self._sim is None:
            raise RuntimeError("Network not open - use 'with SincalNetwork(...)' or .open()")
        table = _KINDS[kind.lower()][0]
        try:
            if self.persist:
                obj = self._db.GetCommonObject(table, name)
            else:
                obj = self._sim.GetObj(table, name)
        except Exception as exc:
            raise KeyError(f"{kind} '{name}' not found ({exc})") from None
        if obj is None:
            raise KeyError(f"{kind} '{name}' not found")
        return obj

    def _read(self, obj, field: str):
        return obj.GetValue(field) if self.persist else obj.Item(field)

    def _write(self, obj, field: str, value) -> None:
        if self.persist:
            obj.SetValue(field, value)
        else:
            obj.SetItem(field, value)

    # -- public API --------------------------------------------------------
    def get(self, kind: str, name: str, *fields: str) -> dict:
        """Read raw fields (native units) of a 'load' or 'pv' element."""
        obj = self._obj(kind, name)
        return {f: self._read(obj, f) for f in fields}

    def set(self, kind: str, name: str, **values) -> dict:
        """
        Set raw fields (native units, e.g. P in MW) and verify by reading back.
        SINCAL silently ignores unknown field names, so a mismatch raises.
        """
        obj = self._obj(kind, name)
        for f, v in values.items():
            self._write(obj, f, v)
        if self.persist:
            obj.Update()
        result = {f: self._read(obj, f) for f in values}
        bad = []
        for f, want in values.items():
            got = result[f]
            try:
                ok = math.isclose(float(got), float(want), rel_tol=1e-6, abs_tol=1e-9)
            except (TypeError, ValueError):
                ok = (got == want)
            if not ok:
                bad.append(f"{f}: wrote {want}, read back {got}")
        if bad:
            raise RuntimeError(f"{kind} '{name}' did not accept: " + "; ".join(bad)
                               + "  (check the field name)")
        return result

    def _power(self, kind, name, kW, kvar):
        _, pf, qf, sign = _KINDS[kind]
        vals = {}
        if kW is not None:
            vals[pf] = sign * kW / 1000.0
        if kvar is not None:
            vals[qf] = sign * kvar / 1000.0
        return self.set(kind, name, **vals)

    def set_load(self, name: str, kW: float | None = None,
                 kvar: float | None = None) -> dict:
        """Set a load's active/reactive power (kW / kvar consumed)."""
        obj = self._obj("load", name)
        fmt = None
        for f in ("Flag_Lf", "Flag_LF"):
            try:
                fmt = self._read(obj, f)
                break
            except Exception:
                continue
        if fmt is not None and int(fmt) != 1:
            raise ValueError(
                f"Load '{name}' uses input format {fmt}, not 'P and Q' (1); "
                "set its per-phase fields with .set('load', name, P1=..., Q1=...)")
        return self._power("load", name, kW, kvar)

    def set_pv(self, name: str, kW: float | None = None,
               kvar: float | None = None) -> dict:
        """Set a PV VarShunt's output (kW / kvar *generated*, positive = injecting)."""
        return self._power("pv", name, kW, kvar)

    def solve(self, method: str | None = None) -> bool:
        """Run a calculation on the in-memory model (persist=False only)."""
        if self.persist:
            raise RuntimeError("solve() needs persist=False (in-memory mode)")
        self._sim.Start(method or self.method)
        ok = self._sim.StatusID == 1101
        if not ok:
            print(f"Calculation failed: {self._sim.StatusText}")
        return ok


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python run_qsts.py <network.sin>")
    print_network(sys.argv[1])