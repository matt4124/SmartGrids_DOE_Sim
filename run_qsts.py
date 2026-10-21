"""
run_qsts.py
===========

Quasi-static time series (QSTS) simulation of a PSS-SINCAL LV network.

* reads a folder of customer CSV profiles
      interval_start, demand, solar, hot_water, air_con   (kW, one row/interval)
* assigns one profile to every load in the network (and the same customer's
  solar profile to the PV element at that load's bus)
* for each interval sets the load / PV power, runs the unbalanced power flow
  (LF_USYM) and records voltages, transformer and line loading and what every
  load / PV element actually did

The CSVs have no kvar data, so reactive power of the loads is
``Q = P * tan(acos(load_pf))`` (default pf 0.95).

PV elements
-----------
Two element types are supported transparently:

* ``VarShuntElement`` (default, single-phase capable)
    Active power is stored in ``Plf`` (positive = consumption, so
    generation has a *negative* Plf).
* ``DCInfeeder`` (older networks / balanced three-phase)
    Active power is stored in ``P`` (positive = injection in the convention
    used by opendss_to_sincal.py).

The element type is discovered once from the SQLite database.  The
user-facing convention is ``+p = generation`` everywhere in this module;
the sign translation to ``Plf`` happens inside ``set_pv``.

The calculation settings are always cleared of the four alternative
load-data sources (load assignment / operating point / time series /
network development) before every ``LoadDB``.  A fresh SINCAL database can
have any of them set to 1; when the backing table is empty the LF then
silently sets every load's effective P to 0 and produces a "converged"
solve of a zero-injection network.

A ``_post_build_sanity`` test is run once, after opening the session,
that forces 1 MW onto the first load and rejects the run if no node voltage
drops below 240 V.  That is the only reliable way to distinguish a live
network from a silent-zero one.

Usage (command line)
--------------------
    python run_qsts.py  <network.sin>  <profiles_folder>
           [--start "2023-05-09 00:00"] [--intervals 288 | --hours 24]
           [--outdir DIR] [--seed 1] [--load-pf 0.95]
           [--demand-columns demand] [--power-unit kW|kWh]
           [--pv-scale as_is|peak] [--align timestamp|sequence]
           [--backend auto|com|db] [--v-max 253] [--v-min 216] [--no-plot]

Requires: Python 3.10+, pandas, pywin32, PSS-SINCAL V22.5 (COM version 28).
matplotlib is optional (overview plot).
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COM_VER = "28"            # PSS-SINCAL 22.5
SIM_OK = 1101             # StatusID: simulation finished without errors
PHASE_NAME = {1: "L1", 2: "L2", 3: "L3", 4: "L12", 5: "L23", 6: "L31",
              7: "L123"}

# User-facing sign of PV power: +1 = feeds the grid.
PV_P_SIGN = 1.0

# The four CalcParameter flags that override Load.P.  Setting any to 1
# while its backing table is empty makes the LF treat every load as 0 W.
ALT_DATA_SOURCE_PARAMS = (
    "LFDATA_USE_LOAD_ASSIGN",
    "LFDATA_USE_TIME_SERIES",
    "LFDATA_USE_OPERATING_POINT",
    "LFDATA_USE_LOAD_INCREASE",
)

NODE_FIELDS = ("Flag_Phase", "U1", "U2", "U3")
BRANCH_FIELDS = ("Flag_Phase", "P", "Q", "S", "P1", "P2", "P3", "Q1", "Q2",
                 "Q3", "Imax")


class BackendError(RuntimeError):
    """The chosen way of driving SINCAL does not work on this installation."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class QSTSConfig:
    network: str
    profiles_dir: str
    outdir: str | None = None
    start: str | None = None
    n_intervals: int = 288
    interval_minutes: float | None = None
    seed: int | None = 1
    method: str = "LF_USYM"
    demand_columns: tuple = ("demand",)
    solar_column: str = "solar"
    power_unit: str = "kW"
    load_pf: float = 0.95
    pv_scale: str = "as_is"
    align: str = "timestamp"
    max_gap: int = 3
    reuse_profiles: bool = True
    backend: str = "auto"
    virtual_db: bool = False
    v_max: float = 253.0
    v_min: float = 216.0
    max_failures: int = 5
    ignore_sign_check: bool = False
    plot: bool = True


# ---------------------------------------------------------------------------
# Network information (read-only from the SINCAL SQLite database)
# ---------------------------------------------------------------------------

@dataclass
class ElemInfo:
    element_id: int
    name: str
    node: str
    phase: int = 7
    flag_lf: int | None = None
    flag_state: int = 1
    rating_kw: float = 0.0


@dataclass
class NetworkInfo:
    db_file: str
    loads: list[ElemInfo] = field(default_factory=list)
    pvs: list[ElemInfo] = field(default_factory=list)
    lines: list[ElemInfo] = field(default_factory=list)
    trafos: list[ElemInfo] = field(default_factory=list)
    nodes: list[str] = field(default_factory=list)
    node_un_kv: dict[str, float] = field(default_factory=dict)
    pv_table: str = "DCInfeeder"
    node_ids: dict[str, int] = field(default_factory=dict)

    @property
    def lv_nodes(self) -> list[str]:
        lv = [n for n in self.nodes if 0 < self.node_un_kv.get(n, 0.4) < 1.0]
        return lv or list(self.nodes)


def _db_path(sin_file: str) -> str:
    base = sin_file[:-4] if sin_file.lower().endswith(".sin") else sin_file
    return os.path.join(base + "_files", "database.db")


def read_network(db_file: str) -> NetworkInfo:
    if not os.path.exists(db_file):
        raise FileNotFoundError(f"SINCAL database not found: {db_file}")
    con = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        net = NetworkInfo(db_file)

        un_by_level: dict[int, float] = {}
        try:
            for r in con.execute('SELECT VoltLevel_ID, Un FROM "VoltageLevel"'):
                un_by_level[r["VoltLevel_ID"]] = float(r["Un"])
        except sqlite3.Error:
            pass
        try:
            for r in con.execute('SELECT Node_ID, Name, VoltLevel_ID FROM "Node"'):
                net.nodes.append(r["Name"])
                net.node_ids[r["Name"]] = r["Node_ID"]
                if r["VoltLevel_ID"] in un_by_level:
                    net.node_un_kv[r["Name"]] = un_by_level[r["VoltLevel_ID"]]
        except sqlite3.Error:
            net.nodes = [r["Name"] for r in con.execute('SELECT Name FROM "Node"')]

        extra_lf: dict[int, int] = {}
        try:
            for r in con.execute('SELECT Element_ID, Flag_Lf FROM "Load"'):
                extra_lf[r["Element_ID"]] = int(r["Flag_Lf"])
        except sqlite3.Error:
            pass

        pv_table = None
        try:
            if con.execute('SELECT COUNT(*) FROM "VarShuntElement"').fetchone()[0] > 0:
                pv_table = "VarShuntElement"
        except sqlite3.Error:
            pass
        if pv_table is None:
            try:
                if con.execute('SELECT COUNT(*) FROM "DCInfeeder"').fetchone()[0] > 0:
                    pv_table = "DCInfeeder"
            except sqlite3.Error:
                pass
        net.pv_table = pv_table or "DCInfeeder"

        pv_rating: dict[int, float] = {}
        if pv_table == "VarShuntElement":
            try:
                for r in con.execute('SELECT Element_ID, Plf FROM "VarShuntElement"'):
                    pv_rating[r["Element_ID"]] = abs(float(r["Plf"] or 0.0)) * 1000.0
            except sqlite3.Error:
                pass
        elif pv_table == "DCInfeeder":
            try:
                for r in con.execute(
                        'SELECT Element_ID, Pmax, Sn_Inverter FROM "DCInfeeder"'):
                    kw = float(r["Pmax"] or 0.0) * 1000.0
                    pv_rating[r["Element_ID"]] = kw or float(r["Sn_Inverter"] or 0.0) * 1000.0
            except sqlite3.Error:
                pass

        trafo_sn: dict[int, float] = {}
        try:
            for r in con.execute('SELECT Element_ID, Sn FROM "TwoWindingTransformer"'):
                trafo_sn[r["Element_ID"]] = float(r["Sn"] or 0.0) * 1000.0
        except sqlite3.Error:
            pass

        tables_to_read = [("Load", net.loads)]
        if pv_table:
            tables_to_read.append((pv_table, net.pvs))
        tables_to_read += [("Line", net.lines),
                           ("TwoWindingTransformer", net.trafos)]

        for table, bucket in tables_to_read:
            try:
                rows = con.execute(
                    f'SELECT e.Element_ID AS eid, e.Name AS name, '
                    f'e.Flag_State AS fstate, '
                    f't.TerminalNo AS tno, t.Flag_Terminal AS ph, '
                    f'n.Name AS node FROM "{table}" x '
                    'JOIN "Element" e ON e.Element_ID = x.Element_ID '
                    'JOIN "Terminal" t ON t.Element_ID = e.Element_ID '
                    'JOIN "Node" n ON n.Node_ID = t.Node_ID '
                    'ORDER BY e.Element_ID, t.TerminalNo').fetchall()
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"cannot read table {table} from {db_file}: {exc}") from exc
            seen: set[int] = set()
            for r in rows:
                if r["eid"] in seen:
                    continue
                seen.add(r["eid"])
                info = ElemInfo(r["eid"], r["name"], r["node"], int(r["ph"] or 7))
                info.flag_state = int(r["fstate"] or 1)
                if table == "Load":
                    info.flag_lf = extra_lf.get(r["eid"])
                elif table in ("VarShuntElement", "DCInfeeder"):
                    info.rating_kw = pv_rating.get(r["eid"], 0.0)
                elif table == "TwoWindingTransformer":
                    info.rating_kw = trafo_sn.get(r["eid"], 0.0)
                bucket.append(info)

        if not net.loads:
            counts = {}
            for table in ("Load", "Element", "Terminal", "VarShuntElement",
                          "DCInfeeder", "Line"):
                try:
                    counts[table] = con.execute(
                        f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                except sqlite3.Error:
                    counts[table] = "n/a"
            print(f"  [warn] no loads found; rows per table: {counts}")
        return net
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Customer profiles
# ---------------------------------------------------------------------------

@dataclass
class Profile:
    name: str
    path: str
    load_kw: pd.Series
    pv_kw: pd.Series
    interval_min: float
    has_solar: bool
    peak_pv_kw: float


def _infer_interval_min(idx: pd.DatetimeIndex) -> float:
    if len(idx) < 2:
        return 5.0
    d = pd.Series(idx[1:] - idx[:-1]).dt.total_seconds().div(60.0)
    d = d[d > 0]
    return float(d.median()) if len(d) else 5.0


def load_profile_file(path: str, cfg: QSTSConfig) -> Profile:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "interval_start" not in df.columns:
        raise ValueError(f"{os.path.basename(path)}: no 'interval_start' column "
                         f"(columns: {list(df.columns)})")
    cols = [c.lower() for c in cfg.demand_columns]
    missing = [c for c in cols + [cfg.solar_column.lower()] if c not in df.columns]
    if missing:
        raise ValueError(f"{os.path.basename(path)}: missing column(s) {missing} "
                         f"(columns: {list(df.columns)})")

    ts = pd.to_datetime(df["interval_start"], errors="coerce")
    df = df.loc[ts.notna()].copy()
    df.index = pd.DatetimeIndex(ts[ts.notna()])
    df = df[~df.index.duplicated(keep="first")].sort_index()

    native = _infer_interval_min(df.index)
    scale = 60.0 / native if cfg.power_unit.lower() == "kwh" else 1.0
    demand = df[cols].apply(pd.to_numeric, errors="coerce").sum(axis=1, min_count=1) * scale
    solar = pd.to_numeric(df[cfg.solar_column.lower()], errors="coerce").abs() * scale

    peak = float(solar.max()) if len(solar) and solar.notna().any() else 0.0
    return Profile(os.path.basename(path), path, demand, solar, native,
                   has_solar=peak > 0.05, peak_pv_kw=peak)


def load_profiles(cfg: QSTSConfig) -> list[Profile]:
    files = sorted(f for f in os.listdir(cfg.profiles_dir) if f.lower().endswith(".csv"))
    if not files:
        raise FileNotFoundError(f"no .csv files in {cfg.profiles_dir}")
    out, skipped = [], []
    for f in files:
        try:
            out.append(load_profile_file(os.path.join(cfg.profiles_dir, f), cfg))
        except ValueError as exc:
            skipped.append(str(exc))
    for s in skipped[:5]:
        print(f"  [warn] skipped: {s}")
    if len(skipped) > 5:
        print(f"  [warn] ... and {len(skipped) - 5} more skipped files")
    if not out:
        raise ValueError("no usable profile files")
    return out


def to_interval(s: pd.Series, native: float, target: float) -> pd.Series:
    if abs(native - target) < 1e-6:
        return s
    rule = f"{int(round(target))}min"
    if native < target:
        return s.resample(rule).mean()
    return s.resample(rule).ffill()


# ---------------------------------------------------------------------------
# Assignment of profiles to loads / PV
# ---------------------------------------------------------------------------

@dataclass
class Assignment:
    load: ElemInfo
    profile: Profile
    pv: ElemInfo | None = None
    pv_profile: Profile | None = None


def assign_profiles(net: NetworkInfo, profiles: list[Profile],
                    cfg: QSTSConfig) -> tuple[list[Assignment], list[tuple[ElemInfo, Profile]]]:
    rng = random.Random(cfg.seed)
    solar_pool = [p for p in profiles if p.has_solar]
    plain_pool = [p for p in profiles if not p.has_solar]
    used: set[str] = set()
    reused = 0

    def pick(*pools: list[Profile]) -> Profile:
        nonlocal reused
        for pool in pools:
            cand = [p for p in pool if p.name not in used]
            if cand:
                p = rng.choice(cand)
                used.add(p.name)
                return p
        if not cfg.reuse_profiles:
            raise ValueError(f"{len(net.loads)} loads need profiles but only "
                             f"{len(profiles)} CSVs were found (reuse_profiles=False)")
        reused += 1
        pool = pools[0] or pools[-1] or profiles
        return rng.choice(pool or profiles)

    pv_by_node: dict[str, list[ElemInfo]] = {}
    for pv in net.pvs:
        pv_by_node.setdefault(pv.node, []).append(pv)

    pv_of_load: dict[str, ElemInfo] = {}
    taken: set[str] = set()
    for ld in net.loads:
        for pv in pv_by_node.get(ld.node, []):
            if pv.name not in taken and pv.phase == ld.phase:
                pv_of_load[ld.name] = pv
                taken.add(pv.name)
                break
        else:
            for pv in pv_by_node.get(ld.node, []):
                if pv.name not in taken:
                    pv_of_load[ld.name] = pv
                    taken.add(pv.name)
                    break

    loads = list(net.loads)
    rng.shuffle(loads)
    loads.sort(key=lambda l: l.name not in pv_of_load)

    result: list[Assignment] = []
    for ld in loads:
        pv = pv_of_load.get(ld.name)
        if pv is not None:
            prof = pick(solar_pool, plain_pool)
            donor = prof if prof.has_solar else (rng.choice(solar_pool) if solar_pool else None)
            result.append(Assignment(ld, prof, pv, donor))
        else:
            result.append(Assignment(ld, pick(plain_pool, solar_pool)))
    result.sort(key=lambda a: a.load.name)

    orphans = []
    for pv in net.pvs:
        if pv.name not in taken:
            if not solar_pool:
                raise ValueError("PV elements exist but no CSV contains solar data")
            orphans.append((pv, rng.choice(solar_pool)))

    if reused:
        print(f"  [warn] {reused} load(s) reuse a CSV used by another load "
              f"({len(profiles)} files for {len(net.loads)} loads)")
    for a in result:
        if a.pv is not None and a.pv_profile is None:
            print(f"  [warn] {a.pv.name}: no CSV with solar data - PV output will be zero")
        elif a.pv is not None and a.pv_profile is not a.profile:
            print(f"  [note] {a.pv.name}: customer {a.profile.name} has no solar, "
                  f"using the solar series of {a.pv_profile.name}")
    return result, orphans


# ---------------------------------------------------------------------------
# Time axis and arrays
# ---------------------------------------------------------------------------

def build_timeline(used: list[Profile], cfg: QSTSConfig,
                   interval: float) -> pd.DatetimeIndex:
    freq = f"{int(round(interval))}min"
    first_data = max(p.load_kw.index[0] for p in used)
    last_data = min(p.load_kw.index[-1] for p in used)

    if cfg.start is not None:
        start = pd.Timestamp(cfg.start)
        end = start + pd.Timedelta(freq) * (cfg.n_intervals - 1)
        if end < first_data or start > last_data:
            latest_start = (last_data
                            - pd.Timedelta(freq) * (cfg.n_intervals - 1)).floor(freq)
            raise ValueError(
                f"requested window {start} -> {end} does not overlap the "
                f"profile data ({first_data} -> {last_data}).\n"
                f"  Pick start between {first_data.ceil(freq)} and "
                f"{latest_start},\n"
                f"  or pass start=None to use the intersection automatically.")
        if start < first_data or end > last_data:
            print(f"  [warn] requested window {start} -> {end} is only "
                  f"partially covered by the data ({first_data} -> {last_data}); "
                  f"missing intervals will be forward-filled then zeroed.")
        return pd.date_range(start, periods=cfg.n_intervals, freq=freq)

    start = first_data.ceil(freq)
    avail = int((last_data - start) / pd.Timedelta(freq)) + 1
    if cfg.align == "timestamp" and avail < cfg.n_intervals:
        rng = "\n".join(f"    {p.name}: {p.load_kw.index[0]} -> {p.load_kw.index[-1]}"
                        for p in used[:10])
        raise ValueError(
            f"the assigned CSVs overlap for only {avail} intervals "
            f"({start} -> {last_data}) but {cfg.n_intervals} were requested.\n{rng}\n"
            f"  Reduce n_intervals, pass start=..., or use align='sequence'.")
    return pd.date_range(start, periods=cfg.n_intervals, freq=freq)


def aligned_series(p: Profile, which: str, timeline: pd.DatetimeIndex,
                   interval: float, cfg: QSTSConfig) -> pd.Series:
    s = p.load_kw if which == "load" else p.pv_kw
    s = to_interval(s, p.interval_min, interval)
    if cfg.align == "sequence":
        vals = s.iloc[:len(timeline)]
        if len(vals) < len(timeline):
            raise ValueError(f"{p.name} has only {len(vals)} intervals, "
                             f"{len(timeline)} requested")
        out = pd.Series(vals.to_numpy(), index=timeline)
    else:
        out = s.reindex(timeline)
        n_missing = int(out.isna().sum())
        if n_missing == len(out):
            raise ValueError(f"{p.name} has no data in {timeline[0]} -> {timeline[-1]} "
                             f"(file covers {p.load_kw.index[0]} -> {p.load_kw.index[-1]})")
        out = out.ffill(limit=cfg.max_gap)
    n_left = int(out.isna().sum())
    if n_left:
        print(f"  [warn] {p.name} ({which}): {n_left} interval(s) missing, set to 0")
    return out.fillna(0.0).clip(lower=0.0)


# ---------------------------------------------------------------------------
# Simulation back ends
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    ok: bool
    msg: str = ""
    node_v: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    elem_pq: dict[str, tuple[float, float]] = field(default_factory=dict)
    trafo: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    line_i: dict[str, float] = field(default_factory=dict)


def _f(rec: dict, key: str, default: float = 0.0) -> float:
    try:
        v = float(rec.get(key))
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def reduce_node(records: list[dict]) -> tuple[float, float, float]:
    v = [0.0, 0.0, 0.0]
    for r in records:
        for k, key in enumerate(("U1", "U2", "U3")):
            x = _f(r, key)
            if abs(x) > abs(v[k]):
                v[k] = x
    return (1000.0 * v[0], 1000.0 * v[1], 1000.0 * v[2])


def reduce_branch(records: list[dict]) -> tuple[float, float, float]:
    if not records:
        return (math.nan, math.nan, math.nan)
    tot = [r for r in records if int(_f(r, "Flag_Phase", 7)) == 7]
    if tot:
        use = tot[:1]
    else:
        per_ph = [r for r in records if int(_f(r, "Flag_Phase", 7)) in (1, 2, 3)]
        use = per_ph if len(per_ph) > 1 else records[:1]
    p = q = 0.0
    for r in use:
        ps = _f(r, "P1") + _f(r, "P2") + _f(r, "P3")
        qs = _f(r, "Q1") + _f(r, "Q2") + _f(r, "Q3")
        p += ps if ps != 0 else _f(r, "P")
        q += qs if qs != 0 else _f(r, "Q")
    i = max(_f(r, "Imax") for r in records)
    return (p * 1000.0, q * 1000.0, i * 1000.0)


def _messages(sim) -> str:
    kinds = {1: "STATUS", 2: "INFO", 3: "WARNING", 4: "ERROR", 6: "TASK"}
    out = []
    try:
        msgs = sim.Messages
        for i in range(1, msgs.Count + 1):
            m = msgs.Item(i)
            if m.Type in (3, 4):
                out.append(f"[{kinds.get(m.Type, m.Type)}] {m.MessageId}: {m.Text}")
    except Exception as exc:
        out.append(f"(could not read messages: {exc})")
    return "; ".join(dict.fromkeys(out)) or "(no warning/error messages)"


def _call(obj, attr: str, *args):
    a = getattr(obj, attr)
    return a(*args) if callable(a) else a





class Session:
    kind = "?"
    pv_table = "DCInfeeder"

    def set_load(self, name: str, p_mw: float, q_mvar: float) -> None: ...
    def set_pv(self, name: str, p_mw: float, pmax_mw: float) -> None: ...
    def solve(self) -> tuple[bool, str]: ...
    def read(self, net: NetworkInfo) -> StepResult: ...
    def close(self) -> None: ...

    def probe(self, net: NetworkInfo) -> None: ...


class ComSession(Session):
    kind = "com"
    pv_table = "DCInfeeder"

    def __init__(self, sin_file: str, db_file: str, method: str,
                 virtual_db: bool = False):
        try:
            import win32com.client
        except ImportError as exc:
            raise BackendError("pywin32 is required (pip install pywin32)") from exc
        self._w = win32com.client
        self.method = method
        self.conn = (f"TYP=NET;MODE=SQLITE;FILE={db_file};USR=Admin;PWD=;"
                     f"SINFILE={sin_file};")
        try:
            self.srv = self._w.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
            self.sim = self.srv.GetSimulation()
            if virtual_db:
                self.sim.BatchMode(1)
            self.sim.Database(self.conn)
            self.sim.SetParameter("IDENTIFICATION", "Name")
            self.sim.LoadDB(method)
            self.sim.Start(method)
        except Exception as exc:
            raise BackendError(f"could not start the SINCAL simulation: {exc}") from exc

    def _get(self, kind: str, name: str):
        ids = self.node_ids if kind == "Node" else self.elem_ids
        keys = ([ids[name]] if name in ids else []) + [name]     # DB id first, then name
        for k in (kind, kind.upper()):
            for key in keys:
                try:
                    o = self.sim.GetObj(k, key)
                except Exception:
                    o = None
                if o is not None:
                    return o
        raise BackendError(f"calculation object {kind} '{name}' not found")

    def _records(self, obj, table: str, term: int, fields: tuple) -> list[dict]:
        try:
            res = obj.Result(table, term)
        except Exception:
            return []
        if res is None:
            return []
        try:
            n = int(_call(res, "RecordCount"))
            out = []
            if n > 0:
                _call(res, "MoveFirst")
            for _ in range(n):
                rec = {}
                for key in fields:
                    try:
                        rec[key] = res.Item(key)
                    except Exception:
                        pass
                out.append(rec)
                _call(res, "MoveNext")
            return out
        finally:
            res = None

    def probe(self, net: NetworkInfo) -> None:
        if net.loads:
            target, kind = net.loads[0], "Load"
        elif net.pvs:
            target, kind = net.pvs[0], net.pv_table
        else:
            raise BackendError("network has no loads or PV elements")
        try:
            o = self._get(kind, target.name)
            if kind == "VarShuntElement":
                v = o.Item("Plf")
                o.SetItem("Plf", float(v))
                if abs(float(o.Item("Plf")) - float(v)) > 1e-12:
                    raise BackendError("SetItem did not stick on VarShuntElement.Plf")
            else:
                v = o.Item("P")
                o.SetItem("P", float(v))
                if abs(float(o.Item("P")) - float(v)) > 1e-12:
                    raise BackendError("SetItem did not stick")
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError(f"SetItem/Item on {kind} failed: {exc}") from exc

    def set_load(self, name, p_mw, q_mvar):
        o = self._get("Load", name)
        o.SetItem("P", float(p_mw))
        o.SetItem("Q", float(q_mvar))
        try:
            o.SetItem("fP", 1.0)
            o.SetItem("fQ", 1.0)
        except Exception:
            pass

    def set_pv(self, name, p_mw, pmax_mw):
        if self.pv_table == "VarShuntElement":
            o = self._get("VarShuntElement", name)
            o.SetItem("Plf", -float(p_mw))
            o.SetItem("Qlf", 0.0)
            try:
                o.SetItem("f_p_k", 1.0)
                o.SetItem("f_q_k", 1.0)
            except Exception:
                pass
        else:
            o = self._get("DCInfeeder", name)
            o.SetItem("P", float(p_mw))
            o.SetItem("fP", 1.0)
            o.SetItem("Pmax", float(pmax_mw))

    def solve(self):
        # _clear_alt_data_sources(self.sim)   # in case a prior run reset them
        self.sim.Start(self.method)
        status = int(self.sim.StatusID)
        if status == SIM_OK:
            return True, ""
        return False, f"status {status}: {_messages(self.sim)}"

    def read(self, net):
        r = StepResult(True)
        for n in net.lv_nodes:
            recs = self._records(self._get("Node", n), "ULFNODERESULT", 1, NODE_FIELDS)
            r.node_v[n] = reduce_node(recs)
        for e in net.loads:
            recs = self._records(self._get("Load", e.name), "ULFBRANCHRESULT", 1, BRANCH_FIELDS)
            r.elem_pq[e.name] = reduce_branch(recs)[:2]
        for e in net.pvs:
            recs = self._records(self._get(net.pv_table, e.name),
                                 "ULFBRANCHRESULT", 1, BRANCH_FIELDS)
            r.elem_pq[e.name] = reduce_branch(recs)[:2]
        for e in net.trafos:
            recs = self._records(self._get("TwoWindingTransformer", e.name),
                                 "ULFBRANCHRESULT", 1, BRANCH_FIELDS)
            p, q, _ = reduce_branch(recs)
            r.trafo[e.name] = (p, q, math.hypot(p, q))
        for e in net.lines:
            recs = self._records(self._get("Line", e.name), "ULFBRANCHRESULT", 1, BRANCH_FIELDS)
            r.line_i[e.name] = reduce_branch(recs)[2]
        return r

    def close(self):
        self.sim = None
        self.srv = None


class DbSession(Session):
    kind = "db"
    pv_table = "DCInfeeder"

    def __init__(self, sin_file: str, db_file: str, method: str):
        try:
            import win32com.client
        except ImportError as exc:
            raise BackendError("pywin32 is required (pip install pywin32)") from exc
        self._w = win32com.client
        self.method = method
        self.db_file = db_file
        self.conn = (f"TYP=NET;MODE=SQLITE;FILE={db_file};USR=Admin;PWD=;"
                     f"SINFILE={sin_file};")
        self.loads: dict[str, tuple[float, float]] = {}
        self.pvs: dict[str, tuple[float, float]] = {}

    def probe(self, net):
        con = sqlite3.connect(self.db_file, timeout=30)
        try:
            con.execute('SELECT COUNT(*) FROM "ULFNodeResult"').fetchone()
        finally:
            con.close()

    def set_load(self, name, p_mw, q_mvar):
        self.loads[name] = (p_mw, q_mvar)

    def set_pv(self, name, p_mw, pmax_mw):
        self.pvs[name] = (p_mw, pmax_mw)

    def _flush(self):
        con = sqlite3.connect(self.db_file, timeout=30)
        try:
            eid = '(SELECT Element_ID FROM "Element" WHERE Name=?)'

            load_cols = {r[1].lower(): r[1] for r in
                         con.execute('PRAGMA table_info("Load")')}
            fp_col = load_cols.get("fp")
            fq_col = load_cols.get("fq")
            load_extra = ""
            if fp_col:
                load_extra += f', "{fp_col}"=1.0'
            if fq_col:
                load_extra += f', "{fq_col}"=1.0'

            for name, (p, q) in self.loads.items():
                con.execute(
                    f'UPDATE "Load" SET P=?, Q=?{load_extra} '
                    f'WHERE Element_ID={eid}', (p, q, name))

            if self.pv_table == "VarShuntElement":
                cols = {r[1].lower(): r[1] for r in
                        con.execute('PRAGMA table_info("VarShuntElement")')}
                plf_col = cols.get("plf") or cols.get("pif")
                qlf_col = cols.get("qlf") or cols.get("qif")
                fpk_col = cols.get("f_p_k") or cols.get("fpk")
                fqk_col = cols.get("f_q_k") or cols.get("fqk")
                if not plf_col:
                    raise BackendError(
                        "cannot find the VarShuntElement power column "
                        "(expected 'Plf' or 'PIf')")
                extra = ""
                if fpk_col:
                    extra += f', "{fpk_col}"=1.0'
                if fqk_col:
                    extra += f', "{fqk_col}"=1.0'
                for name, (p, _pmax) in self.pvs.items():
                    if qlf_col:
                        con.execute(
                            f'UPDATE "VarShuntElement" '
                            f'SET "{plf_col}"=?, "{qlf_col}"=0{extra} '
                            f'WHERE Element_ID={eid}', (-p, name))
                    else:
                        con.execute(
                            f'UPDATE "VarShuntElement" '
                            f'SET "{plf_col}"=?{extra} '
                            f'WHERE Element_ID={eid}', (-p, name))
            else:
                for name, (p, pmax) in self.pvs.items():
                    con.execute(
                        f'UPDATE "DCInfeeder" '
                        f'SET P=?, fP=1.0, Pmax=? WHERE Element_ID={eid}',
                        (p, pmax, name))

            # Belt-and-braces: keep the alt data sources off in case anything
            # has re-enabled them since the last build.
            cp_cols = {r[1].lower(): r[1] for r in
                       con.execute('PRAGMA table_info("CalcParameter")')}
            updates = []
            for field in ("flag_usela", "flag_useopser",
                          "flag_usetimeser", "flag_useincser"):
                if field in cp_cols:
                    updates.append(f'"{cp_cols[field]}"=0')
            if updates:
                con.execute('UPDATE "CalcParameter" SET ' + ', '.join(updates))
                # Diagnostic: read back what actually ended up in the DB
                #row = con.execute(
                #    'SELECT * FROM "CalcParameter" LIMIT 1').fetchone()
                #cols = [d[0] for d in con.description]
                #got = {c: v for c, v in zip(cols, row)
                #       if c.lower() in ("flag_usela", "flag_useopser",
                #                        "flag_usetimeser", "flag_useincser")}
                #print(f"    [diag] CalcParameter flags: {got}")

            con.execute('DELETE FROM "ULFNodeResult"')
            con.execute('DELETE FROM "ULFBranchResult"')
            # LF_USYM uses the balanced LF results as a warm start.
            # Stale values from a prior calculation confuse the solver
            # and can trigger error 2534 when they contradict the new P.
            con.execute('DELETE FROM "LFNodeResult"')
            con.execute('DELETE FROM "LFBranchResult"')
            con.commit()
            con.commit()
        finally:
            con.close()

    def solve(self):
        self._flush()
        sim = None
        srv = None
        try:
            srv = self._w.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
            sim = srv.GetSimulation()
            sim.Database(self.conn)
            sim.SetParameter("IDENTIFICATION", "Name")
            # _clear_alt_data_sources(sim)      # <-- critical, before LoadDB
            sim.LoadDB(self.method)
            sim.Start(self.method)
            status = int(sim.StatusID)
            msg = "" if status == SIM_OK else f"status {status}: {_messages(sim)}"
        except Exception as exc:
            return False, f"COM error: {exc}"
        finally:
            sim = None
            srv = None
        return status == SIM_OK, msg

    def read(self, net):
        r = StepResult(True)
        con = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            by_node: dict[str, list[dict]] = {}
            for row in con.execute(
                    'SELECT n.Name AS nm, r.* FROM "ULFNodeResult" r '
                    'JOIN "Node" n ON n.Node_ID = r.Node_ID'):
                by_node.setdefault(row["nm"], []).append(dict(row))
            by_elem: dict[str, list[dict]] = {}
            for row in con.execute(
                    'SELECT e.Name AS nm, r.* FROM "ULFBranchResult" r '
                    'JOIN "Terminal" t ON t.Terminal_ID = r.Terminal1_ID '
                    'JOIN "Element" e ON e.Element_ID = t.Element_ID'):
                by_elem.setdefault(row["nm"], []).append(dict(row))
        finally:
            con.close()
        if not by_node:
            raise BackendError(
                "the calculation stored no unbalanced node results in the "
                "database (check the calculation settings 'Store Results')")
        for n in net.lv_nodes:
            r.node_v[n] = reduce_node(by_node.get(n, []))
        for e in net.loads + net.pvs:
            r.elem_pq[e.name] = reduce_branch(by_elem.get(e.name, []))[:2]
        for e in net.trafos:
            p, q, _ = reduce_branch(by_elem.get(e.name, []))
            r.trafo[e.name] = (p, q, math.hypot(p, q))
        for e in net.lines:
            r.line_i[e.name] = reduce_branch(by_elem.get(e.name, []))[2]
        return r


def open_session(cfg: QSTSConfig, sin_file: str, db_file: str,
                 net: NetworkInfo) -> Session:
    order = {"auto": ("com", "db"), "com": ("com",), "db": ("db",)}[cfg.backend]
    last: Exception | None = None
    for kind in order:
        try:
            s = (ComSession(sin_file, db_file, cfg.method, cfg.virtual_db)
                 if kind == "com"
                 else DbSession(sin_file, db_file, cfg.method))
            s.pv_table = net.pv_table
            if kind == "com":
                s.elem_ids = {e.name: e.element_id
                              for e in net.loads + net.pvs + net.lines + net.trafos}
                s.node_ids = net.node_ids
            s.probe(net)
            print(f"    back end     : {kind} "
                  f"({'calculation objects' if kind == 'com' else 'database rewrite + reload'})")
            return s
        except BackendError as exc:
            print(f"  [note] back end '{kind}' unavailable: {exc}")
            last = exc
    raise BackendError(f"no usable back end: {last}")


# ---------------------------------------------------------------------------
# Step context
# ---------------------------------------------------------------------------

class StepContext:
    def __init__(self, i: int, ts: pd.Timestamp, load_kw: dict[str, float],
                 pv_avail_kw: dict[str, float], pv_rated_kw: dict[str, float]):
        self.i = i
        self.ts = ts
        self.load_kw = load_kw
        self.pv_avail_kw = pv_avail_kw
        self.pv_limit_kw = dict(pv_rated_kw)

    def limit_pv(self, name: str, kw: float) -> None:
        if name not in self.pv_limit_kw:
            raise KeyError(f"unknown PV converter '{name}'")
        self.pv_limit_kw[name] = max(0.0, float(kw))


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
EPS_MW = 1e-6   # 1 W: SINCAL rejects exactly-zero P/Q on loads and var-shunts

def _apply_step(session: Session, ctx: StepContext, pv_names: list[str],
                load_names: list[str], q_factor: float) -> None:
    for n in load_names:
        p = max(ctx.load_kw[n] / 1000.0, EPS_MW)
        session.set_load(n, p, p * q_factor)
    for n in pv_names:
        lim = ctx.pv_limit_kw[n]
        p = max(min(ctx.pv_avail_kw[n], lim) / 1000.0, EPS_MW)
        session.set_pv(n, PV_P_SIGN * p, max(lim / 1000.0, 1e-6))


def _sign_check(session: Session, net: NetworkInfo, loads: dict[str, pd.Series],
                pvs: dict[str, pd.Series], pv_rated: dict[str, float],
                q_factor: float, timeline: pd.DatetimeIndex) -> None:
    if not net.trafos or not pvs:
        return
    tot_l = sum(s.to_numpy() for s in loads.values())
    tot_p = sum(s.to_numpy() for s in pvs.values())
    overlap = pd.Series([min(a, b) for a, b in zip(tot_l, tot_p)])
    j = int(overlap.idxmax())
    if overlap.iloc[j] < 1.0:
        print("    sign check   : skipped (PV and load never overlap by 1 kW)")
        return
    ctx = StepContext(j, timeline[j], {n: s.iloc[j] for n, s in loads.items()},
                      {n: s.iloc[j] for n, s in pvs.items()}, pv_rated)
    _apply_step(session, ctx, list(pvs), list(loads), q_factor)
    ok, msg = session.solve()
    if not ok:
        print(f"    sign check   : skipped (pre-flight solve failed: {msg})")
        return
    res = session.read(net)
    measured = abs(next(iter(res.trafo.values()))[0])
    a = abs(tot_l[j] - tot_p[j])
    b = tot_l[j] + tot_p[j]
    print(f"    sign check   : {timeline[j]}  load {tot_l[j]:.1f} kW, PV {tot_p[j]:.1f} kW, "
          f"transformer {measured:.1f} kW (expect ~{a:.1f}; ~{b:.1f} would mean PV is "
          f"acting as load)")
    if abs(measured - b) < abs(measured - a):
        msg = ("PV appears to be modelled as LOAD (transformer flow is closer to "
               f"load+PV than to load-PV).  Check the sign convention: PV_P_SIGN "
               f"in this module is {PV_P_SIGN:+.0f}.")
        print(f"  [ERROR] {msg}")
        raise RuntimeError(msg)

def _post_build_sanity(session, net, q_factor, load_names, pv_names, test_kw=10.0):
    if not load_names:
        return
    test_load = load_names[0]

    def vmin_with(kw):
        ctx = StepContext(0, pd.Timestamp("2000-01-01"),
                          {n: (kw if n == test_load else 0.0) for n in load_names},
                          {n: 0.0 for n in pv_names},
                          {n: 5.0 for n in pv_names})
        _apply_step(session, ctx, pv_names, load_names, q_factor)
        ok, msg = session.solve()
        if not ok:
            raise RuntimeError(f"sanity solve failed ({kw:g} kW on '{test_load}'): {msg}")
        res = session.read(net)
        v = [x for vs in res.node_v.values() for x in vs if x > 1.0]
        return min(v) if v else math.nan

    v0, v1 = vmin_with(0.0), vmin_with(test_kw)
    if not (v0 - v1 > 0.05):
        raise RuntimeError(f"sanity failed: Vmin {v0:.2f} V at no load vs {v1:.2f} V "
                           f"with {test_kw:g} kW on '{test_load}' - loads look ignored.")
    print(f"    post-build   : Vmin {v0:.1f} -> {v1:.1f} V with {test_kw:g} kW on "
          f"'{test_load}' (load values are being honoured)")


def run_qsts(cfg: QSTSConfig,
             step_hook: Callable[[StepContext], None] | None = None,
             session: Session | None = None) -> dict[str, pd.DataFrame]:
    t_start = time.time()
    sin_file = os.path.abspath(cfg.network)
    db_file = _db_path(sin_file)
    outdir = cfg.outdir or os.path.join(os.path.dirname(sin_file), "qsts_results")
    os.makedirs(outdir, exist_ok=True)

    print(f"=== 1. Network: {sin_file}")
    net = read_network(db_file)
    print(f"    loads {len(net.loads)}, PV elements {len(net.pvs)}, lines {len(net.lines)}, "
          f"transformers {len(net.trafos)}, nodes {len(net.nodes)}")
    print(f"    PV element  : {net.pv_table}")
    if not net.loads:
        raise ValueError("the network has no loads")
    bad_fmt = [l.name for l in net.loads if l.flag_lf not in (None, 1)]
    if bad_fmt:
        raise ValueError(
            f"{len(bad_fmt)} load(s) use an input format other than 'P and Q' "
            f"(e.g. {bad_fmt[0]}). Rebuild the network with the latest "
            f"opendss_to_sincal.py (single-phase loads are P and Q on a phase terminal).")

    off = [e.name for e in net.loads + net.pvs if e.flag_state == 0]
    if off:
        raise ValueError(
            f"{len(off)} load/PV element(s) have Flag_State = 0 (out of service); "
            f"e.g. '{off[0]}'.  The QSTS would silently solve a de-energised "
            f"network.  Rebuild with the latest opendss_to_sincal.py.")

    ph_count = {}
    for l in net.loads:
        ph_count[PHASE_NAME.get(l.phase, l.phase)] = ph_count.get(PHASE_NAME.get(l.phase, l.phase), 0) + 1
    print("    load phases  : " + ", ".join(f"{k}={v}" for k, v in sorted(ph_count.items())))
    if set(ph_count) == {"L123"}:
        print("  [warn] every load is connected L123 (balanced three-phase) - the "
              "network was probably built without single-phase connections")

    print(f"\n=== 2. Profiles: {cfg.profiles_dir}")
    profiles = load_profiles(cfg)
    natives = pd.Series([p.interval_min for p in profiles])
    interval = float(cfg.interval_minutes or natives.mode().iloc[0])
    print(f"    {len(profiles)} CSVs, {sum(p.has_solar for p in profiles)} with solar, "
          f"interval {interval:g} min, unit {cfg.power_unit}, "
          f"demand = {'+'.join(cfg.demand_columns)}")

    assigned, orphans = assign_profiles(net, profiles, cfg)
    used: dict[str, Profile] = {}
    for a in assigned:
        used[a.profile.name] = a.profile
        if a.pv_profile is not None:
            used[a.pv_profile.name] = a.pv_profile
    for _, p in orphans:
        used[p.name] = p
    timeline = build_timeline(list(used.values()), cfg, interval)
    print(f"    window       : {timeline[0]} -> {timeline[-1]}  "
          f"({len(timeline)} intervals of {interval:g} min = "
          f"{len(timeline) * interval / 60:.1f} h)")

    q_factor = math.tan(math.acos(min(max(cfg.load_pf, 0.01), 1.0))) if cfg.load_pf < 1 else 0.0
    loads: dict[str, pd.Series] = {}
    pvs: dict[str, pd.Series] = {}
    pv_rated: dict[str, float] = {}
    for pv in net.pvs:
        pv_rated[pv.name] = pv.rating_kw if pv.rating_kw > 0 else 5.0

    def pv_series(p: Profile, pv: ElemInfo) -> pd.Series:
        s = aligned_series(p, "pv", timeline, interval, cfg)
        if cfg.pv_scale == "peak" and p.peak_pv_kw > 0:
            s = s * (pv_rated[pv.name] / p.peak_pv_kw)
        return s

    rows = []
    for a in assigned:
        loads[a.load.name] = aligned_series(a.profile, "load", timeline, interval, cfg)
        if a.pv is not None:
            pvs[a.pv.name] = (pv_series(a.pv_profile, a.pv) if a.pv_profile is not None
                              else pd.Series(0.0, index=timeline))
        rows.append(dict(load=a.load.name, node=a.load.node,
                         phase=PHASE_NAME.get(a.load.phase, a.load.phase),
                         profile=a.profile.name, pv=a.pv.name if a.pv else "",
                         pv_profile=a.pv_profile.name if a.pv_profile else "",
                         mean_load_kw=round(float(loads[a.load.name].mean()), 3)))
    for pv, p in orphans:
        pvs[pv.name] = pv_series(p, pv)
        rows.append(dict(load="", node=pv.node, phase=PHASE_NAME.get(pv.phase, pv.phase),
                         profile="", pv=pv.name, pv_profile=p.name, mean_load_kw=0.0))
    assignment = pd.DataFrame(rows)
    assignment.to_csv(os.path.join(outdir, "assignment.csv"), index=False)

    load_names = list(loads)
    pv_names = list(pvs)
    tot_load = sum(s.to_numpy() for s in loads.values())
    tot_pv = sum(s.to_numpy() for s in pvs.values()) if pvs else None
    print(f"    peak load    : {tot_load.max():.1f} kW"
          + (f",  peak PV {tot_pv.max():.1f} kW" if tot_pv is not None else ""))
    print(f"    load Q       : pf {cfg.load_pf:g} lagging (CSV has no kvar)")

    print(f"\n=== 3. Simulation ({cfg.method})")
    own_session = session is None
    if session is None:
        session = open_session(cfg, sin_file, db_file, net)
    else:
        session.pv_table = net.pv_table
    # Mandatory safety net: any silent-zero failure must be caught here,
    # before 288 intervals of flat voltage are written to disk.
    _post_build_sanity(session, net, q_factor, load_names, pv_names)
    if not cfg.ignore_sign_check:
        _sign_check(session, net, loads, pvs, pv_rated, q_factor, timeline)

    summary, node_rows, elem_rows = [], [], []
    fails = fails_in_row = 0
    n = len(timeline)
    step_every = max(1, n // 10)
    try:
        for i, ts in enumerate(timeline):
            ctx = StepContext(i, ts, {k: float(loads[k].iloc[i]) for k in load_names},
                              {k: float(pvs[k].iloc[i]) for k in pv_names}, pv_rated)
            if step_hook:
                step_hook(ctx)
            _apply_step(session, ctx, pv_names, load_names, q_factor)
            ok, msg = session.solve()
            res = session.read(net) if ok else StepResult(False, msg)
            row = dict(timestamp=ts, converged=ok, load_kw=sum(ctx.load_kw.values()),
                       pv_avail_kw=sum(ctx.pv_avail_kw.values()),
                       pv_limit_kw=sum(min(ctx.pv_avail_kw[k], ctx.pv_limit_kw[k]) for k in pv_names))
            if not ok:
                fails += 1
                fails_in_row += 1
                row["message"] = msg
                summary.append(row)
                print(f"  [warn] {ts}: not converged - {msg}")
                if fails_in_row >= cfg.max_failures:
                    raise RuntimeError(f"{fails_in_row} consecutive failed intervals - aborting")
                continue
            fails_in_row = 0

            volts = [(v, nm) for nm, vs in res.node_v.items() for v in vs if v > 1.0]
            if volts:
                vmin, nmin = min(volts)
                vmax, nmax = max(volts)
            else:
                vmin = vmax = math.nan
                nmin = nmax = ""
            over = sum(1 for vs in res.node_v.values() if any(v > cfg.v_max for v in vs))
            under = sum(1 for vs in res.node_v.values() if any(1.0 < v < cfg.v_min for v in vs))
            t_name, (tp, tq, ts_kva) = next(iter(res.trafo.items()), ("", (math.nan,) * 3))
            sn = next((t.rating_kw for t in net.trafos if t.name == t_name), 0.0)
            li_name, li = max(res.line_i.items(), key=lambda kv: (kv[1] if math.isfinite(kv[1]) else -1),
                              default=("", math.nan))
            pv_out = sum(abs(res.elem_pq[k][0]) for k in pv_names)
            pv_q = sum(res.elem_pq[k][1] for k in pv_names)
            row.update(pv_out_kw=pv_out, pv_q_kvar=pv_q,
                       trafo_p_kw=tp, trafo_q_kvar=tq, trafo_s_kva=ts_kva,
                       trafo_loading_pct=100.0 * ts_kva / sn if sn else math.nan,
                       v_min=vmin, v_min_node=nmin, v_max=vmax, v_max_node=nmax,
                       nodes_over_vmax=over, nodes_under_vmin=under,
                       max_line_a=li, max_line=li_name)
            summary.append(row)
            for nm, vs in res.node_v.items():
                node_rows.append(dict(timestamp=ts, node=nm, v_l1=vs[0], v_l2=vs[1], v_l3=vs[2]))
            for k in load_names:
                elem_rows.append(dict(timestamp=ts, element=k, kind="load",
                                      p_set_kw=ctx.load_kw[k], q_set_kvar=ctx.load_kw[k] * q_factor,
                                      p_res_kw=res.elem_pq[k][0], q_res_kvar=res.elem_pq[k][1]))
            for k in pv_names:
                elem_rows.append(dict(timestamp=ts, element=k, kind="pv",
                                      p_set_kw=min(ctx.pv_avail_kw[k], ctx.pv_limit_kw[k]),
                                      q_set_kvar=0.0, p_avail_kw=ctx.pv_avail_kw[k],
                                      p_res_kw=res.elem_pq[k][0], q_res_kvar=res.elem_pq[k][1]))
            if (i + 1) % step_every == 0 or i == n - 1:
                el = time.time() - t_start
                print(f"    [{i + 1:>4}/{n}] {ts}  Vmin {vmin:6.1f}  Vmax {vmax:6.1f} V  "
                      f"trafo {ts_kva:6.1f} kVA   ({el:.0f} s)")
    finally:
        if own_session:
            session.close()

    out = dict(summary=pd.DataFrame(summary), nodes=pd.DataFrame(node_rows),
               elements=pd.DataFrame(elem_rows), assignment=assignment)
    for k, df in out.items():
        if k != "assignment":
            df.to_csv(os.path.join(outdir, f"{k}.csv"), index=False)

    _report(out, cfg, interval, outdir, fails, time.time() - t_start)
    if cfg.plot:
        _plot(out["summary"], cfg, outdir)
    return out


def _report(out: dict, cfg: QSTSConfig, interval: float, outdir: str,
            fails: int, secs: float) -> None:
    s = out["summary"]
    ok = s[s["converged"]] if len(s) else s
    print(f"\n=== 4. Results  ({len(ok)}/{len(s)} intervals converged, {secs:.0f} s)")
    if not len(ok):
        print("    nothing converged - see the messages above")
        return
    imax, imin = ok["v_max"].idxmax(), ok["v_min"].idxmin()
    print(f"    highest V    : {ok.loc[imax, 'v_max']:.1f} V at {ok.loc[imax, 'v_max_node']} "
          f"({ok.loc[imax, 'timestamp']})")
    print(f"    lowest V     : {ok.loc[imin, 'v_min']:.1f} V at {ok.loc[imin, 'v_min_node']} "
          f"({ok.loc[imin, 'timestamp']})")
    n_over = int((ok["nodes_over_vmax"] > 0).sum())
    n_under = int((ok["nodes_under_vmin"] > 0).sum())
    print(f"    > {cfg.v_max:g} V    : {n_over} intervals ({n_over * interval / 60:.1f} h) "
          f"| < {cfg.v_min:g} V: {n_under} intervals")
    if "trafo_loading_pct" in ok and ok["trafo_loading_pct"].notna().any():
        print(f"    peak trafo   : {ok['trafo_s_kva'].max():.1f} kVA "
              f"({ok['trafo_loading_pct'].max():.0f} % of rating)")
    if ok["pv_avail_kw"].sum() > 0:
        k = interval / 60.0
        avail = ok["pv_avail_kw"].sum() * k
        limited = ok["pv_limit_kw"].sum() * k
        delivered = ok["pv_out_kw"].sum() * k
        print(f"    PV energy    : available {avail:.1f} kWh | after export limits "
              f"{limited:.1f} kWh | delivered by converters {delivered:.1f} kWh")
        print(f"                   export limits removed {avail - limited:.1f} kWh, "
              f"converter control (volt-watt / Pmax) removed {max(limited - delivered, 0):.1f} kWh")
        print(f"    PV var       : largest |Q| {ok['pv_q_kvar'].abs().max():.2f} kvar total "
              f"(volt-var response)")
    print(f"    files        : {outdir}  (summary.csv, nodes.csv, elements.csv, assignment.csv)")


def _plot(summary: pd.DataFrame, cfg: QSTSConfig, outdir: str) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return
    ok = summary[summary["converged"]]
    if not len(ok):
        return
    fig, ax = plt.subplots(3, 1, figsize=(10, 9), sharex=True)
    ax[0].plot(ok["timestamp"], ok["load_kw"], label="load")
    ax[0].plot(ok["timestamp"], ok["pv_avail_kw"], label="PV available")
    ax[0].plot(ok["timestamp"], ok["pv_out_kw"], label="PV delivered", ls="--")
    ax[0].set_ylabel("kW")
    ax[0].legend()
    ax[1].fill_between(ok["timestamp"], ok["v_min"], ok["v_max"], alpha=0.3)
    ax[1].plot(ok["timestamp"], ok["v_max"], lw=1, label="max")
    ax[1].plot(ok["timestamp"], ok["v_min"], lw=1, label="min")
    ax[1].axhline(cfg.v_max, color="r", ls=":")
    ax[1].axhline(cfg.v_min, color="r", ls=":")
    ax[1].set_ylabel("V (phase-neutral)")
    ax[1].legend()
    ax[2].plot(ok["timestamp"], ok["trafo_s_kva"])
    ax[2].set_ylabel("transformer kVA")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(os.path.join(outdir, "overview.png"), dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Quasi-static time series simulation of a PSS-SINCAL LV network.")
    ap.add_argument("network", help="path to the .sin file")
    ap.add_argument("profiles", help="folder containing the customer CSV profiles")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--start", default=None, help='first interval, e.g. "2023-05-09 00:00"')
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--intervals", type=int, default=None)
    g.add_argument("--hours", type=float, default=None)
    ap.add_argument("--interval-minutes", type=float, default=None)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--load-pf", type=float, default=0.95)
    ap.add_argument("--demand-columns", default="demand")
    ap.add_argument("--solar-column", default="solar")
    ap.add_argument("--power-unit", choices=["kW", "kWh"], default="kW")
    ap.add_argument("--pv-scale", choices=["as_is", "peak"], default="as_is")
    ap.add_argument("--align", choices=["timestamp", "sequence"], default="timestamp")
    ap.add_argument("--backend", choices=["auto", "com", "db"], default="auto")
    ap.add_argument("--virtual-db", action="store_true")
    ap.add_argument("--v-max", type=float, default=253.0)
    ap.add_argument("--v-min", type=float, default=216.0)
    ap.add_argument("--no-reuse", action="store_true")
    ap.add_argument("--ignore-sign-check", action="store_true")
    ap.add_argument("--no-plot", action="store_true")
    a = ap.parse_args()

    interval = a.interval_minutes or 5.0
    n = a.intervals or (int(round(a.hours * 60 / interval)) if a.hours else 288)
    cfg = QSTSConfig(
        network=a.network, profiles_dir=a.profiles, outdir=a.outdir, start=a.start,
        n_intervals=n, interval_minutes=a.interval_minutes, seed=a.seed,
        demand_columns=tuple(c.strip() for c in a.demand_columns.split(",")),
        solar_column=a.solar_column, power_unit=a.power_unit, load_pf=a.load_pf,
        pv_scale=a.pv_scale, align=a.align, backend=a.backend, virtual_db=a.virtual_db,
        v_max=a.v_max, v_min=a.v_min, reuse_profiles=not a.no_reuse,
        ignore_sign_check=a.ignore_sign_check, plot=not a.no_plot)
    try:
        run_qsts(cfg)
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()