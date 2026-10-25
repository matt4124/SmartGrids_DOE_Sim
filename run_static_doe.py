"""
run_static_doe.py
=================

Static dynamic-operating-envelope (DOE) calculation for a PSS-SINCAL LV network:
an export limit (kW) for every PV generator, found by repeatedly simulating a
conservative snapshot and growing the limits until a network constraint binds.

Use from a Jupyter notebook (run_qsts.py must be importable):

    from run_static_doe import (Constraints, LoadScenario, compute_static_doe)

    doe = compute_static_doe(
        TARGET_NETWORK,
        Constraints(v_max_pu=1.10, v_min_pu=0.94, vuf_max_pct=2.0,
                    line_loading_max_pct=100, trafo_loading_max_pct=100),
        max_export_kw=5.0,
        source_voltage_pu=1.03,
    )
    doe.limits          # DataFrame: export limit (kW) per PV + what limited it
    doe.history         # one row per iteration
    doe.final_metrics   # network health at the final limits, per scenario

Method
------
1. Loads are set by a LoadScenario (default: zero load and zero kvar).
2. Every PV starts at `start_kw`; this must be feasible (checked).
3. Run a steady-state unbalanced power flow (LF_USYM) per scenario.
4. Check constraints: voltage band (all nodes, all phases), voltage unbalance
   factor |V2|/|V1|, line loading (I / rating), transformer loading (S / Sn).
5. No violation  -> raise every still-active generator by its own step.
   Violation     -> discard the trial; the generators *responsible* (those
   downstream of the violating node / branch) halve their step.  A generator
   whose step falls below `min_step_kw` (or that reaches `max_export_kw`) is
   frozen.  Limits are only ever taken from simulated feasible states, so the
   returned limits are verified.

Load vs export
--------------
A PV and its household load share a node and a phase, so the network only sees
the NET injection.  With limit_on="net_export" (default) each PV is simulated
at limit + its scenario load, i.e. net export equals the limit.  Then only the
reactive draw of the loads differs between scenarios, and zero load is the
conservative case.  limit_on="generation" limits the gross PV output instead.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from run_qsts import (SincalNetwork, assign_profiles, db_path_from_sin,
                      list_loads, list_pv, load_profiles, read_branch_flows,
                      read_node_voltages)


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@dataclass
class Constraints:
    """Network limits.  Set an entry to None to switch that check off."""
    v_max_pu: float | None = 1.10          # upper voltage (pu of rated, ph-N)
    v_min_pu: float | None = 0.94          # lower voltage
    vuf_max_pct: float | None = 2.0        # voltage unbalance factor |V2|/|V1|, %
    line_loading_max_pct: float | None = 100.0    # I / line rating
    trafo_loading_max_pct: float | None = 100.0   # S / transformer Sn


@dataclass
class LoadScenario:
    """
    How the household loads are set while the export limits are searched.

    kW        fixed kW at every load (0 = no load).  Ignored if profile_pct set.
    pf        power factor (lagging, i.e. the load absorbs kvar) for kvar.
    profile_pct  take this percentile (0-100) of each household's demand from
              the CSV profiles, inside `hours` (start, end) of the day.
              Needs profile_folder in compute_static_doe().
    """
    name: str = "zero_load"
    kW: float | None = 0.0
    pf: float = 0.95
    profile_pct: float | None = None
    hours: tuple = (10.0, 15.0)


ZERO_LOAD = LoadScenario("zero_load", kW=0.0)


@dataclass
class DOEResult:
    limits: pd.DataFrame            # per PV: node, phase, export_limit_kW, limited_by
    history: pd.DataFrame           # per iteration: accepted?, metrics, violations
    trial_limits: pd.DataFrame      # per iteration: the trial limit of every PV (kW)
    final_metrics: pd.DataFrame     # network health at the final limits, per scenario
    baseline_violations: list       # violations present with PV off (ignored)
    converged: bool
    iterations: int
    notes: list = field(default_factory=list)

    def summary(self) -> None:
        print(f"Static DOE: {len(self.limits)} generators, {self.iterations} iterations, "
              f"{'converged' if self.converged else 'NOT converged (max iterations)'}")
        if len(self.limits):
            s = self.limits["export_limit_kW"]
            print(f"  export limit kW: min {s.min():.2f}  mean {s.mean():.2f}  "
                  f"max {s.max():.2f}  total {s.sum():.1f}")
            print(self.limits["limited_by"].value_counts().to_string())
        print(self.final_metrics.to_string(index=False))
        for n in self.notes:
            print("  note:", n)


# ---------------------------------------------------------------------------
# Network information (topology, ratings) from the .sin database
# ---------------------------------------------------------------------------

@dataclass
class NetworkInfo:
    source_node: str
    edges: list                     # (element, node_a, node_b, kind)  kind: line | trafo
    line_rating_a: dict             # element -> A
    trafo_sn_kva: dict              # element -> kVA
    subtree: dict                   # node -> set(nodes at/below it, radial from source)
    edge_child: dict                # element -> node on its downstream side


def _ro(sin_file: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db_path_from_sin(sin_file)}?mode=ro", uri=True)


def read_network_info(sin_file: str, line_rating_a=None) -> NetworkInfo:
    """Topology and ratings.  line_rating_a: float or {line: A} overriding the database."""
    con = _ro(sin_file)
    try:
        row = con.execute(
            "SELECT n.Name FROM Infeeder i JOIN Terminal t ON t.Element_ID=i.Element_ID "
            "AND t.TerminalNo=1 AND t.Flag_Variant=1 JOIN Node n ON n.Node_ID=t.Node_ID "
            "WHERE i.Flag_Variant=1 ORDER BY i.Element_ID").fetchone()
        if row is None:
            raise RuntimeError("no Infeeder (source) found in the network")
        source = row[0]

        edges, ratings, sn = [], {}, {}
        q = ("SELECT e.Name, na.Name, nb.Name, {extra} FROM {tab} x "
             "JOIN Element e ON e.Element_ID=x.Element_ID AND e.Flag_Variant=1 "
             "JOIN Terminal ta ON ta.Element_ID=e.Element_ID AND ta.TerminalNo=1 AND ta.Flag_Variant=1 "
             "JOIN Terminal tb ON tb.Element_ID=e.Element_ID AND tb.TerminalNo=2 AND tb.Flag_Variant=1 "
             "JOIN Node na ON na.Node_ID=ta.Node_ID JOIN Node nb ON nb.Node_ID=tb.Node_ID "
             "WHERE x.Flag_Variant=1")
        for name, a, b, ith in con.execute(q.format(extra="x.Ith", tab="Line")):
            edges.append((name, a, b, "line"))
            if ith and ith > 0:
                ratings[name] = ith * 1000.0
        for name, a, b, sn_mva in con.execute(q.format(extra="x.Sn", tab="TwoWindingTransformer")):
            edges.append((name, a, b, "trafo"))
            sn[name] = (sn_mva or 0.0) * 1000.0
    finally:
        con.close()

    if line_rating_a is not None:
        for name, _a, _b, kind in edges:
            if kind != "line":
                continue
            r = line_rating_a.get(name) if isinstance(line_rating_a, dict) else line_rating_a
            if r:
                ratings[name] = float(r)

    # radial tree from the source (BFS); loops/closed rings keep the first path found
    adj: dict[str, list] = {}
    for name, a, b, _k in edges:
        adj.setdefault(a, []).append((b, name))
        adj.setdefault(b, []).append((a, name))
    depth, parent, order = {source: 0}, {}, [source]
    for u in order:
        for v, name in adj.get(u, []):
            if v not in depth:
                depth[v] = depth[u] + 1
                parent[v] = (u, name)
                order.append(v)
    subtree = {n: {n} for n in order}
    for v in reversed(order):
        if v in parent:
            subtree[parent[v][0]] |= subtree[v]
    edge_child = {}
    for name, a, b, _k in edges:
        da, db = depth.get(a), depth.get(b)
        if da is None or db is None:
            continue
        edge_child[name] = b if db > da else a
    return NetworkInfo(source, edges, ratings, sn, subtree, edge_child)


def set_converter_control(sin_file: str, enabled: bool) -> int:
    """
    Switch the converters' built-in volt-watt / volt-var control off (enabled=False)
    so they inject exactly the P (and Q) the DOE sets.  Returns converters changed.
    """
    if enabled:
        return 0
    con = sqlite3.connect(db_path_from_sin(sin_file))
    try:
        cur = con.execute('UPDATE "DCInfeeder" SET "Flag_Pctrl"=0, "Flag_Qctrl"=0, "Q"=0 '
                          'WHERE "Flag_Variant"=1')
        con.commit()
        return cur.rowcount
    except sqlite3.Error:
        return 0
    finally:
        con.close()


def set_source_voltage(sin_file: str, pu: float) -> None:
    """Set every network feeder's voltage (Infeeder.u, in % of nominal)."""
    con = sqlite3.connect(db_path_from_sin(sin_file))
    try:
        con.execute('UPDATE "Infeeder" SET "u"=? WHERE "Flag_Variant"=1', (pu * 100.0,))
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Load scenarios
# ---------------------------------------------------------------------------

def resolve_load_scenarios(sin_file: str, scenarios, profile_folder=None,
                           mapping=None, include=("demand",)) -> dict:
    """-> {scenario name: {load name: (kW, kvar)}}"""
    loads = list_loads(sin_file)
    names = list(loads["name"]) if hasattr(loads, "columns") else [r["name"] for r in loads]
    profiles = assign = None
    out = {}
    for sc in scenarios:
        vals = {}
        for ld in names:
            if sc.profile_pct is not None:
                if profile_folder is None:
                    raise ValueError(f"scenario '{sc.name}' uses profile_pct: pass profile_folder")
                if profiles is None:
                    profiles = load_profiles(profile_folder, include)
                    assign = assign_profiles(sin_file, profiles, mapping)
                s = profiles[assign[ld]]["kW_load"].dropna()
                h = s.index.hour + s.index.minute / 60.0
                s = s[(h >= sc.hours[0]) & (h < sc.hours[1])]
                kw = float(np.percentile(s, sc.profile_pct)) if len(s) else 0.0
            else:
                kw = float(sc.kW or 0.0)
            vals[ld] = (kw, kw * math.tan(math.acos(sc.pf)))
        out[sc.name] = vals
    return out


# ---------------------------------------------------------------------------
# Metrics and constraint checks
# ---------------------------------------------------------------------------

def compute_vuf_pct(nodes: pd.DataFrame, source: str = "auto") -> pd.Series:
    """
    Voltage unbalance factor |V2|/|V1| in % per node (index = node row).
    Uses voltage magnitudes and angles; falls back to SINCAL's own unbalance
    value (VUF_sincal_pct) where angles are missing or all equal.
    """
    sincal = nodes["VUF_sincal_pct"] if "VUF_sincal_pct" in nodes else pd.Series(np.nan, nodes.index)
    if source == "sincal" or not {"ang1_deg", "ang2_deg", "ang3_deg"} <= set(nodes.columns):
        return sincal.astype(float)
    a = np.exp(2j * np.pi / 3)
    v = [nodes[f"U{i}_kV"].to_numpy(float) * np.exp(1j * np.deg2rad(nodes[f"ang{i}_deg"].to_numpy(float)))
         for i in (1, 2, 3)]
    v1 = (v[0] + a * v[1] + a * a * v[2]) / 3
    v2 = (v[0] + a * a * v[1] + a * v[2]) / 3
    with np.errstate(divide="ignore", invalid="ignore"):
        calc = pd.Series(100 * np.abs(v2) / np.abs(v1), nodes.index)
    ang = nodes[["ang1_deg", "ang2_deg", "ang3_deg"]].to_numpy(float)
    usable = (np.nanmax(ang, axis=1) - np.nanmin(ang, axis=1) > 1.0) & np.isfinite(calc)
    return calc.where(usable, sincal).astype(float)


@dataclass
class Violation:
    kind: str           # v_max | v_min | vuf | line_loading | trafo_loading | no_convergence
    where: str
    value: float
    limit: float | None
    gens: frozenset     # generators responsible

    @property
    def key(self):
        return (self.kind, self.where)


def evaluate_network(nodes: pd.DataFrame, flows: pd.DataFrame, info: NetworkInfo,
                     cons: Constraints, gen_nodes: dict, vuf_source: str = "auto"):
    """-> (metrics dict, list[Violation])"""
    all_gens = frozenset(gen_nodes)

    def gens_below(node):
        sub = info.subtree.get(node, {node})
        return frozenset(g for g, n in gen_nodes.items() if n in sub)

    metrics = dict(max_v_pu=np.nan, min_v_pu=np.nan, max_vuf_pct=np.nan,
                   max_line_loading_pct=np.nan, max_trafo_loading_pct=np.nan)
    viol: list[Violation] = []

    # --- voltages / unbalance on LV nodes (rated < 1 kV line-line) ---
    n = nodes.copy()
    n = n[n["U1_Un"] > 0]
    rated_ll = (n["U1_kV"] / (n["U1_Un"] / 100.0)) * math.sqrt(3)
    n = n[rated_ll < 1.0]
    if len(n):
        pu = n[["U1_Un", "U2_Un", "U3_Un"]].to_numpy(float) / 100.0
        vmax, vmin = pu.max(axis=1), pu.min(axis=1)
        vuf = compute_vuf_pct(n, vuf_source).to_numpy(float)
        metrics.update(max_v_pu=float(vmax.max()), min_v_pu=float(vmin.min()),
                       max_vuf_pct=float(np.nanmax(vuf)) if np.isfinite(vuf).any() else np.nan)
        names = n["node"].to_numpy()
        if cons.v_max_pu is not None:
            for i in np.where(vmax > cons.v_max_pu)[0]:
                viol.append(Violation("v_max", names[i], vmax[i], cons.v_max_pu, gens_below(names[i])))
        if cons.v_min_pu is not None:
            for i in np.where(vmin < cons.v_min_pu)[0]:
                viol.append(Violation("v_min", names[i], vmin[i], cons.v_min_pu, gens_below(names[i])))
        if cons.vuf_max_pct is not None:
            for i in np.where(np.nan_to_num(vuf) > cons.vuf_max_pct)[0]:
                viol.append(Violation("vuf", names[i], vuf[i], cons.vuf_max_pct, gens_below(names[i])))

    # --- branch loading ---
    if len(flows):
        f = flows.copy()
        cur = f[f["element"].isin(info.line_rating_a)].copy()
        if len(cur):
            cur["load"] = 100.0 * cur["I_max_A"] / cur["element"].map(info.line_rating_a)
            worst = cur.groupby("element")["load"].max()
            metrics["max_line_loading_pct"] = float(worst.max())
            if cons.line_loading_max_pct is not None:
                for el, val in worst[worst > cons.line_loading_max_pct].items():
                    viol.append(Violation("line_loading", el, float(val), cons.line_loading_max_pct,
                                          gens_below(info.edge_child.get(el, info.source_node))))
        tx = f[f["element"].isin(info.trafo_sn_kva)].copy()
        if len(tx):
            tx["load"] = 100.0 * tx["S_kVA"].abs() / tx["element"].map(info.trafo_sn_kva)
            # SINCAL's own loading is current based (I / In per phase); with single-phase
            # PV/loads one phase can be overloaded while total S looks fine, so take the worse.
            if "loading_pct" in tx.columns:
                tx["load"] = np.fmax(tx["load"], tx["loading_pct"].abs())
            worst = tx.groupby("element")["load"].max()
            metrics["max_trafo_loading_pct"] = float(worst.max())
            if cons.trafo_loading_max_pct is not None:
                for el, val in worst[worst > cons.trafo_loading_max_pct].items():
                    viol.append(Violation("trafo_loading", el, float(val), cons.trafo_loading_max_pct,
                                          gens_below(info.edge_child.get(el, info.source_node))))
    return metrics, viol


# ---------------------------------------------------------------------------
# The DOE search
# ---------------------------------------------------------------------------

class StaticDOE:
    def __init__(self, sin_file, cons, scenario_loads, max_export_kw, min_export_kw,
                 start_kw, initial_step_kw, min_step_kw, max_iterations, limit_on,
                 pv_kvar_per_kw, line_rating_a, vuf_source, verbose):
        self.sin_file, self.cons = sin_file, cons
        self.scen = scenario_loads
        self.start_kw, self.min_export = start_kw, min_export_kw
        self.step0, self.min_step = initial_step_kw, min_step_kw
        self.max_iter, self.limit_on = max_iterations, limit_on
        self.kvar_per_kw, self.vuf_source, self.verbose = pv_kvar_per_kw, vuf_source, verbose
        self.net = SincalNetwork(sin_file)
        self.info = read_network_info(sin_file, line_rating_a)

        pv = list_pv(sin_file)
        pv = pv if hasattr(pv, "columns") else pd.DataFrame(pv)
        self.pv = pv.set_index("name")
        self.gens = list(self.pv.index)
        self.gen_node = self.pv["node"].to_dict()
        ld = list_loads(sin_file)
        ld = ld if hasattr(ld, "columns") else pd.DataFrame(ld)
        self.loads_at = ld.groupby("node")["name"].apply(list).to_dict()
        # inverter ratings (converter Pmax, kW): SINCAL clamps P above this
        self.rating = {}
        try:
            con = _ro(sin_file)
            for nm, pmax in con.execute(
                    "SELECT e.Name, c.Pmax FROM DCInfeeder c JOIN Element e "
                    "ON e.Element_ID=c.Element_ID AND e.Flag_Variant=1 WHERE c.Flag_Variant=1"):
                if pmax and pmax > 0:
                    self.rating[nm] = pmax * 1000.0
            con.close()
        except sqlite3.Error:
            pass
        self.notes0 = []
        self.cap = {}
        for g in self.gens:
            user = (max_export_kw.get(g) if isinstance(max_export_kw, dict) else max_export_kw)
            rated = self.rating.get(g)
            cap = float(user) if user is not None else (rated if rated else 5.0)
            if rated and cap > rated + 1e-9:
                self.notes0.append(f"{g}: cap {cap:g} kW lowered to the converter's Pmax "
                                   f"{rated:g} kW (SINCAL clamps P above Pmax)")
                cap = rated
            self.cap[g] = cap

    # -- one steady-state run -------------------------------------------------
    def _simulate(self, loads: dict, pvs: dict):
        self.net.set_powers(loads=loads, pvs=pvs)
        if not self.net.solve(verbose=False):
            return None, None
        return read_node_voltages(self.sin_file), read_branch_flows(self.sin_file)

    def _pv_setpoints(self, limits: dict, loads: dict) -> dict:
        out = {}
        for g, lim in limits.items():
            gen = lim
            if self.limit_on == "net_export":
                gen += sum(loads[l][0] for l in self.loads_at.get(self.gen_node[g], []) if l in loads)
            out[g] = (gen, gen * self.kvar_per_kw)
        return out

    def evaluate(self, limits: dict):
        """Run every scenario; -> (feasible, violations, per-scenario metrics list)"""
        all_v, rows = [], []
        for sname, loads in self.scen.items():
            nodes, flows = self._simulate(loads, self._pv_setpoints(limits, loads))
            if nodes is None or nodes.empty:
                rows.append(dict(scenario=sname, converged=False))
                all_v.append(Violation("no_convergence", sname, np.nan, None, frozenset(self.gens)))
                continue
            m, v = evaluate_network(nodes, flows, self.info, self.cons, self.gen_node, self.vuf_source)
            # does the simulated net export actually reach the transformer?
            dp = np.nan
            tx = flows[flows["element"].isin(self.info.trafo_sn_kva)] if len(flows) else flows
            if len(tx) and "P_kW" in tx:
                sp = self._pv_setpoints(limits, loads)
                expected = sum(g for g, _ in sp.values()) - sum(kw for kw, _ in loads.values())
                if abs(expected) > 1.0:
                    dp = 100.0 * float(tx["P_kW"].abs().max()) / abs(expected)
            rows.append(dict(scenario=sname, converged=True, delivered_pct=dp, **m))
            all_v += v
        return all_v, rows

    # -- search --------------------------------------------------------------
    def run(self) -> DOEResult:
        say = print if self.verbose else (lambda *a, **k: None)
        notes = list(self.notes0[:3])
        if len(self.notes0) > 3:
            notes.append(f"... and {len(self.notes0) - 3} more generators capped at their Pmax")

        # baseline: violations that exist with every PV off are not the PV's fault
        zero = {g: 0.0 for g in self.gens}
        base_v, _ = self.evaluate(zero)
        base = {v.key: v for v in base_v}
        baseline = set(base)
        if baseline:
            notes.append(f"{len(baseline)} constraint(s) already violated with PV off (they only "
                         "count again if PV makes them worse): "
                         + ", ".join(f"{k}@{w}={base[(k, w)].value:.3g}" for k, w in sorted(baseline)[:6]))
        if any(k == "no_convergence" for k, _ in baseline):
            raise RuntimeError("power flow does not converge even with all PV at 0 kW")

        margin = dict(v_max=0.002, v_min=0.002, vuf=0.1, line_loading=1.0, trafo_loading=1.0)

        def worse(v):
            b = base[v.key].value
            m = margin.get(v.kind, 0.0)
            return v.value < b - m if v.kind == "v_min" else v.value > b + m

        def real(viol):
            return [v for v in viol if v.key not in base or worse(v)]

        start = {g: min(self.start_kw, self.cap[g]) for g in self.gens}
        v0, rows0 = self.evaluate(start)
        v0 = real(v0)
        if v0:
            notes.append(f"start_kw={self.start_kw} already violates "
                         f"{sorted({v.kind for v in v0})}; those generators start at 0 kW")
            blamed = set().union(*[v.gens for v in v0])
            start = {g: (0.0 if g in blamed else lim) for g, lim in start.items()}
            v0, rows0 = self.evaluate(start)
            v0 = real(v0)
            if v0:
                start = dict(zero)
                v0, rows0 = self.evaluate(start)

        best, best_rows = dict(start), rows0
        step = {g: self.step0 for g in self.gens}
        active = {g for g in self.gens if best[g] < self.cap[g]}
        reason = {g: "max_export" if best[g] >= self.cap[g] else "" for g in self.gens}
        hist, trials = [], []
        converged = False
        it = 0
        for it in range(1, self.max_iter + 1):
            if not active:
                converged = True
                break
            trial = dict(best)
            for g in active:
                trial[g] = min(self.cap[g], best[g] + step[g])
            changed = {g for g in active if trial[g] > best[g]}
            if not changed:
                converged = True
                break
            viol, rows = self.evaluate(trial)
            viol = real(viol)
            kinds = sorted({v.kind for v in viol})
            rdf = pd.DataFrame(rows).reindex(columns=["max_v_pu", "min_v_pu", "max_vuf_pct",
                                                     "max_line_loading_pct", "max_trafo_loading_pct", "delivered_pct"])
            hist.append(dict(iteration=it, accepted=not viol, n_violations=len(viol),
                             violated=",".join(kinds), n_active=len(active),
                             total_limit_kW=sum(trial.values()),
                             **{k: rdf[k].max() for k in ("max_v_pu", "max_vuf_pct",
                                                          "max_line_loading_pct",
                                                          "max_trafo_loading_pct")},
                             min_v_pu=rdf["min_v_pu"].min(), delivered_pct=rdf["delivered_pct"].min()))
            trials.append({"iteration": it, **trial})
            if not viol:
                best, best_rows = trial, rows
                for g in list(active):
                    if best[g] >= self.cap[g]:
                        active.discard(g)
                        reason[g] = "max_export"
                say(f"  it {it:>3}: OK    total {sum(best.values()):7.1f} kW  active {len(active)}")
            else:
                blamed = set().union(*[v.gens for v in viol]) & changed
                if not blamed:
                    blamed = changed
                for g in blamed:
                    kinds_g = sorted({v.kind for v in viol if g in v.gens}) or kinds
                    reason[g] = "+".join(kinds_g)
                    step[g] /= 2.0
                    if step[g] < self.min_step:
                        active.discard(g)
                say(f"  it {it:>3}: VIOL  {kinds}  {len(blamed)} generators slow down  active {len(active)}")
        else:
            converged = not active

        lim = pd.DataFrame({
            "node": [self.gen_node[g] for g in self.gens],
            "phase": [self.pv.loc[g, "phase"] for g in self.gens],
            "export_limit_kW": [max(best[g], self.min_export) for g in self.gens],
            "limited_by": [reason[g] or "step_limit" for g in self.gens],
        }, index=pd.Index(self.gens, name="pv"))
        if self.min_export > 0 and (lim["export_limit_kW"] > pd.Series(best)).any():
            notes.append(f"min_export_kw={self.min_export} raises some limits above the "
                         "verified level (guaranteed minimum, not network-verified)")
        final = pd.DataFrame(best_rows)
        if "delivered_pct" in final:
            dpv = final["delivered_pct"].dropna()
            if len(dpv) and ((dpv < 90.0) | (dpv > 110.0)).any():
                bad = dpv[(dpv < 90.0) | (dpv > 110.0)].iloc[0]
                notes.append(
                    f"WARNING: the transformer carries {bad:.0f} % of the export the search "
                    "set (expected ~100 %). SINCAL is not applying the setpoints as given "
                    "(inverter Pmax clamp, converter controls, or other injections), so the "
                    "limits are NOT verified. Check the converter settings.")
        return DOEResult(lim, pd.DataFrame(hist), pd.DataFrame(trials), final,
                         sorted(baseline), converged, it, notes)


def compute_static_doe(sin_file: str, constraints: Constraints | None = None, *,
                       scenarios=None, profile_folder=None, mapping=None,
                       max_export_kw=None, min_export_kw: float = 0.0,
                       start_kw: float = 0.5, initial_step_kw: float = 1.0,
                       min_step_kw: float = 0.05, max_iterations: int = 80,
                       limit_on: str = "net_export", pv_kvar_per_kw: float = 0.0,
                       line_rating_a=None, source_voltage_pu: float | None = None,
                       vuf_source: str = "auto", converter_control: str = "off",
                       verbose: bool = True) -> DOEResult:
    """
    Compute static export limits for every PV in the network.

    constraints      Constraints(...)  (defaults: 0.94-1.10 pu, VUF 2 %, 100 % loading)
    scenarios        list[LoadScenario]; default [ZERO_LOAD].  The result satisfies
                     the constraints in EVERY scenario (the most restrictive wins).
    profile_folder, mapping   only for scenarios with profile_pct
    max_export_kw    cap per generator (float, or {pv name: kW}).  Default: each
                     converter's own Pmax, else 5 kW.  Never above the converter's Pmax.
    min_export_kw    guaranteed floor reported in the limits (not network-verified)
    start_kw         starting level for every PV (must be safely feasible)
    initial_step_kw, min_step_kw   per-generator step; halves when blamed
    limit_on         "net_export" (PV = limit + household load) or "generation"
    pv_kvar_per_kw   inverter reactive power per kW generated (negative absorbs)
    line_rating_a    float or {line: A} to override/supply line ratings
    source_voltage_pu  set the MV feeder voltage (e.g. 1.03) before the search
    vuf_source       "auto" (compute from angles, else SINCAL) | "sincal"
    converter_control  "off" (default): switch the converters' volt-watt / volt-var
                     control off so each PV injects exactly the setpoint (+ the Q from
                     pv_kvar_per_kw);  "as_built": leave the controllers as built.
                     "off" is written to the database.
    """
    if limit_on not in ("net_export", "generation"):
        raise ValueError("limit_on must be 'net_export' or 'generation'")
    cons = constraints or Constraints()
    scenarios = scenarios or [ZERO_LOAD]
    scen_loads = resolve_load_scenarios(sin_file, scenarios, profile_folder, mapping)
    pre_notes = []
    if source_voltage_pu is not None:
        set_source_voltage(sin_file, source_voltage_pu)
    if converter_control not in ("off", "as_built"):
        raise ValueError("converter_control must be 'off' or 'as_built'")
    n_off = set_converter_control(sin_file, converter_control == "as_built")
    if n_off:
        pre_notes.append(f"volt-watt / volt-var control switched off on {n_off} converter(s)")
    doe = StaticDOE(sin_file, cons, scen_loads, max_export_kw, min_export_kw, start_kw,
                    initial_step_kw, min_step_kw, max_iterations, limit_on,
                    pv_kvar_per_kw, line_rating_a, vuf_source, verbose)
    if cons.line_loading_max_pct is not None and not doe.info.line_rating_a:
        raise ValueError("no line ratings available: rebuild the network with the updated "
                         "converter (reads normamps), pass line_rating_a=..., or set "
                         "line_loading_max_pct=None")
    res = doe.run()
    res.notes = pre_notes + res.notes
    return res