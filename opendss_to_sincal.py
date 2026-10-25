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
                                [--pv-region A] [--pv-element varshunt|converter]
                                [--source-sk-mva MVA]
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
LF_USYM needs a zero-sequence model on every element, otherwise elements are
reported "partially isolated (zero system)" (messages 1057 / 1045) and left
out of the calculation: the solve succeeds, but they have no effect.

Realistic residential LV values are applied (defaults at the top of the file):
  Lines        r0 / x0 from the linecode (Kron-reduced neutral); if absent,
               Z0 = 4 R1 + j 3 X1 (four-core LV cable).  Closed switches are
               1 m of cable (not a SINCAL "connector", which is open in the
               zero system).
  Transformer  Dyn11, uk 4.5 %, ur from the .dss %r (else 1.0 %), i0 0.3 %,
               Z0/Z1 = 0.95 (delta winding), R0/X0 from ur and uk.
  Network feeder  Sk'' 250 MVA (--source-sk-mva), R/X 0.1, solidly grounded,
               Z0 = Z1.
  Loads        Grounded wye, Z0 = Z1, R0/X0 from the load's own P/Q
               (pf 0.95 if the .dss gives neither kvar nor pf).
  PV shunt     Same convention as the load.

Every value is written under its real database column name (SINCAL silently
ignores wrong names such as "R"/"Len" on Line, whose columns are r/l) and is
re-checked and, if needed, corrected directly in the database after the
build ("Field check" in the log).  After the solve, any element SINCAL left
out of the power flow is listed.

PV option
---------
``--pv-pct 40`` puts rooftop PV at a random 40 % of the customer buses
(buses carrying a load); ``--pv-pct 100`` gives every load a PV system.

``--pv-element varshunt`` (default): VarShuntElement, constant P/Q at unity
power factor.  It supports a single-phase terminal together with a
zero-sequence model, so LF_USYM handles it on the household's own phase.

``--pv-element converter`` (alias ``dcinfeeder``): a SINCAL Converter set to
plant type Photovoltaic - inverter rating (--pv-kva-list), array size
(--pv-kwp-ratio, default 1.3 DC/AC), Umin/Umax 80/115 %, P and Q limits,
fault contribution 1.1 x rated current, and the AS/NZS 4777.2:2020 volt-var
and volt-watt characteristics.  The converter has no zero-sequence fields,
so check the post-build message list: if SINCAL reports it "not considered in
simulation" on a single-phase terminal, use the varshunt option.

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
# (The old +1.0 had this inverted and pushed every converter unstable.)
PV_Q_INDUCTIVE_SIGN = -1.0


@dataclass
class PVConfig:
    pct: float = 0.0                 # % of candidate buses that get PV
    seed: int | None = None
    sizes_kva: tuple = (5.0,)        # inverter ratings drawn at random (kVA)
    kwp_ratio: float = 1.3           # DC array kWp / inverter kVA (typical AU oversizing)
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
        while len(vals) < i + 1:      # pad for lower triangular
            vals.append(0.0)
        mat.append(vals)
    return mat


def _sym(m):
    """Lower-triangular (or full) OpenDSS matrix -> full symmetric 2D list."""
    n = len(m)
    return [[m[i][j] if j < len(m[i]) else m[j][i] for j in range(n)]
            for i in range(n)]


def _kron_reduce_last(Z):
    """Eliminate the last conductor (assumed solidly grounded neutral)."""
    n = len(Z) - 1
    return [[Z[i][j] - Z[i][n] * Z[n][j] / Z[n][n] for j in range(n)]
            for i in range(n)]


def _seq_from_phase_matrix(M):
    """
    Phase matrix (3x3, or smaller) -> (pos, zero, asymmetry).
    3-phase:  Z1 = Zs - Zm,  Z0 = Zs + 2 Zm   (Zs = mean diag, Zm = mean offdiag)
    1/2-phase: no mutual coupling is assumed, Z1 = Z0 = Zs, which reproduces
    the loop impedance seen by a single-phase service/spur.
    """
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
    """
    Convert OpenDSS linecode R/X/C matrices into SINCAL sequence data.
    A 4-conductor matrix (3 phases + neutral) is Kron-reduced first.
    Returns per-length-unit values.
    """
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
              frozenset({1, 2, 3}): 7}          # L1 L2 L3 L12 L23 L31 L123


def load_connection(ld: dict) -> tuple[str, tuple[int, ...]]:
    """
    Work out how an OpenDSS load is connected from its bus1 terminals,
    ``phases`` and ``conn``.  Returns (kind, phase_nodes):
      ("3ph", (1,2,3))  balanced three-phase
      ("1ph", (k,))     phase-to-neutral on phase k
      ("ll",  (a, b))   phase-to-phase (delta) between a and b
      ("2ph", (a, b))   two phases, wye (power split equally)
    """
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
    """
    OpenDSS winding connections -> SINCAL vector group (index, label).
    Neutrals are grounded (OpenDSS default), so wye -> YN/yn.
    """
    d1, d2 = is_delta(c1), is_delta(c2)
    return {(False, False): (5, "YNyn0"), (True, False): (59, "Dyn11"),
            (True, True): (1, "Dd0"), (False, True): (14, "YNd1")}[(d1, d2)]


def phase_summary(loads: list[dict]) -> None:
    """Print the allocation of load kW/kvar to phases L1/L2/L3."""
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
    """Return only the bus name (drop .N.N.N terminal suffix)."""
    return bus.split(".")[0] if bus else bus


# ---------------------------------------------------------------------------
# Residential LV defaults: zero-sequence and equipment data
# ---------------------------------------------------------------------------
# Used wherever the OpenDSS file does not supply a value.  Typical figures for
# an Australian 11 kV / 433 V (230/400 V) residential feeder.

# Household load power factor when the .dss gives neither kvar nor pf.
DEFAULT_LOAD_PF = 0.95

# Ground-mounted distribution transformer, 100-500 kVA, Dyn11.
TX_DEFAULTS = dict(
    uk_pct=4.5,     # short-circuit voltage (%)
    ur_pct=1.0,     # ohmic part, i.e. copper losses ~1 % of Sn
    i0_pct=0.3,     # no-load current (%)
    z0_z1=0.95,     # Dyn: Z0 ~ Z1 (delta winding circulates the zero sequence)
)

# Used for a cable only if its linecode carries no usable zero-sequence data.
# LV four-core cable with a neutral: Z0 is roughly 3-4 times Z1.
LV_CABLE_R0_R1 = 4.0
LV_CABLE_X0_X1 = 3.0

# Closed switch / link modelled as 1 m of cable with a negligible impedance.
SWITCH_LEN_KM = 0.001
SWITCH_Z_OHM_PER_KM = 0.01       # -> 10 uOhm
SWITCH_Z0_OHM_PER_KM = 0.03

# MV network feeder at the transformer's HV busbar.  Typical 11 kV zone
# substation fault level 150-350 MVA; the delta winding of the Dyn transformer
# blocks MV zero sequence from the LV network, so Z0/Z1 has little effect on it.
SOURCE_SK_MVA = 250.0
SOURCE_R_X = 0.1
SOURCE_Z0_Z1 = 1.0

# Element.Flag_Input is a bit mask that switches input sections on in SINCAL.
# Found by ticking the boxes in the GUI and diffing the database:
#   default 3;  +4    = "Zero-Phase Sequence Data" enabled (line, load,
#                        transformer, feeder, ...);  +4096 = converter
#                        "Power Control" enabled.
# Without the bit the stored values are ignored (greyed out in the GUI).
INPUT_ZERO_SEQ = 4
INPUT_POWER_CTRL = 4096

# Vector groups (SINCAL VecGrp codes) with a delta winding and a grounded star.
_DELTA_GROUNDED_STAR = {10, 24, 45, 59, 79, 14, 26, 49, 61, 80, 81}


def rx_ratio(p: float, q: float, lo: float = 0.5, hi: float = 10.0) -> float:
    """R/X of a constant-power load or PV from its P and Q (clamped)."""
    if abs(q) < 1e-12:
        return hi
    return min(hi, max(lo, abs(p / q)))


def tx_zero_sequence(vecgrp: int, uk: float, ur: float) -> tuple[float, float]:
    """(Z0/Z1, R0/X0) for a two-winding distribution transformer."""
    z0_z1 = TX_DEFAULTS["z0_z1"] if vecgrp in _DELTA_GROUNDED_STAR else 1.0
    xk = math.sqrt(max(uk * uk - ur * ur, 1e-12))
    return z0_z1, max(ur / xk, 0.01)


def lv_cable_zero_sequence(r1: float, x1: float, r0: float, x0: float
                           ) -> tuple[float, float, bool]:
    """
    Zero-sequence R0/X0 (ohm/km).  Uses the linecode's own values when they
    exist; otherwise falls back to Z0 = 4 R1 + j 3 X1.  Returns (r0, x0, fell_back).
    """
    if abs(r0) < 1e-9 and abs(x0) < 1e-9:
        return (max(r1, 1e-5) * LV_CABLE_R0_R1,
                max(x1, 1e-5) * LV_CABLE_X0_X1, True)
    return max(r0, 1e-5), max(x0, 1e-5), False


def winding_r_pct(trafo: dict):
    """Sum of the two winding %r values from a parsed OpenDSS transformer, or None."""
    ws = trafo.get("_windings", [])
    try:
        vals = [float(w["%r"]) for w in ws[:2] if "%r" in w]
    except ValueError:
        return None
    return sum(vals) if len(vals) == 2 else None


# ---------------------------------------------------------------------------
# Auto-layout: BFS radial layout in metres
# ---------------------------------------------------------------------------

def compute_layout(buses: list[str],
                   adjacency: dict[str, set[str]],
                   source: str | None) -> dict[str, tuple[float, float]]:
    """Simple BFS-based layout: one horizontal 'column' per BFS depth."""
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

    dx = 0.15     # m between depth levels
    dy = 0.08     # m between nodes at the same depth
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

    # -- public --
    def parse(self) -> "OpenDSSParser":
        self._parse_file(self.master_file)
        self._apply_batchedits()
        return self

    # -- internal --
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

        # redirect / compile
        m = re.match(r'(?:redirect|compile)\s+["\']?([^"\']+?)["\']?\s*$', low)
        if m:
            target = self._resolve(m.group(1).strip())
            if target:
                self._parse_file(target)
            else:
                print(f"  [warn] cannot resolve redirect '{m.group(1)}'")
            return

        # set basefrequency / defaultbasefrequency
        m = re.match(r'set\s+(?:default)?basefrequency\s*=\s*([\d.]+)', low)
        if m:
            self.base_freq = float(m.group(1))
            return

        # new circuit
        m = re.match(r'new\s+circuit\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            self.circuit_name = m.group(1)
            p = parse_named_params(m.group(2))
            self.base_kv = float(p.get("basekv", ["11.0"])[0])
            if "bus1" in p:
                self.source_bus = strip_bus_node(p["bus1"][0])
            return

        # new linecode
        m = re.match(r'new\s+linecode\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            self.linecodes[m.group(1).lower()] = parse_named_params(m.group(2))
            return

        # new line (may be a switch)
        m = re.match(r'new\s+line\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            name = m.group(1)
            p = parse_named_params(m.group(2))
            p["_name"] = name
            is_switch = str(p.get("switch", ["no"])[0]).lower() in ("y", "yes", "true")
            (self.switches if is_switch else self.lines).append(p)
            return

        # new transformer
        m = re.match(r'new\s+transformer\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            name = m.group(1)
            p = parse_named_params(m.group(2))
            p["_name"] = name
            p["_windings"] = self._parse_windings(m.group(2))
            self.transformers.append(p)
            return

        # new reactor (groundings)
        m = re.match(r'new\s+reactor\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            p = parse_named_params(m.group(2))
            p["_name"] = m.group(1)
            self.reactors.append(p)
            return

        # new load
        m = re.match(r'new\s+load\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            p = parse_named_params(m.group(2))
            p["_name"] = m.group(1)
            self.loads.append(p)
            return

        # new vsource
        m = re.match(r'new\s+vsource\.(\S+)\s+(.*)', cmd, re.IGNORECASE)
        if m:
            p = parse_named_params(m.group(2))
            p["_name"] = m.group(1)
            self.vsources.append(p)
            return

        # batchedit <class>..<regex> <edits>
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

        # Layout / graphic bookkeeping
        self.positions: dict[str, tuple[float, float]] = {}
        self._elem_count: dict[str, int] = {}     # elements placed at each bus

        # PV / converter bookkeeping
        self._columns: dict[str, dict[str, str]] = {}   # table -> {lower: real}
        self.unresolved: set[str] = set()
        self.vp_char_id: int | None = None
        self.unphased: list[str] = []
        self.ctrl_modes_missing = False
        self.pending_phases: list[tuple[str, int]] = []   # (element name, code)
        self.phase_cols: list[tuple[str, str]] = []
        self._intended: list[tuple[str, str, str, object]] = []   # (table, element, column, value)
        self.unknown_fields: set[tuple[str, str]] = set()

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
                self._setparam(self.db, "GraphicArea", 1)   # default view
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

    # ---- layout helper ---------------------------------------------------
    def _symbol_pos(self, bus: str, side: int = +1) -> tuple[float, float]:
        """Small offset from the node's position so node-type elements don't overlap."""
        x, y = self.positions.get(bus, (0.0, 0.0))
        n = self._elem_count.get(bus, 0)
        self._elem_count[bus] = n + 1
        return (x + 0.025 * n, y + side * 0.040)

    # ---- safe field writer ------------------------------------------------
    def _f(self, obj, table: str, owner: str, field: str, value) -> None:
        """
        Set one field.  The name is matched (case-insensitively) against the
        real database column, because SINCAL silently ignores SetValue calls
        with a wrong name (e.g. "R" or "Len" on Line, whose columns are r / l).
        Unknown names are reported once.  Every write is recorded so
        apply_fields_sql() can enforce and verify it after the build.
        """
        cols = self._columns.get(table)
        real = cols.get(field.lower()) if cols else field
        if real is None:
            if (table, field) not in self.unknown_fields:
                self.unknown_fields.add((table, field))
                print(f"  [WARN] {table} has no column '{field}' - value not written")
            return
        obj.SetValue(real, value)
        self._intended.append((table, owner, real, value))

    # ---- element creators ------------------------------------------------
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
        n.SetValue("Node.Flag_Typ", 4)       # busbar
        n.Update()
        if self.graphics and name in self.positions:
            x, y = self.positions[name]
            n.CreateGraphic(x, y, x + 0.02, y)
        self.nodes[name] = n.RowId
        return n.RowId

    def create_infeeder(self, name, node, kv, v_pu=1.0, angle_deg=0.0,
                        sk_mva=SOURCE_SK_MVA, r_x=SOURCE_R_X):
        T = "Infeeder"
        n = self.db.GetCommonObject("Node", node)
        inf = self.db.CreateElement("Infeeder", name, n)
        Z = (kv ** 2) / sk_mva if sk_mva > 0 else 1e-6
        X = Z / math.sqrt(1 + r_x ** 2)
        R = r_x * X
        f = lambda k, v: self._f(inf, T, name, k, v)
        f("Flag_Lf", 3)                      # |usrc| & delta
        f("Flag_Typ", 1)                     # R & X
        f("R", R)
        f("X", X)
        f("R_X", r_x)
        f("Sk2", float(sk_mva))
        f("u", v_pu * 100.0)
        f("delta", angle_deg)
        # ---- zero sequence: solidly grounded source, Z0 = Z1 ----
        f("Flag_Z0", 1)                      # fixed grounded
        f("Flag_Z0_Input", 1)                # Z0/Z1 and R0/X0
        f("Z0_Z1", SOURCE_Z0_Z1)
        f("R0_X0", r_x)
        inf.Update()
        if self.graphics:
            sx, sy = self._symbol_pos(node, side=+1)
            inf.CreateGraphic(sx, sy)
        print(f"    + Infeeder  {name:<10} @ {node}  ({kv} kV, Sk''={sk_mva:.0f} MVA)")
        return inf

    def create_line(self, name, n1_name, n2_name, length_km, seq):
        T = "Line"
        n1 = self.db.GetCommonObject("Node", n1_name)
        n2 = self.db.GetCommonObject("Node", n2_name)
        ln = self.db.CreateElement("Line", name, n1, n2)
        f = lambda k, v: self._f(ln, T, name, k, v)
        f("Flag_LineTyp", 1)                 # cable
        # Floor the length at 1 m: some OpenDSS LV networks have sub-mm
        # segments that would trigger "2603 Short circuit impedance is zero".
        f("l", max(float(length_km), 1e-3))
        f("ParSys", 1.0)
        f("r", float(seq["R1"]))
        f("x", float(seq["X1"]))
        f("c", float(seq["C1"]))
        f("fn", float(self.freq))
        # ---- zero sequence: explicit r0 / x0 in ohm/km, never 0 ----
        r0, x0, fell_back = lv_cable_zero_sequence(
            float(seq["R1"]), float(seq["X1"]), float(seq["R0"]), float(seq["X0"]))
        f("Flag_Z0_Input", 2)                # r0 and x0
        f("r0", r0)
        f("x0", x0)
        f("c0", float(seq.get("C0", 0.0)))
        ln.Update()
        if self.graphics:
            ln.CreateGraphic()
        tag = "  (Z0 default: 4R1 + j3X1)" if fell_back else ""
        print(f"    + Line      {name:<12} {n1_name} -> {n2_name}  "
              f"({length_km:.6f} km){tag}")
        return ln

    def create_switch_line(self, name, n1_name, n2_name):
        """
        Closed switch / link = 1 m of cable with a negligible impedance.
        (A SINCAL 'connector' line type is NOT used: with it SINCAL treats the
        switch as open in the zero-sequence system and drops everything behind
        it from the unbalanced power flow - warnings 1045 / 1057.)
        """
        T = "Line"
        n1 = self.db.GetCommonObject("Node", n1_name)
        n2 = self.db.GetCommonObject("Node", n2_name)
        ln = self.db.CreateElement("Line", name, n1, n2)
        f = lambda k, v: self._f(ln, T, name, k, v)
        f("Flag_LineTyp", 1)                 # cable
        f("l", SWITCH_LEN_KM)
        f("ParSys", 1.0)
        f("r", SWITCH_Z_OHM_PER_KM)
        f("x", SWITCH_Z_OHM_PER_KM)
        f("c", 0.0)
        f("fn", float(self.freq))
        f("Flag_Z0_Input", 2)
        f("r0", SWITCH_Z0_OHM_PER_KM)
        f("x0", SWITCH_Z0_OHM_PER_KM)
        f("c0", 0.0)
        ln.Update()
        if self.graphics:
            ln.CreateGraphic()
        print(f"    + Switch    {name:<12} {n1_name} -> {n2_name}")
        return ln

    def create_transformer(self, name, n1_name, n2_name,
                           kv1, kv2, sn_mva,
                           uk_pct=TX_DEFAULTS["uk_pct"], ur_pct=TX_DEFAULTS["ur_pct"],
                           vecgrp=59, vg_label=""):
        T = "TwoWindingTransformer"
        n1 = self.db.GetCommonObject("Node", n1_name)
        n2 = self.db.GetCommonObject("Node", n2_name)
        t = self.db.CreateElement("TwoWindingTransformer", name, n1, n2)
        f = lambda k, v: self._f(t, T, name, k, v)
        f("Sn", float(sn_mva))
        f("Un1", float(kv1))
        f("Un2", float(kv2))
        f("uk", float(uk_pct))
        f("Flag_ur", 1)                      # ur = ohmic part of uk
        f("ur", float(ur_pct))
        f("i0", TX_DEFAULTS["i0_pct"])
        f("VecGrp", int(vecgrp))
        f("Flag_Lf", 1)
        f("Flag_ConNode", 1)
        # ---- zero sequence ----
        z0_z1, r0_x0 = tx_zero_sequence(int(vecgrp), float(uk_pct), float(ur_pct))
        f("Flag_Z0_Input", 1)                # Z0/Z1 and R0/X0
        f("Z0_Z1", z0_z1)
        f("R0_X0", r0_x0)
        t.Update()
        if self.graphics:
            t.CreateGraphic()
        print(f"    + Trafo     {name:<12} {n1_name}({kv1}kV) -> {n2_name}({kv2}kV)  "
              f"{vg_label}  {sn_mva*1000:.0f} kVA uk={uk_pct:.1f}% ur={ur_pct:.2f}% "
              f"Z0/Z1={z0_z1:.2f}")
        return t

    def create_load(self, name, node, p_mw, q_mvar, conn=None):
        """
        conn: (kind, phase_nodes) from load_connection(); None or "3ph" gives
        the original balanced load.
        """
        T = "Load"
        n = self.db.GetCommonObject("Node", node)
        ld = self.db.CreateElement("Load", name, n)
        f = lambda k, v: self._f(ld, T, name, k, v)
        kind, ph = conn if conn else ("3ph", (1, 2, 3))
        f("Flag_Load", 1)
        f("Flag_Typified", 3)                # household
        f("Flag_LoadType", 2)                # P, Q constant
        # ---- zero sequence: grounded (wye) load, Z0 = Z1, R/X from its pf ----
        f("Flag_Z0", 1)                      # fixed grounded
        f("Flag_Z0_Input", 1)                # Z0/Z1 and R0/X0
        f("Z0_Z1", 1.0)
        f("R0_X0", rx_ratio(p_mw, q_mvar))
        f("P", float(p_mw))
        f("Q", float(q_mvar))
        if kind in ("1ph", "ll"):
            self.queue_phase(name, ph)       # Terminal.Flag_Terminal, written post-build
        if kind == "3ph":
            f("Flag_Lf", 1)
            where = "L123"
        elif kind == "1ph":
            f("Flag_Lf", 1)
            where = "L" + "".join(str(k) for k in sorted(ph))
        elif kind == "2ph":
            f("Flag_Lf", 13)                 # Pi, Qi and (v) - star
            for k in (1, 2, 3):
                share = (1.0 / len(ph)) if k in ph else 0.0
                f(f"P{k}", float(p_mw) * share)
                f(f"Q{k}", float(q_mvar) * share)
            where = "L" + "".join(str(k) for k in ph)
        else:                                # "ll": phase-to-phase
            f("Flag_Lf", 14)                 # Pij, Qij and (v) - delta
            key = {frozenset({1, 2}): "12", frozenset({2, 3}): "23",
                   frozenset({1, 3}): "31"}[frozenset(ph)]
            for kk in ("12", "23", "31"):
                on = 1.0 if kk == key else 0.0
                f(f"P{kk}", float(p_mw) * on)
                f(f"Q{kk}", float(q_mvar) * on)
            where = "L" + key
        ld.Update()
        if self.graphics:
            sx, sy = self._symbol_pos(node, side=-1)
            ld.CreateGraphic(sx, sy)
        print(f"    + Load      {name:<16} @ {node}  {where:<5} "
              f"P={p_mw:.4f} MW  Q={q_mvar:.4f} Mvar")
        return ld

    def create_shunt_impedance(self, name, node, r_ohm, x_ohm, kv):
        T = "ShuntImpedance"
        n = self.db.GetCommonObject("Node", node)
        sh = self.db.CreateElement("ShuntImpedance", name, n)
        f = lambda k, v: self._f(sh, T, name, k, v)
        f("Un", float(kv))
        f("R", float(r_ohm))
        f("X", float(x_ohm))
        f("Flag_Lf", 1)
        # ---- zero sequence: grounded shunt, Z0 = Z1 ----
        f("Flag_Z0", 1)
        f("Flag_Z0_Input", 1)
        f("Z0_Z1", 1.0)
        f("R0_X0", 1.0)
        sh.Update()
        if self.graphics:
            sx, sy = self._symbol_pos(node, side=-1)
            sh.CreateGraphic(sx, sy)
        print(f"    + ShuntImp  {name:<20} @ {node}  R={r_ohm} X={x_ohm}")
        return sh

    # ---- converter (PV) support -----------------------------------------
    def read_schema(self, tables=None) -> None:
        """Read table/column names from the SQLite database (read-only)."""
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
        """Show how loads / converters ended up distributed over L1/L2/L3."""
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
        """Read the converters / PV shunts back to confirm what was stored."""
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
        """Create a Characteristic (+ CharacteristicValue rows); returns its id."""
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
        """
        Single-phase PV as a VarShuntElement (constant P/Q injection).

        VarShuntElement carries a zero-sequence model (Flag_Z0_Input / Z0_Z1 /
        R0_X0) next to a single-phase terminal, so it works in LF_USYM on the
        same phase as the household load.  Plf uses SINCAL's LOAD convention,
        so generation is NEGATIVE.  Fields are Plf / Qlf / Ulf (lowercase 'l'),
        not PIf / QIf / UIf as written in the PDF.
        """
        T = "VarShuntElement"
        pv = self.db.CreateElement("VarShuntElement", name, n)
        f = lambda k, v: self._f(pv, T, name, k, v)

        want = PV_SHUNT_P_SIGN * p_rated * float(cfg.output_pu)
        f("Flag_LF", 1)                           # 1 = Power (Plf / Qlf)
        f("Flag_LoadType", 2)                     # 2 = P and Q constant
        f("Plf", want)
        f("Qlf", 0.0)                             # unity power factor
        f("Ulf", float(kv_ll))
        # ---- zero sequence: same convention as the household load ----
        f("Flag_Z0_Input", 1)                     # Z0/Z1 and R0/X0
        f("Z0_Z1", 1.0)
        f("R0_X0", rx_ratio(want, 0.0))
        f("Pneg", 0.0)
        f("Qneg", 0.0)
        pv.Update()
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
        sn = kva / 1000.0                          # MVA
        p_rated = min(kwp, kva) / 1000.0           # MW, inverter-limited
        n = self.db.GetCommonObject("Node", node)

        if cfg.use_varshunt:
            return self._create_pv_varshunt(name, node, n, p_rated, sn,
                                            cfg, phases, kv_ll)

        # ---- Converter (DCInfeeder) acting as a rooftop PV inverter ----
        T = "DCInfeeder"
        vv = std["vv"]
        pct = lambda v: 100.0 * v / v_base
        cv = self.db.CreateElement("DCInfeeder", name, n)
        f = lambda k, v: self._f(cv, T, name, k, v)

        # Plant / converter data
        f("Flag_DCtyp", 7)                         # photovoltaic system
        f("Flag_Connect", 1)                       # directly, no step-up trafo
        f("IslandOp", 0)                           # grid-following, no islanding
        f("Sn_Inverter", sn)
        f("Ur_Inverter", float(kv_ll))
        f("Umin_Inverter", 80.0)                   # %
        f("Umax_Inverter", 115.0)                  # % (AS/NZS 4777.2 trips at 265 V)
        f("DC_power", float(kwp) / 1000.0)         # installed array size, MW

        # Operating point: P and Q at the AC terminals, generation positive
        f("Flag_Lf", 1)                            # P and Q
        f("P", PV_P_SIGN * p_rated)
        f("Q", 0.0)
        f("fP", float(cfg.output_pu))
        f("fQ", 1.0)

        # Fault contribution: grid-following inverter, ~1.1 x rated current
        f("Flag_Converter", 0)                     # factor x rated current
        f("fSC", 1.1)

        # Limits
        f("Flag_LfLimit", 5)                       # limits on P and Q
        f("Flag_ChkType", 1)                       # warning on violation
        f("Flag_LimitType", 1)                     # limits act on the output
        f("Pmax", p_rated)
        f("Pmin", 0.0)
        q_lim = max(std["q_cap"], std["q_ind"]) * sn
        f("Qmax", q_lim)
        f("Qmin", -q_lim)
        f("cosphi_lim", round(math.sqrt(1 - std["q_ind"] ** 2), 3))
        f("Flag_CtrlPrior", 2)                     # reactive power priority

        # Grid-support controllers: AS/NZS 4777.2:2020 volt-watt and volt-var
        if vp_id is not None:
            f("Flag_Pctrl", 1)                     # Volt-watt (of Pn)
            f("Qctrl_U_P_ID", int(vp_id))
        if PV_VOLTVAR_MODE == "volt_var" and vq_id is not None:
            f("Flag_Qctrl", 3)                     # Volt-var
            f("Qctrl_U_Q_ID", int(vq_id))
            q_pu = max(std["q_cap"], std["q_ind"])
            f("Qctrl_Qmin", -q_pu)
            f("Qctrl_Qmax", q_pu)
        elif PV_VOLTVAR_MODE == "volt_pf":
            f("Flag_Qctrl", 1)                     # Volt-power factor
            f("Qctrl_cosphi_c", -round(math.sqrt(1 - std["q_cap"] ** 2), 4))
            f("Qctrl_U1_c", round(pct(vv[1]), 2))
            f("Qctrl_U2_c", round(pct(vv[0]), 2))
            f("Qctrl_cosphi", round(math.sqrt(1 - std["q_ind"] ** 2), 4))
            f("Qctrl_U1", round(pct(vv[2]), 2))
            f("Qctrl_U2", round(pct(vv[3]), 2))
        cv.Update()
        self.queue_phase(name, phases)

        if self.graphics:
            sx, sy = self._symbol_pos(node, side=+1)
            cv.CreateGraphic(sx, sy)
        ph_txt = "L" + "".join(str(k) for k in sorted(phases))
        print(f"    + PV conv   {name:<16} @ {node}  {ph_txt:<4} Sn={kva:.1f} kVA  "
              f"Pmax={p_rated * 1000:.2f} kW  (array {kwp:.1f} kWp)")
        return cv

    def apply_fields_sql(self) -> None:
        """
        Enforce every field written through _f() directly in the database and
        report any that did not stick through COM (SINCAL ignores some
        SetValue calls without an error).  Run after close().
        """
        if not self._intended:
            return
        con = sqlite3.connect(self.db_file)
        fixed: Counter = Counter()
        missing: list[str] = []
        try:
            for table, owner, col, value in self._intended:
                where = ('WHERE "Element_ID" IN (SELECT "Element_ID" FROM "Element" '
                         'WHERE "Name"=? AND "Flag_Variant"=1) AND "Flag_Variant"=1')
                row = con.execute(f'SELECT "{col}" FROM "{table}" {where}',
                                  (owner,)).fetchone()
                if row is None:
                    missing.append(f"{owner} ({table})")
                    continue
                cur = row[0]
                try:
                    same = cur is not None and math.isclose(
                        float(cur), float(value), rel_tol=1e-9, abs_tol=1e-12)
                except (TypeError, ValueError):
                    same = cur == value
                if not same:
                    con.execute(f'UPDATE "{table}" SET "{col}"=? {where}',
                                (value, owner))
                    fixed[(table, col)] += 1
            con.commit()
        finally:
            con.close()
        self._enable_input_flags()
        n = len(self._intended)
        nfix = sum(fixed.values())
        print(f"\n    Field check: {n - nfix - len(missing)} of {n} values stored "
              f"correctly through COM; {nfix} corrected directly in the database")
        if fixed:
            parts = ", ".join(f"{t}.{c} x{k}" for (t, c), k in sorted(fixed.items()))
            print(f"      corrected: {parts}")
        if missing:
            print(f"  [WARN] element rows not found for {len(missing)} write(s): "
                  + ", ".join(sorted(set(missing))[:6]))
        self._intended = []

    def _enable_input_flags(self) -> None:
        """
        Switch on the zero-sequence / power-control input sections via
        Element.Flag_Input, for every element that was given such data.
        """
        z0 = {o for (_t, o, c, _v) in self._intended if c.lower() == "flag_z0_input"}
        pc = {o for (_t, o, c, v) in self._intended
              if c.lower() in ("flag_pctrl", "flag_qctrl") and v}
        if not (z0 or pc):
            return
        con = sqlite3.connect(self.db_file)
        bad: list[str] = []
        try:
            cols = {r[1].lower() for r in con.execute('PRAGMA table_info("Element")')}
            if "flag_input" not in cols:
                print("  [WARN] Element.Flag_Input not found - zero-sequence / power "
                      "control data is NOT enabled")
                return
            for owners, bit in ((z0, INPUT_ZERO_SEQ), (pc, INPUT_POWER_CTRL)):
                for name in owners:
                    con.execute('UPDATE "Element" SET "Flag_Input"=COALESCE("Flag_Input",0)|? '
                                'WHERE "Name"=? AND "Flag_Variant"=1', (bit, name))
                    row = con.execute('SELECT "Flag_Input" FROM "Element" WHERE "Name"=? '
                                      'AND "Flag_Variant"=1', (name,)).fetchone()
                    if row is None or not (int(row[0]) & bit):
                        bad.append(name)
            con.commit()
        finally:
            con.close()
        print(f"    Input sections enabled: zero-sequence data on {len(z0)} elements, "
              f"power control on {len(pc)} converters")
        if bad:
            print("  [WARN] Flag_Input not set for: " + ", ".join(sorted(set(bad))[:8]))

    def verify_solve(self, method: str = "LF") -> bool:
        SimSrv = win32com.client.Dispatch(f"Sincal.SimulationSrv.{COM_VER}")
        Sim = SimSrv.GetSimulation()
        Sim.Database(self.conn)
        Sim.SetParameter("IDENTIFICATION", "Name")
        Sim.LoadDB(method)
        Sim.Start(method)
        status = Sim.StatusID
        text = Sim.StatusText
        print(f"\n  Verification ({method}): status={status} ({text})")

        names: dict[int, str] = {}
        try:
            con = sqlite3.connect(f"file:{self.db_file}?mode=ro", uri=True)
            names = dict(con.execute(
                'SELECT "Element_ID", "Name" FROM "Element" WHERE "Flag_Variant"=1'))
            con.close()
        except Exception:
            pass

        def objects(m) -> str:
            try:
                ids = [int(m.ObjectIdAt(i)) for i in range(1, int(m.CountObjectIds) + 1)]
            except Exception:
                return ""
            shown = ", ".join(f"{names.get(i, '?')}({i})" for i in ids[:10])
            return f"  -> {shown}" + (" ..." if len(ids) > 10 else "")

        excluded: list[str] = []
        kinds = {1: "STATUS", 2: "INFO", 3: "WARNING", 4: "ERROR", 6: "TASK"}
        msgs = Sim.Messages
        for i in range(1, msgs.Count + 1):
            m = msgs.Item(i)
            if m.Type in (3, 4) or status != 1101:
                print(f"    [{kinds.get(m.Type, m.Type):>7}] {m.MessageId:>6}  "
                      f"{m.Text}{objects(m)}")
            if m.MessageId == 1045:               # not considered in simulation
                try:
                    excluded += [names.get(int(m.ObjectIdAt(j)), "?")
                                 for j in range(1, int(m.CountObjectIds) + 1)]
                except Exception:
                    pass
        Sim = None
        SimSrv = None
        if excluded:
            print(f"\n  [WARN] {len(excluded)} element(s) are NOT part of the power "
                  f"flow (message 1045): {', '.join(excluded[:12])}"
                  f"{' ...' if len(excluded) > 12 else ''}\n"
                  f"         The solve 'succeeds' but those loads / generators have "
                  f"no effect on the results.")
        return status == 1101 and not excluded


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
    """linecode name -> sequence data per *linecode length unit* (+ 'units')."""
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
                   "R0": f("r0", r1 * LV_CABLE_R0_R1), "X0": f("x0", x1 * LV_CABLE_X0_X1),
                   "C0": f("c0", c1),
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
    """Pick a random cfg.pct % of candidate LV buses."""
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
    """
    Merge the two buses of every *closed* switch (name contains 'closed') into
    one node and drop the switch.  Exact for a zero-impedance closed switch;
    reduces the node count (the demo/student licence allows only 50 nodes).
    """
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
    """True for reactors that earth a neutral (bus1=x.4 / x.0, or series to earth)."""
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
                   legacy_ground_shunts: bool = False,
                   source_sk_mva: float = SOURCE_SK_MVA) -> None:
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

    # ---- auto-merge of closed switches when the node count would exceed 50 ----
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

    # ---- Build adjacency for BFS layout ----
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

    # ---- output location ----
    if out_dir is None:
        out_dir = os.path.dirname(os.path.abspath(master))
    if out_name is None:
        out_name = parser.circuit_name or "OpenDSS_Net"

    # -- build ---------------------------------------------------------------
    print(f"\n=== 2. Building PSS-SINCAL network '{out_name}'"
          f"{'' if not no_graphics else '  [graphics disabled]'}")
    b = SincalBuilder(out_dir, out_name, parser.base_freq,
                      graphics=not no_graphics)
    b.positions = positions
    b.create_database()
    b.read_schema()
    b.open_database()

    # voltage levels
    vl_names: dict[float, str] = {}
    for kv in sorted(set(round(v, 3) for v in bus_kv.values()), reverse=True):
        name = f"{kv}kV".replace(".", "_").replace("-", "m")
        vl_names[kv] = name
        b.ensure_voltage_level(name, kv)

    # nodes
    for bus, kv in bus_kv.items():
        b.ensure_node(bus, vl_names[round(kv, 3)])

    # infeeders
    if parser.source_bus:
        b.create_infeeder(
            name="Source",
            node=parser.source_bus,
            kv=parser.base_kv,
            v_pu=1.0,
            angle_deg=0.0,
            sk_mva=source_sk_mva,
            r_x=SOURCE_R_X,
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
            sk_mva=source_sk_mva,
            r_x=SOURCE_R_X,
        )

    # transformers
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
            uk = float(t.get("xhl", [TX_DEFAULTS["uk_pct"]])[0])
        except (ValueError, KeyError):
            uk = TX_DEFAULTS["uk_pct"]
        if uk < 1.0:
            uk *= 100.0
        ur = winding_r_pct(t)                 # from the .dss %r of both windings
        if ur is None or not 0.0 < ur < uk:
            ur = min(TX_DEFAULTS["ur_pct"], 0.5 * uk)
        vg, vg_label = vecgrp_from_conns(w1.get("conn"), w2.get("conn"))
        b.create_transformer(t["_name"], b1, b2, kv1, kv2, sn, uk, ur,
                             vecgrp=vg, vg_label=vg_label)

    # lines
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

    # switches
    for sw in parser.switches:
        b1 = strip_bus_node(sw.get("bus1", [""])[0])
        b2 = strip_bus_node(sw.get("bus2", [""])[0])
        b.create_switch_line(sw["_name"], b1, b2)

    # loads
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
            kvar = kw * math.tan(math.acos(DEFAULT_LOAD_PF))   # household pf
        conn = load_connection(ld) if phase_aware else None
        b.create_load(ld["_name"], bus, kw / 1000.0, kvar / 1000.0, conn)

    # PV converters (random subset of customer buses)
    if pv.pct > 0:
        mode_label = "VarShuntElement at unity PF" if pv.use_varshunt \
                     else "Converter (DCInfeeder, PV) with AS/NZS 4777.2 controllers"
        print(f"\n    --- PV converters ({mode_label}) ---")
        std = PV_REGIONS[pv.region]
        chosen = select_pv_buses(parser, bus_kv, pv, phase_aware)
        if chosen:
            lv_kv = bus_kv[chosen[0][0]]
            v_base = lv_kv * 1000.0 / math.sqrt(3)
            pc = lambda v: 100.0 * v / v_base
            vv = std["vv"]
            vp_id = vq_id = None
            # Only the DCInfeeder path supports the AS/NZS 4777.2 characteristics.
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

    # reactors (groundings -> shunt impedance)
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
    b.apply_fields_sql()
    b.apply_pending_phases_sql()
    if phase_aware or pv.pct > 0:
        b.report_phase_status(use_varshunt=pv.use_varshunt)
    if pv.pct > 0:
        b.report_converters_sql(use_varshunt=pv.use_varshunt)

    # -- verify -------------------------------------------------------------
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
    ap.add_argument("--outdir", default=None,
                    help="Directory where the .sin file is written")
    ap.add_argument("--name", default=None,
                    help="Name of the generated PSS-SINCAL network")
    ap.add_argument("--no-graphics", action="store_true",
                    help="Skip graphic creation (electrical model only)")
    ap.add_argument("--merge-closed-switches", action="store_true", default=None,
                    help="Merge buses joined by closed switches (cuts node "
                         "count for the 50-node demo licence).  Default: auto - "
                         "merge only when node count exceeds 50.")
    ap.add_argument("--no-merge-closed-switches", action="store_false",
                    dest="merge_closed_switches",
                    help="Disable auto-merging of closed switches")
    ap.add_argument("--legacy-ground-shunts", action="store_true",
                    help="Model neutral-earthing reactors as 3-phase shunt "
                         "impedances (old behaviour, NOT recommended)")
    ap.add_argument("--balanced", action="store_true",
                    help="Ignore OpenDSS phases: balanced loads, balanced "
                         "power flow (the original behaviour)")
    g = ap.add_argument_group("PV generation")
    g.add_argument("--pv-pct", type=float, default=0.0,
                   help="Percent of customer buses given a PV converter "
                        "(0 = none, default)")
    g.add_argument("--pv-seed", type=int, default=None,
                   help="Random seed for repeatable bus selection / sizing")
    g.add_argument("--pv-kva-list", default="5",
                   help="Comma-separated inverter sizes in kVA drawn at "
                        "random per site (default 5)")
    g.add_argument("--pv-kwp-ratio", type=float, default=1.3,
                   help="DC array kWp / inverter kVA (default 1.3)")
    g.add_argument("--pv-output", type=float, default=1.0,
                   help="fP: PV output as fraction of rated (default 1.0)")
    g.add_argument("--pv-all-lv-buses", action="store_true",
                   help="Candidates are all LV buses, not just buses with loads")
    g.add_argument("--pv-region", default="A",
                   help="AS/NZS 4777.2 region settings (default A)")
    g.add_argument("--pv-element", choices=["varshunt", "converter", "dcinfeeder"],
                   default="varshunt",
                   help="Element used to represent PV.  'varshunt' (default): "
                        "VarShuntElement, constant P/Q at unity power factor, "
                        "has a zero-sequence model so it works on a single "
                        "phase in LF_USYM.  'converter' (alias 'dcinfeeder'): "
                        "a SINCAL Converter set to plant type Photovoltaic "
                        "with inverter limits and AS/NZS 4777.2:2020 volt-var "
                        "and volt-watt control.  The converter has no "
                        "zero-sequence data; if SINCAL leaves it out of the "
                        "calculation, the post-build check lists it.")
    ap.add_argument("--source-sk-mva", type=float, default=SOURCE_SK_MVA,
                    help="Short-circuit power of the MV network feeder in MVA "
                         f"(default {SOURCE_SK_MVA:.0f})")
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
                       legacy_ground_shunts=args.legacy_ground_shunts,
                       source_sk_mva=args.source_sk_mva)
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise


# python .\build_network.py "C:\Users\Matt\Documents\python_uni_projects\archive\Renewable Energy\MV-associated-LV\data\LV\LV1_14bus\Master.dss" --outdir "./new_network/" --no-graphics
if __name__ == "__main__":
    main()