"""
opendss_to_sincal.py
====================

Convert an OpenDSS LV network (.dss) into a PSS-SINCAL V22.5 network (.sin)
using the PSS-SINCAL COM automation interface.

Usage
-----
    python opendss_to_sincal.py <Master.dss> [--outdir DIR] [--name NAME] [--no-graphics]
                                [--pv-pct PCT] [--pv-seed N] [--pv-kva-list 3,5,6.6]
                                [--pv-kwp-ratio R] [--pv-output PU] [--pv-all-lv-buses]
                                [--pv-region A|B|C] [--pv-element varshunt|dcinfeeder]
                                [--balanced]
                                [--merge-closed-switches | --no-merge-closed-switches]

Phases
------
By default the OpenDSS phase connections are kept: single-phase loads (bus1=x.2)
become per-phase loads on L1/L2/L3, line matrices are converted to correct
Z1/Z0 (mutual terms and a Kron-reduced neutral), transformers get a vector
group from their winding conns (grounded wye secondary), and the check runs
the unbalanced power flow (LF_USYM).  ``--balanced`` restores the old behaviour.

Zero-sequence data
------------------
Every element that supports a zero-sequence model (Line, TwoWindingTransformer,
Infeeder, Load, ShuntImpedance, VarShuntElement) is given a realistic Z0.
Without this, the unbalanced power flow (LF_USYM) aborts with
"Voltage is going up - please check zero sequence data" because the
zero-sequence network is a short-circuit.

PV option
---------
``--pv-pct 40`` puts a rooftop-PV converter at a random 40 % of the customer
buses (buses carrying a load).

By default PV is modelled as a **VarShuntElement** with a constant P/Q
injection at unity power factor.  This is the only element type in the SINCAL
database that supports a single-phase terminal together with a full
zero-sequence model, so it is what allows LF_USYM to converge when the PV
sits on the same phase as the residential load.

``--pv-element dcinfeeder`` switches to the older DCInfeeder model with the
AS/NZS 4777.2:2020 volt-var and volt-watt controllers.  It has **no**
zero-sequence fields, so it can only be used for balanced three-phase
terminals; the unbalanced solver will diverge otherwise.

Requires: Python 3.10+, pywin32, PSS-SINCAL V22.5 installed (COM version 28).
"""

from __future__ import annotations

import argparse
import math
import os
import random
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from collections import Counter, OrderedDict

try:
    import win32com.client
except ImportError:
    sys.exit("pywin32 is required:  pip install pywin32")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

COM_VER = "28"   # PSS-SINCAL 22.5 -> COM version 28


# ---------------------------------------------------------------------------
# PV (rooftop solar) configuration - AS/NZS 4777.2:2020
# ---------------------------------------------------------------------------
#
# Voltages in PV_REGIONS are volts phase-to-neutral; they are converted to %
# of the node's actual Un/sqrt(3), so a 0.4 kV level gives a 230.94 V base.

PV_REGIONS = {
    "A": dict(vv=(207.0, 220.0, 240.0, 258.0), q_cap=0.44, q_ind=0.60,
              vw1=253.0, vw2=260.0, p_vw2=0.20),
}

# Sign of P for generation in the Converter element (+1 = P>0 feeds the grid).
# Only used by the DCInfeeder path.
PV_P_SIGN = 1.0

# Sign of Plf for a VarShuntElement that represents generation.
# SINCAL uses a load convention for VarShuntElement (positive Plf = consumption),
# so a generating PV system has a NEGATIVE Plf.
PV_SHUNT_P_SIGN = -1.0

# Converter (DCInfeeder) controller, per the PSS-SINCAL Database Description:
#   Flag_DCtyp 7 = photovoltaic;  Flag_LfLimit 5 = limits on P and Q
#   Flag_Pctrl 1 = Volt-watt(Pn), Qctrl_U_P_ID -> characteristic (CharType 14)
#   Flag_Qctrl 3 = Volt-var,      Qctrl_U_Q_ID -> characteristic (CharType 12)
#   Flag_Qctrl 1 = Volt-power factor, numeric Qctrl_U1/U2/cosphi fields
PV_VOLTVAR_MODE = "volt_var"      # or "volt_pf" (cos-phi(V) approximation)

# Characteristic axes: x = voltage in % of nominal (phase-to-neutral),
# y = P/Pn (pu) for volt-watt and Q/Sn (pu) for volt-var.
#
# For a PV converter in SINCAL, P > 0 means injecting active power, and Q > 0
# means injecting reactive power (capacitive, over-excited).  AS/NZS 4777.2
# region A requires the converter to
#     - inject Q (positive) when V is low  (to raise voltage)
#     - absorb Q (negative) when V is high (to lower voltage)
# so the inductive region at the top of the curve must be *negative*.
PV_Q_INDUCTIVE_SIGN = -1.0


@dataclass
class PVConfig:
    pct: float = 0.0                 # % of candidate buses that get PV
    seed: int | None = None
    sizes_kva: tuple = (5.0,)        # inverter ratings drawn at random (kVA)
    kwp_ratio: float = 1.0           # DC array kWp / inverter kVA
    output_pu: float = 1.0           # fP: 1.0 = full available output
    all_lv_buses: bool = False       # False: only buses that carry a load
    region: str = "A"
    use_varshunt: bool = True        # True: single-phase PV as VarShuntElement


# ---------------------------------------------------------------------------
# Generic OpenDSS token parser
# ---------------------------------------------------------------------------

def parse_named_params(text: str) -> "OrderedDict[str, list[str]]":
    """
    Parse ``key=value key2=value2 ...`` into an ordered dict of lists.
    Supports brackets ``[...]`` that can contain whitespace, ``|`` row
    separators and both single/double quotes for strings.
    Repeated keys (e.g. ``bus=`` inside transformer windings) become lists.
    """
    params: "OrderedDict[str, list[str]]" = OrderedDict()

    tokens: list[str] = []
    cur = ""
    depth = 0
    for ch in text:
        if ch in "[({":
            depth += 1
            cur += ch
        elif ch in "])}":
            depth -= 1
            cur += ch
        elif ch.isspace() and depth == 0:
            if cur:
                tokens.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        tokens.append(cur)

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if "=" not in tok:
            i += 1
            continue
        key, val = tok.split("=", 1)
        key = key.strip().lower()
        val = val.strip()

        if val and val[0] in "[({'\"":  # opening bracket -> keep joining
            opener = val[0]
            closer = {"[": "]", "(": ")", "{": "}", '"': '"', "'": "'"}[opener]
            while not val.rstrip().endswith(closer):
                i += 1
                if i >= len(tokens):
                    break
                val += " " + tokens[i]

        params.setdefault(key, []).append(val)
        i += 1
    return params


def parse_matrix(s: str | None) -> list[list[float]] | None:
    """Parse ``[v1 | v2 v3 | v4 v5 v6]`` into lower-triangular 2D list."""
    if not s:
        return None
    s = s.strip()
    for br in "[](){}":
        s = s.replace(br, " ")
    rows = [r.strip() for r in s.split("|")]
    mat: list[list[float]] = []
    for i, row in enumerate(rows):
        vals = []
        for v in row.split():
            try:
                vals.append(float(v))
            except ValueError:
                pass
        while len(vals) < i + 1:
            vals.append(0.0)
        mat.append(vals)
    return mat


def _sym(m):
    n = len(m)
    return [[m[i][j] if j < len(m[i]) else m[j][i] for j in range(n)]
            for i in range(n)]


def _kron_reduce_last(Z):
    n = len(Z) - 1
    return [[Z[i][j] - Z[i][n] * Z[n][j] / Z[n][n] for j in range(n)]
            for i in range(n)]


def _seq_from_phase_matrix(M):
    n = len(M)
    diag = [M[i][i] for i in range(n)]
    zs = sum(diag) / n
    asym = max(abs(d - zs) for d in diag) / abs(zs) if zs else 0.0
    if n >= 3:
        offs = [M[i][j] for i in range(3) for j in range(i + 1, 3)]
        zm = sum(offs) / len(offs)
        return zs - zm, zs + 2 * zm, asym
    return zs, zs, asym


def matrix_to_seq(Rmat, Xmat, Cmat, nphases: int):
    if not Rmat:
        return None
    n = len(Rmat)
    R = _sym(Rmat)
    X = _sym(Xmat) if Xmat else [[0.0] * n for _ in range(n)]
    C = _sym(Cmat) if Cmat else [[0.0] * n for _ in range(n)]

    if n >= 4:
        Z = [[complex(R[i][j], X[i][j]) for j in range(n)] for i in range(n)]
        while len(Z) > 3:
            Z = _kron_reduce_last(Z)
        R = [[Z[i][j].real for j in range(3)] for i in range(3)]
        X = [[Z[i][j].imag for j in range(3)] for i in range(3)]
        C = [[C[i][j] for j in range(3)] for i in range(3)]

    r1, r0, a_r = _seq_from_phase_matrix(R)
    x1, x0, a_x = _seq_from_phase_matrix(X)
    c1, c0, _ = _seq_from_phase_matrix(C)
    return {"R1": r1, "X1": x1, "C1": c1, "R0": r0, "X0": x0, "C0": c0,
            "asym": max(a_r, a_x)}


PHASE_CODE = {frozenset({1}): 1, frozenset({2}): 2, frozenset({3}): 3,
              frozenset({1, 2}): 4, frozenset({2, 3}): 5, frozenset({1, 3}): 6,
              frozenset({1, 2, 3}): 7}


def load_connection(ld: dict) -> tuple[str, tuple[int, ...]]:
    raw = ld.get("bus1", [""])[0]
    nodes: list[int] = []
    for t in raw.split(".")[1:]:
        try:
            k = int(t)
        except ValueError:
            continue
        if k in (1, 2, 3) and k not in nodes:
            nodes.append(k)
    try:
        nph = int(float(ld.get("phases", ["3"])[0]))
    except ValueError:
        nph = 3
    delta = str(ld.get("conn", ["wye"])[0]).lower() in ("delta", "d", "ll")
    if not nodes:
        nodes = [1, 2, 3][:max(1, min(nph, 3))]
    if len(nodes) >= 3:
        return "3ph", (1, 2, 3)
    if len(nodes) == 1:
        return "1ph", (nodes[0],)
    if nph == 1 or delta:
        return "ll", tuple(nodes[:2])
    return "2ph", tuple(nodes[:2])


def is_delta(conn: str | None) -> bool:
    return str(conn or "wye").lower() in ("delta", "d", "ll")


def vecgrp_from_conns(c1: str | None, c2: str | None) -> tuple[int, str]:
    d1, d2 = is_delta(c1), is_delta(c2)
    return {(False, False): (5, "YNyn0"), (True, False): (10, "Dyn1"),
            (True, True): (1, "Dd0"), (False, True): (14, "YNd1")}[(d1, d2)]


def phase_summary(loads: list[dict]) -> None:
    tot = {1: [0.0, 0], 2: [0.0, 0], 3: [0.0, 0]}
    for ld in loads:
        try:
            kw = float(ld.get("kw", ["1.0"])[0])
        except ValueError:
            kw = 1.0
        kind, nodes = load_connection(ld)
        if kind == "ll":
            share = {nodes[0]: kw / 2, nodes[1]: kw / 2}
        elif kind == "2ph":
            share = {nodes[0]: kw / 2, nodes[1]: kw / 2}
        elif kind == "1ph":
            share = {nodes[0]: kw}
        else:
            share = {1: kw / 3, 2: kw / 3, 3: kw / 3}
        for ph, v in share.items():
            tot[ph][0] += v
        for ph in (nodes if kind != "3ph" else (1, 2, 3)):
            tot[ph][1] += 1
    mean = sum(v[0] for v in tot.values()) / 3 or 1.0
    print("    Phase allocation of load (L-L loads split equally between their phases):")
    for ph in (1, 2, 3):
        print(f"      L{ph}: {tot[ph][0]:9.2f} kW  ({tot[ph][1]} connections)")
    spread = (max(v[0] for v in tot.values()) - min(v[0] for v in tot.values())) / mean
    print(f"      kW spread (max-min)/mean: {spread*100:.1f} %")


def strip_bus_node(bus: str) -> str:
    return bus.split(".")[0] if bus else bus


# ---------------------------------------------------------------------------
# Auto-layout: BFS radial layout in metres
# ---------------------------------------------------------------------------

def compute_layout(buses: list[str],
                   adjacency: dict[str, set[str]],
                   source: str | None) -> dict[str, tuple[float, float]]:
    depth: dict[str, int] = {}
    if source and source in adjacency:
        depth[source] = 0
        queue = [source]
        while queue:
            cur = queue.pop(0)
            for nb in adjacency.get(cur, ()):
                if nb not in depth:
                    depth[nb] = depth[cur] + 1
                    queue.append(nb)

    next_d = max(depth.values(), default=0) + 1
    for b in buses:
        if b not in depth:
            depth[b] = next_d
            next_d += 1

    by_depth: dict[int, list[str]] = {}
    for b, d in depth.items():
        by_depth.setdefault(d, []).append(b)

    dx = 0.15
    dy = 0.08
    pos: dict[str, tuple[float, float]] = {}
    for d in sorted(by_depth):
        row = sorted(by_depth[d])
        n = len(row)
        for i, b in enumerate(row):
            x = 0.05 + d * dx
            y = 0.50 + (i - (n - 1) / 2.0) * dy
            pos[b] = (x, y)
    return pos


# ---------------------------------------------------------------------------
# OpenDSS parser
# ---------------------------------------------------------------------------

class OpenDSSParser:
    def __init__(self, master_file: str):
        self.master_file = os.path.abspath(master_file)
        self.base_dir = os.path.dirname(self.master_file)

        self.circuit_name: str | None = None
        self.source_bus: str | None = None
        self.base_kv: float = 11.0
        self.base_freq: float = 50.0

        self.linecodes: dict[str, dict] = {}
        self.lines: list[dict] = []
        self.switches: list[dict] = []
        self.transformers: list[dict] = []
        self.loads: list[dict] = []
        self.reactors: list[dict] = []
        self.vsources: list[dict] = []
        self.batchedits: list[tuple[str, str]] = []

    def parse(self) -> "OpenDSSParser":
        self._parse_file(self.master_file)
        self._apply_batchedits()
        return self

    def _resolve(self, name: str) -> str | None:
        if os.path.isabs(name) and os.path.exists(name):
            return name
        candidates = [name]
        base, ext = os.path.splitext(name)
        if ext.lower() == ".dss":
            candidates += [base + ".txt", base + ".DSS", base + ".TXT"]
        elif ext.lower() == ".txt":
            candidates += [base + ".dss", base + ".DSS"]
        else:
            candidates += [name + ".dss", name + ".txt"]
        for c in candidates:
            p = os.path.join(self.base_dir, c)
            if os.path.exists(p):
                return p
        return None

    def _parse_file(self, filepath: str) -> None:
        if not os.path.isabs(filepath):
            filepath = os.path.join(self.base_dir, filepath)
        if not os.path.exists(filepath):
            print(f"  [warn] not found: {filepath}")
            return

        print(f"  parsing {os.path.relpath(filepath, self.base_dir)}")
        with open(filepath, "r", encoding="utf-8", errors="ignore") as fh:
            raw = fh.readlines()

        commands: list[str] = []
        cur = ""
        for line in raw:
            s = line.strip()
            if not s or s.startswith("//"):
                continue
            if "!" in s:
                s = s[: s.index("!")].strip()
            if not s:
                continue
            if s.startswith("~"):
                cur += " " + s[1:].strip()
            elif re.match(r"^more\b", s, re.IGNORECASE):
                cur += " " + re.sub(r"^more\s+", "", s, flags=re.IGNORECASE)
            else:
                if cur:
                    commands.append(cur)
                cur = s
        if cur:
            commands.append(cur)

        for cmd in commands:
            self._process_command(cmd)

    def _process_command(self, cmd: str) -> None:
        low = cmd.lower()

        m = re.match(r'(?:redirect|compile)\s+["\']?([^"\']+?)["\']?\s*$', low)
        if m:
            target = self._resolve(m.group(1).strip())
            if target:
                self._parse_file(target)
            else:
                print(f"  [warn] cannot resolve redirect '{m.group(1)}'")
            return

        m = re.match(r'set\s+(?:default)?basefrequency\s*=\s*([\d.]+)', low)
        if m:
            self.base_freq = float(m.group(1))
            return

        m = re.match(r'new\s+circuit\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            self.circuit_name = m.group(1)
            p = parse_named_params(m.group(2))
            self.base_kv = float(p.get("basekv", ["11.0"])[0])
            if "bus1" in p:
                self.source_bus = strip_bus_node(p["bus1"][0])
            return

        m = re.match(r'new\s+linecode\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            self.linecodes[m.group(1).lower()] = parse_named_params(m.group(2))
            return

        m = re.match(r'new\s+line\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            name = m.group(1)
            p = parse_named_params(m.group(2))
            p["_name"] = name
            is_switch = str(p.get("switch", ["no"])[0]).lower() in ("y", "yes", "true")
            (self.switches if is_switch else self.lines).append(p)
            return

        m = re.match(r'new\s+transformer\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            name = m.group(1)
            p = parse_named_params(m.group(2))
            p["_name"] = name
            p["_windings"] = self._parse_windings(m.group(2))
            self.transformers.append(p)
            return

        m = re.match(r'new\s+reactor\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            p = parse_named_params(m.group(2))
            p["_name"] = m.group(1)
            self.reactors.append(p)
            return

        m = re.match(r'new\s+load\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            p = parse_named_params(m.group(2))
            p["_name"] = m.group(1)
            self.loads.append(p)
            return

        m = re.match(r'new\s+vsource\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            p = parse_named_params(m.group(2))
            p["_name"] = m.group(1)
            self.vsources.append(p)
            return

        m = re.match(r'batchedit\s+(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            self.batchedits.append((m.group(1), m.group(2)))
            return

    @staticmethod
    def _parse_windings(text: str) -> list[dict]:
        parts = re.split(r'\bwdg\s*=\s*', text, flags=re.IGNORECASE)
        out: list[dict] = []
        for part in parts[1:]:
            p = parse_named_params(part)
            w: dict[str, str] = {}
            for k in ("bus", "conn", "kv", "kva", "tap", "%r"):
                if k in p:
                    w[k] = p[k][0]
            out.append(w)
        return out

    def _apply_batchedits(self) -> None:
        for pattern, edits in self.batchedits:
            cls, _ = pattern.split(".", 1) if "." in pattern else (pattern, "")
            e = parse_named_params(edits)
            if cls.lower() == "load":
                for ld in self.loads:
                    for k, v in e.items():
                        ld[k] = v


# ---------------------------------------------------------------------------
# PSS-SINCAL builder
# ---------------------------------------------------------------------------

class SincalBuilder:
    def __init__(self, out_dir: str, name: str, freq: float,
                 graphics: bool = True):
        self.out_dir = os.path.abspath(out_dir)
        self.name = name
        self.freq = freq
        self.graphics = graphics

        self.sin_file = os.path.join(self.out_dir, name + ".sin")
        self.data_dir = self.sin_file[:-4] + "_files"
        self.db_file = os.path.join(self.data_dir, "database.db")

        if os.path.exists(self.sin_file):
            os.remove(self.sin_file)
        if os.path.isdir(self.data_dir):
            time.sleep(0.5)
            shutil.rmtree(self.data_dir)
        os.makedirs(self.data_dir, exist_ok=True)

        self.conn = (f"TYP=NET;MODE=SQLITE;FILE={self.db_file};"
                     f"USR=Admin;PWD=;SINFILE={self.sin_file};")

        self.voltage_levels: dict[str, int] = {}
        self.nodes: dict[str, int] = {}
        self.db = None
        self.sim = None
        self.sim_srv = None

        self.positions: dict[str, tuple[float, float]] = {}
        self._elem_count: dict[str, int] = {}

        self._columns: dict[str, dict[str, str]] = {}
        self.unresolved: set[str] = set()
        self.vp_char_id: int | None = None
        self.unphased: list[str] = []
        self.ctrl_modes_missing = False
        self.pending_phases: list[tuple[str, int]] = []
        self.phase_cols: list[tuple[str, str]] = []

    # ---- lifecycle -------------------------------------------------------
    def create_database(self) -> None:
        DBMgr = win32com.client.Dispatch(f"Sincal.DatabaseManager.{COM_VER}")
        DBMgr.SetConnection(self.conn)
        self._setparam(DBMgr, "LANGUAGE", "ENG")
        rc = DBMgr.CreateDB(1)          # 1 = electrical
        if rc != 0:
            raise RuntimeError(f"CreateDB returned {rc}")
        DBMgr = None
        time.sleep(0.5)

        # Disable every alternative load-data source in CalcParameter.
        # A freshly created database may have any of these set to 1; when
        # the backing table is empty the LF then sets every load's effective
        # P to 0 and produces a "converged" solve that is really a
        # zero-injection network.  Column names are discovered from the
        # schema so this works across SINCAL versions.
        try:
            con = sqlite3.connect(self.db_file)
            try:
                cols = {r[1].lower(): r[1] for r in
                        con.execute('PRAGMA table_info("CalcParameter")')}
                updates = []
                for field, value in (("flag_usela", 0),
                                     ("flag_useopser", 0),
                                     ("flag_usetimeser", 0),
                                     ("flag_useincser", 0)):
                    if field in cols:
                        updates.append(f'"{cols[field]}"={value}')
                if updates:
                    con.execute('UPDATE "CalcParameter" SET ' +
                                ', '.join(updates))
                    con.commit()
                    print(f"    CalcParameter: disabled alternative load "
                          f"data sources ({', '.join(updates)})")
            finally:
                con.close()
        except sqlite3.Error as exc:
            print(f"  [warn] could not update CalcParameter: {exc}")

    def open_database(self) -> None:
        self.sim_srv = win32com.client.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
        self.sim = self.sim_srv.GetSimulation()
        self.sim.Database(self.conn)
        self.db = self.sim.DB_EL()
        self._setparam(self.db, "DBSYS_MODE", 8)     # SQLite
        self._setparam(self.db, "DBSYS_DATAFILE", self.db_file)
        self._setparam(self.db, "DBSYS_USER", "Admin")
        self._setparam(self.db, "DBSYS_PASSWORD", "")
        if self.db.OpenEx() != 0:
            raise RuntimeError("OpenEx failed")

        if self.graphics:
            self._setparam(self.db, "UI_DEFAULTS", True)
            self._setparam(self.db, "COPY_GRAPHICDATA", True)
            try:
                self._setparam(self.db, "GraphicArea", 1)
            except Exception:
                pass

    def close(self) -> None:
        try:
            if self.db:
                self.db.Close()
        except Exception:
            pass
        self.db = None
        self.sim = None
        self.sim_srv = None
        time.sleep(0.3)

    @staticmethod
    def _setparam(obj, name, value):
        try:
            obj.SetParameter(name, value)
        except AttributeError:
            obj.Parameter(name, value)

    def _symbol_pos(self, bus: str, side: int = +1) -> tuple[float, float]:
        x, y = self.positions.get(bus, (0.0, 0.0))
        n = self._elem_count.get(bus, 0)
        self._elem_count[bus] = n + 1
        return (x + 0.025 * n, y + side * 0.040)

    def ensure_voltage_level(self, name: str, kv: float) -> int:
        if name in self.voltage_levels:
            return self.voltage_levels[name]
        vl = self.db.CreateObject("VoltageLevel")
        vl.SetValue("Name", name)
        vl.SetValue("ShortName", name)
        vl.SetValue("Un", float(kv))
        vl.SetValue("Uop", float(kv))
        vl.SetValue("f", float(self.freq))
        vl.SetValue("Flag_Volt", 1)
        vl.Update()
        rid = vl.RowId
        self.voltage_levels[name] = rid
        print(f"    + VoltageLevel {name:<10} ({kv:>8.3f} kV)")
        return rid

    def ensure_node(self, name: str, vl_name: str) -> int:
        if name in self.nodes:
            return self.nodes[name]
        self._setparam(self.db, "NetworkLevel", vl_name)
        n = self.db.CreateNode(name)
        n.SetValue("Node.Flag_Typ", 4)
        n.Update()
        if self.graphics and name in self.positions:
            x, y = self.positions[name]
            n.CreateGraphic(x, y, x + 0.02, y)
        self.nodes[name] = n.RowId
        return n.RowId

    def create_infeeder(self, name, node, kv, v_pu=1.0, angle_deg=0.0,
                        sk_mva=100.0, r_x=0.1):
        n = self.db.GetCommonObject("Node", node)
        inf = self.db.CreateElement("Infeeder", name, n)
        inf.SetValue("Flag_Input", 1)
        inf.SetValue("Flag_State", 1)
        Z = (kv ** 2) / sk_mva if sk_mva > 0 else 1e-6
        X = Z / math.sqrt(1 + r_x ** 2)
        R = r_x * X
        inf.SetValue("Flag_Lf", 3)
        inf.SetValue("Flag_Typ", 1)
        inf.SetValue("R", R)
        inf.SetValue("X", X)
        inf.SetValue("R_X", r_x)
        inf.SetValue("Sk2", sk_mva)
        inf.SetValue("u", v_pu * 100.0)
        inf.SetValue("delta", angle_deg)
        inf.SetValue("Flag_Z0", 1)
        inf.SetValue("Flag_Z0_Input", 1)
        inf.SetValue("Z0_Z1", 1.0)
        inf.SetValue("R0_X0", r_x)
        inf.Update()
        if self.graphics:
            sx, sy = self._symbol_pos(node, side=+1)
            inf.CreateGraphic(sx, sy)
        print(f"    + Infeeder  {name:<10} @ {node}  ({kv} kV)")
        return inf

    def create_line(self, name, n1_name, n2_name, length_km, seq):
        n1 = self.db.GetCommonObject("Node", n1_name)
        n2 = self.db.GetCommonObject("Node", n2_name)
        ln = self.db.CreateElement("Line", name, n1, n2)
        ln.SetValue("Flag_Input", 1)
        ln.SetValue("Flag_State", 1)
        ln.SetValue("Flag_LineTyp", 1)
        ln.SetValue("Len", max(float(length_km), 1e-3))
        ln.SetValue("ParSys", 1.0)
        ln.SetValue("R", float(seq["R1"]))
        ln.SetValue("X", float(seq["X1"]))
        ln.SetValue("C", float(seq["C1"]))
        ln.SetValue("FrqNenn", float(self.freq))
        ln.SetValue("Flag_Lf", 1)
        r0 = float(seq["R0"])
        x0 = float(seq["X0"])
        if abs(r0) < 1e-9 and abs(x0) < 1e-9:
            r0, x0 = 1e-5, 1e-5
        ln.SetValue("Flag_Z0_Input", 2)
        ln.SetValue("R0", r0)
        ln.SetValue("X0", x0)
        try:
            ln.SetValue("C0", float(seq.get("C0", 0.0)))
        except Exception:
            pass
        ln.Update()
        if self.graphics:
            ln.CreateGraphic()
        print(f"    + Line      {name:<12} {n1_name} -> {n2_name}  ({length_km:.6f} km)")
        return ln

    def create_switch_line(self, name, n1_name, n2_name):
        n1 = self.db.GetCommonObject("Node", n1_name)
        n2 = self.db.GetCommonObject("Node", n2_name)
        ln = self.db.CreateElement("Line", name, n1, n2)
        ln.SetValue("Flag_Input", 1)
        ln.SetValue("Flag_State", 1)
        ln.SetValue("Flag_LineTyp", 3)
        ln.SetValue("Len", 0.001)
        ln.SetValue("ParSys", 1.0)
        ln.SetValue("R", 1e-5)
        ln.SetValue("X", 1e-5)
        ln.SetValue("C", 0.0)
        ln.SetValue("FrqNenn", float(self.freq))
        ln.SetValue("Flag_Lf", 1)
        ln.SetValue("Flag_Z0_Input", 2)
        ln.SetValue("R0", 1e-5)
        ln.SetValue("X0", 1e-5)
        ln.Update()
        if self.graphics:
            ln.CreateGraphic()
        print(f"    + Switch    {name:<12} {n1_name} -> {n2_name}")
        return ln

    def create_transformer(self, name, n1_name, n2_name,
                           kv1, kv2, sn_mva, uk_pct=6.0, ur_pct=0.5,
                           vecgrp=1, vg_label=""):
        n1 = self.db.GetCommonObject("Node", n1_name)
        n2 = self.db.GetCommonObject("Node", n2_name)
        t = self.db.CreateElement("TwoWindingTransformer", name, n1, n2)
        t.SetValue("Flag_Input", 1)
        t.SetValue("Flag_State", 1)
        t.SetValue("Sn", float(sn_mva))
        t.SetValue("Un1", float(kv1))
        t.SetValue("Un2", float(kv2))
        t.SetValue("uk", float(uk_pct))
        t.SetValue("ur", float(ur_pct))
        t.SetValue("Flag_Ur", 1)
        t.SetValue("VecGrp", int(vecgrp))
        t.SetValue("Flag_Lf", 1)
        t.SetValue("Flag_ConNode", 1)
        t.SetValue("Flag_Z0_Input", 1)
        t.SetValue("Z0_Z1", 1.0)
        t.SetValue("R0_X0", 0.1)
        t.Update()
        if self.graphics:
            t.CreateGraphic()
        print(f"    + Trafo     {name:<12} {n1_name}({kv1}kV) -> {n2_name}({kv2}kV)  {vg_label}")
        return t

    def create_load(self, name, node, p_mw, q_mvar, conn=None):
        n = self.db.GetCommonObject("Node", node)
        ld = self.db.CreateElement("Load", name, n)
        ld.SetValue("Flag_Input", 1)
        ld.SetValue("Flag_State", 1)
        kind, ph = conn if conn else ("3ph", (1, 2, 3))
        ld.SetValue("Flag_Load", 1)
        ld.SetValue("Flag_Typified", 3)
        ld.SetValue("Flag_LoadType", 2)
        ld.SetValue("Flag_Z0", 1)
        ld.SetValue("Flag_Z0_Input", 1)
        ld.SetValue("Z0_Z1", 1.0)
        ld.SetValue("R0_X0", 1.0)
        ld.SetValue("P", float(p_mw))
        ld.SetValue("Q", float(q_mvar))
        ld.SetValue("fP", 1.0)
        ld.SetValue("fQ", 1.0)

        phase_set = kind in ("1ph", "ll")
        if phase_set:
            self.queue_phase(name, ph)
        if kind == "3ph":
            ld.SetValue("Flag_Lf", 1)
            where = "L123"
        elif kind == "1ph":
            ld.SetValue("Flag_Lf", 1)
            where = "L" + "".join(str(k) for k in sorted(ph))
        elif kind == "2ph":
            ld.SetValue("Flag_Lf", 13)
            for k in (1, 2, 3):
                share = (1.0 / len(ph)) if k in ph else 0.0
                ld.SetValue(f"P{k}", float(p_mw) * share)
                ld.SetValue(f"Q{k}", float(q_mvar) * share)
            where = "L" + "".join(str(k) for k in ph)
        else:
            ld.SetValue("Flag_Lf", 14)
            key = {frozenset({1, 2}): "12", frozenset({2, 3}): "23",
                   frozenset({1, 3}): "31"}[frozenset(ph)]
            for kk in ("12", "23", "31"):
                on = 1.0 if kk == key else 0.0
                ld.SetValue(f"P{kk}", float(p_mw) * on)
                ld.SetValue(f"Q{kk}", float(q_mvar) * on)
            where = "L" + key
        ld.Update()
        if self.graphics:
            sx, sy = self._symbol_pos(node, side=-1)
            ld.CreateGraphic(sx, sy)
        print(f"    + Load      {name:<16} @ {node}  {where:<5} "
              f"P={p_mw:.4f} MW  Q={q_mvar:.4f} Mvar")
        return ld

    def create_shunt_impedance(self, name, node, r_ohm, x_ohm, kv):
        n = self.db.GetCommonObject("Node", node)
        sh = self.db.CreateElement("ShuntImpedance", name, n)
        sh.SetValue("Flag_Input", 1)
        sh.SetValue("Flag_State", 1)
        sh.SetValue("Un", float(kv))
        sh.SetValue("R", float(r_ohm))
        sh.SetValue("X", float(x_ohm))
        sh.SetValue("Flag_Lf", 1)
        sh.SetValue("Flag_Z0", 1)
        sh.SetValue("Flag_Z0_Input", 1)
        sh.SetValue("Z0_Z1", 1.0)
        sh.SetValue("R0_X0", 1.0)
        sh.Update()
        if self.graphics:
            sx, sy = self._symbol_pos(node, side=-1)
            sh.CreateGraphic(sx, sy)
        print(f"    + ShuntImp  {name:<20} @ {node}  R={r_ohm} X={x_ohm}")
        return sh

    # ---- converter (PV) support -----------------------------------------
    def read_schema(self, tables=None) -> None:
        try:
            con = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
            names = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")]
            for t in names:
                rows = con.execute(f'PRAGMA table_info("{t}")').fetchall()
                self._columns[t] = {r[1].lower(): r[1] for r in rows}
            con.close()
        except Exception as exc:
            print(f"  [warn] could not read database schema: {exc}")

    def queue_phase(self, name: str, phases) -> None:
        code = PHASE_CODE[frozenset(phases)]
        if code != 7:
            self.pending_phases.append((name, code))

    def apply_pending_phases_sql(self) -> None:
        if not self.pending_phases:
            return
        tcols = self._columns.get("Terminal", {})
        ecols = self._columns.get("Element", {})
        pcol, tlink = tcols.get("flag_terminal"), tcols.get("element_id")
        eid, ename = ecols.get("element_id"), ecols.get("name")
        if not (pcol and tlink and eid and ename):
            raise RuntimeError(
                "cannot write terminal phases: need Terminal(Flag_Terminal, "
                f"Element_ID) and Element(Element_ID, Name); found Terminal="
                f"{sorted(tcols.values())}, Element={sorted(ecols.values())}")
        no_col = tcols.get("terminalno")
        failed = []
        con = sqlite3.connect(self.db_file)
        try:
            for name, code in self.pending_phases:
                sql = (f'UPDATE "Terminal" SET "{pcol}"=? WHERE "{tlink}" IN '
                       f'(SELECT "{eid}" FROM "Element" WHERE "{ename}"=?)')
                if no_col:
                    sql += f' AND "{no_col}"=1'
                if con.execute(sql, (code, name)).rowcount < 1:
                    failed.append(name)
            con.commit()
        finally:
            con.close()
        n = len(self.pending_phases)
        print(f"\n    Terminal phase written to the database for {n - len(failed)} "
              f"of {n} single-phase elements (Terminal.{pcol})")
        self.pending_phases = []
        if failed:
            raise RuntimeError("terminal phase NOT set for: " + ", ".join(failed))

    def report_phase_status(self, use_varshunt: bool = False) -> None:
        names = {1: "L1", 2: "L2", 3: "L3", 4: "L12", 5: "L23", 6: "L31",
                 7: "L123", 8: "N"}
        tables = [("Load", "Loads     ")]
        if use_varshunt:
            tables.append(("VarShuntElement", "PV shunts "))
        else:
            tables.append(("DCInfeeder", "Converters"))
        try:
            con = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
            found = {}
            for table, label in tables:
                found[label] = con.execute(
                    f'SELECT t."Flag_Terminal", COUNT(*) FROM "{table}" x '
                    'JOIN "Terminal" t ON t."Element_ID"=x."Element_ID" '
                    'WHERE t."TerminalNo"=1 GROUP BY t."Flag_Terminal" ORDER BY 1').fetchall()
            con.close()
        except Exception as exc:
            print(f"  [warn] could not read back terminal phases: {exc}")
            return
        for label, rows in found.items():
            part = ", ".join(f"{names.get(c, c)}={n}" for c, n in rows)
            if part:
                print(f"    {label} by terminal phase in database: {part}")

    def report_converters_sql(self, use_varshunt: bool = False) -> None:
        try:
            con = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
            if use_varshunt:
                rows = con.execute(
                    'SELECT e."Name", d."Plf", d."Qlf", d."Ulf", '
                    'd."Flag_LF", d."Flag_LoadType", d."Flag_Z0_Input" '
                    'FROM "VarShuntElement" d '
                    'JOIN "Element" e ON e."Element_ID"=d."Element_ID" '
                    'ORDER BY 1').fetchall()
            else:
                rows = con.execute(
                    'SELECT e."Name", d."Sn_Inverter", d."Ur_Inverter", d."Flag_DCtyp", '
                    'd."Flag_LfLimit", d."Pmax", d."Flag_Pctrl", d."Flag_Qctrl", '
                    'd."Qctrl_U_P_ID", d."Qctrl_U_Q_ID" FROM "DCInfeeder" d '
                    'JOIN "Element" e ON e."Element_ID"=d."Element_ID" ORDER BY 1').fetchall()
            con.close()
        except Exception as exc:
            print(f"  [warn] could not read converters back: {exc}")
            return

        if use_varshunt:
            print(f"\n    PV shunts stored in database: {len(rows)}")
            print("      name              Plf[MW]  Qlf[Mvar] Ulf[kV] mode loadb Z0in")
            for r in rows[:4]:
                print(f"      {r[0]:<16} {r[1]:>7.4f} {r[2]:>9.4f} "
                      f"{r[3]:>7.3f} {r[4]:>4} {r[5]:>5} {r[6]:>4}")
            if len(rows) > 4:
                print(f"      ... {len(rows) - 4} more")
            bad = [r[0] for r in rows if abs(float(r[1] or 0.0)) < 1e-9]
            if bad:
                print("  [WARN] PV shunts with Plf = 0: " + ", ".join(bad))
        else:
            print(f"\n    Converters stored in database: {len(rows)}")
            print("      name              Sn_Inv[MVA] Ur[kV] type limit Pmax[MW] Pctrl Qctrl  V-P  V-Q")
            for r in rows[:4]:
                print(f"      {r[0]:<16} {r[1]:>10.4f} {r[2]:>6.3f} {r[3]:>4} {r[4]:>5} "
                      f"{r[5]:>8.4f} {r[6]:>5} {r[7]:>5} {r[8]:>4} {r[9]:>4}")
            if len(rows) > 4:
                print(f"      ... {len(rows) - 4} more")
            bad = [r[0] for r in rows if not r[1] or r[3] != 7]
            if bad:
                print("  [WARN] converters with Sn_Inverter = 0 or plant type != PV: "
                      + ", ".join(bad))

    def create_characteristic(self, name: str, char_type: int, points):
        try:
            ch = self.db.CreateObject("Characteristic")
            ch.SetValue("Name", name)
            ch.SetValue("CharType", int(char_type))
            ch.Update()
            cid = ch.RowId
            for n, (x, y) in enumerate(points, 1):
                v = self.db.CreateObject("CharacteristicValue")
                v.SetValue("Char_ID", cid)
                v.SetValue("n", float(n))
                v.SetValue("x", float(x))
                v.SetValue("y", float(y))
                v.Update()
            print(f"    + Characteristic '{name}' (type {char_type}, "
                  f"{len(points)} points, id={cid})")
            return cid
        except Exception as exc:
            print(f"  [warn] characteristic '{name}' creation failed: {exc}")
            return None

    def _create_pv_varshunt(self, name, node, n, p_rated, sn, cfg, phases, kv_ll):
        pv = self.db.CreateElement("VarShuntElement", name, n)
        pv.SetValue("Flag_Input", 1)
        pv.SetValue("Flag_State", 1)
        pv.SetValue("Flag_LF", 1)
        pv.SetValue("Flag_LoadType", 2)
        want = PV_SHUNT_P_SIGN * p_rated * float(cfg.output_pu)
        pv.SetValue("Plf", want)
        pv.SetValue("Qlf", 0.0)
        pv.SetValue("Ulf", float(kv_ll))
        pv.SetValue("f_p_k", 1.0)
        pv.SetValue("f_q_k", 1.0)
        pv.SetValue("Flag_Z0_Input", 1)
        pv.SetValue("Z0_Z1", 1.0)
        pv.SetValue("R0_X0", 1.0)
        pv.SetValue("Pneg", 0.0)
        pv.SetValue("Qneg", 0.0)
        pv.Update()

        try:
            got = float(pv.GetValue("Plf"))
        except Exception:
            got = None
        if got is None or abs(got - want) > 1e-6:
            raise RuntimeError(
                f"PV shunt '{name}': Plf did not stick "
                f"(requested {want:+.6f} MW, read back {got}). "
                f"Check the actual column name with "
                f"PRAGMA table_info(\"VarShuntElement\").")

        self.queue_phase(name, phases)

        if self.graphics:
            sx, sy = self._symbol_pos(node, side=+1)
            pv.CreateGraphic(sx, sy)

        ph_txt = "L" + "".join(str(k) for k in sorted(phases))
        print(f"    + PV (VarShunt) {name:<16} @ {node}  {ph_txt:<4} "
              f"Sn={sn*1000:.1f} kVA  Plf={want*1000:+.2f} kW")
        return pv

    def create_pv_converter(self, name, node, kva, kwp, cfg: PVConfig,
                            phases=(1, 2, 3), v_base=230.94, kv_ll=0.4,
                            vp_id=None, vq_id=None):
        std = PV_REGIONS[cfg.region]
        sn = kva / 1000.0
        p_rated = min(kwp, kva) / 1000.0
        n = self.db.GetCommonObject("Node", node)

        if cfg.use_varshunt:
            return self._create_pv_varshunt(name, node, n, p_rated, sn,
                                            cfg, phases, kv_ll)

        vv = std["vv"]
        pct = lambda v: 100.0 * v / v_base

        cv = self.db.CreateElement("DCInfeeder", name, n)
        cv.SetValue("Flag_Input", 1)
        cv.SetValue("Flag_State", 1)
        cv.SetValue("Flag_DCtyp", 7)
        cv.SetValue("Flag_Connect", 1)
        cv.SetValue("IslandOp", 0)
        cv.SetValue("Sn_Inverter", sn)
        cv.SetValue("Ur_Inverter", float(kv_ll))
        cv.SetValue("Umin_Inverter", 80.0)
        cv.SetValue("Umax_Inverter", 120.0)
        cv.SetValue("Flag_Lf", 1)
        cv.SetValue("P", PV_P_SIGN * p_rated)
        cv.SetValue("Q", 0.0)
        cv.SetValue("fP", float(cfg.output_pu))
        cv.SetValue("fQ", 1.0)
        cv.SetValue("Flag_LfLimit", 5)
        cv.SetValue("Flag_ChkType", 1)
        cv.SetValue("Flag_LimitType", 1)
        cv.SetValue("Pmax", p_rated)
        cv.SetValue("Pmin", 0.0)
        q_lim = max(std["q_cap"], std["q_ind"]) * sn
        cv.SetValue("Qmax", q_lim)
        cv.SetValue("Qmin", -q_lim)
        cv.SetValue("cosphi_lim", round(math.sqrt(1 - std["q_ind"] ** 2), 3))
        cv.SetValue("Flag_CtrlPrior", 2)

        if vp_id is not None:
            cv.SetValue("Flag_Pctrl", 1)
            cv.SetValue("Qctrl_U_P_ID", int(vp_id))
        if PV_VOLTVAR_MODE == "volt_var" and vq_id is not None:
            cv.SetValue("Flag_Qctrl", 3)
            cv.SetValue("Qctrl_U_Q_ID", int(vq_id))
            q_pu = max(std["q_cap"], std["q_ind"])
            cv.SetValue("Qctrl_Qmin", -q_pu)
            cv.SetValue("Qctrl_Qmax",  q_pu)
        elif PV_VOLTVAR_MODE == "volt_pf":
            cv.SetValue("Flag_Qctrl", 1)
            cv.SetValue("Qctrl_cosphi_c", -round(math.sqrt(1 - std["q_cap"] ** 2), 4))
            cv.SetValue("Qctrl_U1_c", round(pct(vv[1]), 2))
            cv.SetValue("Qctrl_U2_c", round(pct(vv[0]), 2))
            cv.SetValue("Qctrl_cosphi", round(math.sqrt(1 - std["q_ind"] ** 2), 4))
            cv.SetValue("Qctrl_U1", round(pct(vv[2]), 2))
            cv.SetValue("Qctrl_U2", round(pct(vv[3]), 2))
        cv.Update()
        self.queue_phase(name, phases)

        if self.graphics:
            sx, sy = self._symbol_pos(node, side=+1)
            cv.CreateGraphic(sx, sy)
        ph_txt = "L" + "".join(str(k) for k in sorted(phases))
        print(f"    + PV conv   {name:<16} @ {node}  {ph_txt:<4} Sn={kva:.1f} kVA  "
              f"Pmax={p_rated * 1000:.2f} kW")
        return cv

    def verify_solve(self, method: str = "LF") -> bool:
        SimSrv = win32com.client.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
        Sim = SimSrv.GetSimulation()
        Sim.Database(self.conn)
        Sim.SetParameter("IDENTIFICATION", "Name")
        # Belt-and-braces: clear the same alt-data-source flags the DB
        # sets, in case a caller has changed them since create_database().
        for param, value in (("LFDATA_USE_LOAD_ASSIGN",      0),
                             ("LFDATA_USE_TIME_SERIES",      0),
                             ("LFDATA_USE_OPERATING_POINT",  0),
                             ("LFDATA_USE_LOAD_INCREASE",    0)):
            try:
                Sim.SetParameter(param, value)
            except Exception:
                pass
        Sim.LoadDB(method)
        Sim.Start(method)
        status = Sim.StatusID
        text = Sim.StatusText
        print(f"\n  Verification ({method}): status={status} ({text})")
        if status != 1101:
            kinds = {1: "STATUS", 2: "INFO", 3: "WARNING", 4: "ERROR", 6: "TASK"}
            msgs = Sim.Messages
            for i in range(1, msgs.Count + 1):
                m = msgs.Item(i)
                print(f"    [{kinds.get(m.Type, m.Type):>7}] {m.MessageId:>6}  {m.Text}")
        Sim = None
        SimSrv = None
        return status == 1101


# ---------------------------------------------------------------------------
# Top-level: build bus voltage map & translate
# ---------------------------------------------------------------------------

def build_bus_voltage_map(parser: OpenDSSParser) -> dict[str, float]:
    bus_kv: dict[str, float] = {}

    if parser.source_bus:
        bus_kv[parser.source_bus] = parser.base_kv

    for t in parser.transformers:
        for w in t.get("_windings", []):
            bus = strip_bus_node(w.get("bus", ""))
            try:
                kv = float(w.get("kv", "0"))
            except ValueError:
                kv = 0.0
            if bus and kv > 0:
                bus_kv[bus] = kv

    changed = True
    while changed:
        changed = False
        for ln in parser.lines + parser.switches:
            b1 = strip_bus_node(ln.get("bus1", [""])[0])
            b2 = strip_bus_node(ln.get("bus2", [""])[0])
            if b1 in bus_kv and b2 and b2 not in bus_kv:
                bus_kv[b2] = bus_kv[b1]; changed = True
            elif b2 in bus_kv and b1 and b1 not in bus_kv:
                bus_kv[b1] = bus_kv[b2]; changed = True

    common = Counter(round(v, 3) for v in bus_kv.values()).most_common(1)
    fallback = common[0][0] if common else parser.base_kv

    for coll in (parser.lines, parser.switches, parser.loads, parser.reactors):
        for obj in coll:
            for key in ("bus1", "bus2"):
                if key in obj:
                    b = strip_bus_node(obj[key][0])
                    if b and b not in bus_kv:
                        bus_kv[b] = fallback
    return bus_kv


def build_linecode_lookup(parser: OpenDSSParser) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, p in parser.linecodes.items():
        try:
            nph = int(float(p.get("nphases", ["3"])[0]))
        except ValueError:
            nph = 3
        try:
            Rm = parse_matrix(p.get("rmatrix", [None])[0])
            Xm = parse_matrix(p.get("xmatrix", [None])[0])
            Cm = parse_matrix(p.get("cmatrix", [None])[0])
        except Exception as exc:
            print(f"  [warn] linecode '{name}': {exc}")
            Rm = Xm = Cm = None

        seq = matrix_to_seq(Rm, Xm, Cm, nph)
        if seq is None and "r1" in p:
            f = lambda k, d: float(p.get(k, [d])[0])
            r1, x1, c1 = f("r1", 0.0), f("x1", 0.0), f("c1", 0.0)
            seq = {"R1": r1, "X1": x1, "C1": c1,
                   "R0": f("r0", r1), "X0": f("x0", x1), "C0": f("c0", c1),
                   "asym": 0.0}
        if seq is None:
            print(f"  [warn] linecode '{name}': no matrices or sequence data, "
                  f"using placeholder impedances")
            seq = {"R1": 0.1, "X1": 0.1, "C1": 0.0, "R0": 0.3, "X0": 0.3,
                   "C0": 0.0, "asym": 0.0}
        elif seq["asym"] > 0.05:
            print(f"  [note] linecode '{name}': phase impedances differ by up "
                  f"to {seq['asym']*100:.0f} % - SINCAL sequence data uses the mean")
        seq["units"] = str(p.get("units", ["none"])[0]).lower()
        out[name] = seq
    return out


def convert_length_to_km(value: float, units: str) -> float:
    units = (units or "km").lower()
    if units == "km":
        return value
    if units == "m":
        return value / 1000.0
    if units == "ft":
        return value * 0.0003048
    if units == "kft":
        return value * 0.3048
    if units == "mi":
        return value * 1.60934
    if units == "in":
        return value * 0.0254
    if units == "cm":
        return value / 100000.0
    return value


def select_pv_buses(parser: OpenDSSParser, bus_kv: dict[str, float],
                    cfg: PVConfig, phase_aware: bool = True
                    ) -> list[tuple[str, tuple[int, ...]]]:
    host: dict[str, tuple[int, ...]] = {}
    for ld in parser.loads:
        b = strip_bus_node(ld.get("bus1", [""])[0])
        if bus_kv.get(b, 99) < 1.0 and b not in host:
            host[b] = load_connection(ld)[1] if phase_aware else (1, 2, 3)
    if cfg.all_lv_buses:
        cand = sorted(b for b, kv in bus_kv.items() if kv < 1.0 and b)
        for b in cand:
            host.setdefault(b, (1, 2, 3))
    else:
        cand = sorted(host)
    k = int(round(len(cand) * cfg.pct / 100.0))
    if cfg.pct > 0:
        k = max(1, k)
    rng = random.Random(cfg.seed)
    chosen = sorted(rng.sample(cand, min(k, len(cand))))
    print(f"    PV buses     : {len(chosen)} of {len(cand)} candidate LV buses "
          f"({cfg.pct:.0f} %, seed={cfg.seed})")
    return [(b, host[b]) for b in chosen]


def _rename_bus(raw: str, alias: dict[str, str]) -> str:
    name, dot, rest = raw.partition(".")
    return alias.get(name, name) + dot + rest


def merge_closed_switches_in(parser: OpenDSSParser) -> int:
    closed = [sw for sw in parser.switches if "closed" in sw["_name"].lower()]
    uf: dict[str, str] = {}

    def find(x):
        uf.setdefault(x, x)
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    for sw in closed:
        a = strip_bus_node(sw.get("bus1", [""])[0])
        b = strip_bus_node(sw.get("bus2", [""])[0])
        if a and b:
            uf[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for x in list(uf):
        groups.setdefault(find(x), []).append(x)
    alias: dict[str, str] = {}
    for members in groups.values():
        rep_bus = (parser.source_bus if parser.source_bus in members
                   else sorted(members)[0])
        for m in members:
            if m != rep_bus:
                alias[m] = rep_bus
    if not alias:
        return 0
    for coll in (parser.lines, parser.switches, parser.reactors, parser.loads,
                 parser.vsources):
        for o in coll:
            for k in ("bus1", "bus2"):
                if k in o:
                    o[k] = [_rename_bus(o[k][0], alias)]
    for t in parser.transformers:
        for w in t.get("_windings", []):
            if "bus" in w:
                w["bus"] = _rename_bus(w["bus"], alias)
    if parser.source_bus:
        parser.source_bus = alias.get(parser.source_bus, parser.source_bus)
    parser.switches = [sw for sw in parser.switches if sw not in closed and
                       strip_bus_node(sw.get("bus1", [""])[0]) !=
                       strip_bus_node(sw.get("bus2", [""])[0])]
    print(f"    merged {len(closed)} closed switches -> {len(alias)} fewer nodes")
    return len(alias)


def is_neutral_grounding(rx: dict) -> bool:
    terms = rx.get("bus1", [""])[0].split(".")[1:]
    phase_terms = [t for t in terms if t in ("1", "2", "3")]
    neutral = any(t in ("0", "4") for t in terms)
    if neutral and not phase_terms:
        return True
    if "bus2" in rx:
        return True
    return "ground" in rx["_name"].lower() and not phase_terms and \
        str(rx.get("phases", ["1"])[0]) == "1"


def run_conversion(master: str, out_dir: str | None, out_name: str | None,
                   no_graphics: bool = False,
                   pv: PVConfig | None = None,
                   phase_aware: bool = True,
                   merge_closed_switches: bool | None = None,
                   legacy_ground_shunts: bool = False) -> None:
    pv = pv or PVConfig()
    if pv.region not in PV_REGIONS:
        raise ValueError(f"PV region '{pv.region}' not defined in PV_REGIONS")
    print(f"=== 1. Parsing OpenDSS master: {master}")
    parser = OpenDSSParser(master).parse()
    print(f"    circuit      : {parser.circuit_name}")
    print(f"    source bus   : {parser.source_bus}")
    print(f"    base kV      : {parser.base_kv}")
    print(f"    base freq    : {parser.base_freq} Hz")
    print(f"    linecodes    : {len(parser.linecodes)}")
    print(f"    lines        : {len(parser.lines)}")
    print(f"    switches     : {len(parser.switches)}")
    print(f"    transformers : {len(parser.transformers)}")
    print(f"    loads        : {len(parser.loads)}")
    print(f"    reactors     : {len(parser.reactors)}")

    if merge_closed_switches is None:
        approx = build_bus_voltage_map(parser)
        merge_closed_switches = len(approx) > 50
        if merge_closed_switches:
            print(f"  [auto] {len(approx)} nodes > 50 -> merging closed switches")

    if merge_closed_switches:
        merge_closed_switches_in(parser)

    bus_kv = build_bus_voltage_map(parser)
    if len(bus_kv) > 50:
        print(f"  [WARN] {len(bus_kv)} nodes: the demo/student licence simulates at "
              f"most 50.  Try merge_closed_switches=True (--merge-closed-switches).")
    lc_lookup = build_linecode_lookup(parser)
    if phase_aware:
        phase_summary(parser.loads)

    adjacency: dict[str, set[str]] = {b: set() for b in bus_kv}

    def _link(a, b):
        if a and b:
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)

    for ln in parser.lines + parser.switches:
        _link(strip_bus_node(ln.get("bus1", [""])[0]),
              strip_bus_node(ln.get("bus2", [""])[0]))
    for t in parser.transformers:
        ws = t.get("_windings", [])
        for i in range(len(ws) - 1):
            _link(strip_bus_node(ws[i].get("bus", "")),
                  strip_bus_node(ws[i + 1].get("bus", "")))

    positions = compute_layout(list(bus_kv.keys()), adjacency, parser.source_bus)
    if not no_graphics:
        print(f"    layout       : {len(positions)} node positions (BFS from "
              f"'{parser.source_bus}')")

    if out_dir is None:
        out_dir = os.path.dirname(os.path.abspath(master))
    if out_name is None:
        out_name = parser.circuit_name or "OpenDSS_Net"

    print(f"\n=== 2. Building PSS-SINCAL network '{out_name}'"
          f"{'' if not no_graphics else '  [graphics disabled]'}")
    b = SincalBuilder(out_dir, out_name, parser.base_freq,
                      graphics=not no_graphics)
    b.positions = positions
    b.create_database()
    b.read_schema()
    b.open_database()

    vl_names: dict[float, str] = {}
    for kv in sorted(set(round(v, 3) for v in bus_kv.values()), reverse=True):
        name = f"{kv}kV".replace(".", "_").replace("-", "m")
        vl_names[kv] = name
        b.ensure_voltage_level(name, kv)

    for bus, kv in bus_kv.items():
        b.ensure_node(bus, vl_names[round(kv, 3)])

    if parser.source_bus:
        b.create_infeeder(
            name="Source",
            node=parser.source_bus,
            kv=parser.base_kv,
            v_pu=1.0,
            angle_deg=0.0,
            sk_mva=100.0,
            r_x=0.1,
        )
    for vs in parser.vsources:
        bus = strip_bus_node(vs.get("bus1", [""])[0])
        kv = float(vs.get("basekv", [str(parser.base_kv)])[0])
        b.create_infeeder(
            name=vs["_name"],
            node=bus,
            kv=kv,
            v_pu=float(vs.get("pu", ["1.0"])[0]),
            angle_deg=float(vs.get("angle", ["0"])[0]),
            sk_mva=100.0,
        )

    for t in parser.transformers:
        ws = t.get("_windings", [])
        if len(ws) < 2:
            print(f"    [warn] transformer '{t['_name']}' has <2 windings - skipped")
            continue
        w1, w2 = ws[0], ws[1]
        b1 = strip_bus_node(w1.get("bus", ""))
        b2 = strip_bus_node(w2.get("bus", ""))
        try:
            kv1 = float(w1.get("kv", "11"))
            kv2 = float(w2.get("kv", "0.4"))
            kva = float(w1.get("kva", "100"))
        except ValueError:
            continue
        sn = kva / 1000.0
        try:
            uk = float(t.get("xhl", ["6"])[0])
        except (ValueError, KeyError):
            uk = 6.0
        if uk < 1.0:
            uk *= 100.0
        vg, vg_label = vecgrp_from_conns(w1.get("conn"), w2.get("conn"))
        b.create_transformer(t["_name"], b1, b2, kv1, kv2, sn, uk, 0.5,
                             vecgrp=vg, vg_label=vg_label)

    for ln in parser.lines:
        name = ln["_name"]
        b1 = strip_bus_node(ln.get("bus1", [""])[0])
        b2 = strip_bus_node(ln.get("bus2", [""])[0])
        lc = ln.get("linecode", [""])[0].lower()
        line_units = ln.get("units", ["km"])[0]
        try:
            L = convert_length_to_km(float(ln.get("length", ["0"])[0]),
                                     line_units)
        except ValueError:
            L = 0.0
        seq = lc_lookup.get(lc)
        if seq is None:
            print(f"    [warn] line '{name}': unknown linecode '{lc}'")
            seq = {"R1": 0.1, "X1": 0.1, "C1": 0.0, "R0": 0.3, "X0": 0.3,
                   "C0": 0.0, "units": "km"}
        lc_units = seq.get("units", "none")
        eff = line_units if lc_units in ("none", "") else lc_units
        per_km = 1.0 / (convert_length_to_km(1.0, eff) or 1.0)
        seq = {k: (v * per_km if k != "asym" and k != "units" else v)
               for k, v in seq.items()}
        b.create_line(name, b1, b2, L, seq)

    for sw in parser.switches:
        b1 = strip_bus_node(sw.get("bus1", [""])[0])
        b2 = strip_bus_node(sw.get("bus2", [""])[0])
        b.create_switch_line(sw["_name"], b1, b2)

    for ld in parser.loads:
        bus = strip_bus_node(ld.get("bus1", [""])[0])
        try:
            kw = float(ld.get("kw", ["1.0"])[0])
        except ValueError:
            kw = 1.0
        if "kvar" in ld:
            try:
                kvar = float(ld["kvar"][0])
            except ValueError:
                kvar = 0.0
        elif "pf" in ld:
            try:
                pf = float(ld["pf"][0])
                kvar = kw * math.tan(math.acos(abs(pf))) * (1 if pf >= 0 else -1)
            except (ValueError, ZeroDivisionError):
                kvar = 0.0
        else:
            kvar = 0.0
        conn = load_connection(ld) if phase_aware else None
        b.create_load(ld["_name"], bus, kw / 1000.0, kvar / 1000.0, conn)

    if pv.pct > 0:
        mode_label = "VarShuntElement at unity PF" if pv.use_varshunt \
                     else "DCInfeeder with AS/NZS 4777.2 controllers"
        print(f"\n    --- PV converters ({mode_label}) ---")
        std = PV_REGIONS[pv.region]
        chosen = select_pv_buses(parser, bus_kv, pv, phase_aware)
        if chosen:
            lv_kv = bus_kv[chosen[0][0]]
            v_base = lv_kv * 1000.0 / math.sqrt(3)
            pc = lambda v: 100.0 * v / v_base
            vv = std["vv"]
            vp_id = vq_id = None
            if not pv.use_varshunt:
                vp_id = b.create_characteristic(
                    f"PV_VoltWatt_{pv.region}", 14,
                    [(0.0, 1.0), (pc(std["vw1"]), 1.0),
                     (pc(std["vw2"]), std["p_vw2"]), (150.0, std["p_vw2"])])
                if PV_VOLTVAR_MODE == "volt_var":
                    sg = PV_Q_INDUCTIVE_SIGN
                    vq_id = b.create_characteristic(
                        f"PV_VoltVar_{pv.region}", 12,
                        [(0.0,       +sg * std["q_cap"]),
                         (pc(vv[0]), +sg * std["q_cap"]),
                         (pc(vv[1]),  0.0),
                         (pc(vv[2]),  0.0),
                         (pc(vv[3]), -sg * std["q_ind"]),
                         (150.0,     -sg * std["q_ind"])])
            else:
                print("    (unity PF, constant P/Q: volt-var / volt-watt "
                      "characteristics not applied)")
            pv_rng = random.Random(None if pv.seed is None else pv.seed + 1)
            for bus, phases in chosen:
                kva = pv_rng.choice(pv.sizes_kva)
                b.create_pv_converter(f"PV_{bus}", bus, kva, kva * pv.kwp_ratio,
                                      pv, phases=phases, v_base=v_base,
                                      kv_ll=lv_kv, vp_id=vp_id, vq_id=vq_id)

    skipped_gnd = []
    for rx in parser.reactors:
        if not legacy_ground_shunts and is_neutral_grounding(rx):
            skipped_gnd.append(f"{rx['_name']} ({rx.get('bus1', [''])[0]}, "
                               f"R={rx.get('r', ['0'])[0]})")
            continue
        bus = strip_bus_node(rx.get("bus1", [""])[0])
        try:
            R = float(rx.get("r", ["0"])[0])
            X = float(rx.get("x", ["0"])[0])
        except ValueError:
            continue
        kv = bus_kv.get(bus, parser.base_kv)
        b.create_shunt_impedance(rx["_name"], bus, R, X, kv)

    if skipped_gnd:
        print(f"\n    Neutral groundings NOT modelled as shunts ({len(skipped_gnd)}): "
              "the 4-wire neutral is treated as solidly/multi-grounded.")
        print("      " + "; ".join(skipped_gnd[:4]) + (" ..." if len(skipped_gnd) > 4 else ""))
    b.close()
    b.apply_pending_phases_sql()
    if phase_aware or pv.pct > 0:
        b.report_phase_status(use_varshunt=pv.use_varshunt)
    if pv.pct > 0:
        b.report_converters_sql(use_varshunt=pv.use_varshunt)

    print("\n=== 3. Verifying with a power flow")
    ok = b.verify_solve("LF_USYM" if phase_aware else "LF")
    print(f"\n=== DONE")
    print(f"    SIN file : {b.sin_file}")
    print(f"    Database : {b.db_file}")
    print(f"    Solvable : {ok}")
    if ok and not no_graphics:
        print(f"    Graphics : created  (open the .sin and press Ctrl+Shift+F)")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Convert an OpenDSS LV network into PSS-SINCAL V22.5.")
    ap.add_argument("master", help="Path to the OpenDSS master .dss file")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--no-graphics", action="store_true")
    ap.add_argument("--merge-closed-switches", action="store_true", default=None)
    ap.add_argument("--no-merge-closed-switches", action="store_false",
                    dest="merge_closed_switches")
    ap.add_argument("--legacy-ground-shunts", action="store_true")
    ap.add_argument("--balanced", action="store_true")
    g = ap.add_argument_group("PV generation")
    g.add_argument("--pv-pct", type=float, default=0.0)
    g.add_argument("--pv-seed", type=int, default=None)
    g.add_argument("--pv-kva-list", default="5")
    g.add_argument("--pv-kwp-ratio", type=float, default=1.0)
    g.add_argument("--pv-output", type=float, default=1.0)
    g.add_argument("--pv-all-lv-buses", action="store_true")
    g.add_argument("--pv-region", default="A")
    g.add_argument("--pv-element", choices=["varshunt", "dcinfeeder"],
                   default="varshunt")
    args = ap.parse_args()

    if not 0.0 <= args.pv_pct <= 100.0:
        ap.error("--pv-pct must be between 0 and 100")
    pv = PVConfig(pct=args.pv_pct, seed=args.pv_seed,
                  sizes_kva=tuple(float(x) for x in args.pv_kva_list.split(",")),
                  kwp_ratio=args.pv_kwp_ratio, output_pu=args.pv_output,
                  all_lv_buses=args.pv_all_lv_buses,
                  region=args.pv_region.upper(),
                  use_varshunt=(args.pv_element == "varshunt"))

    try:
        run_conversion(args.master, args.outdir, args.name,
                       no_graphics=args.no_graphics, pv=pv,
                       phase_aware=not args.balanced,
                       merge_closed_switches=args.merge_closed_switches,
                       legacy_ground_shunts=args.legacy_ground_shunts)
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()