"""
svm_doe.py
==========

Surrogate ("digital twin") of a PSS-SINCAL LV network built from five Support
Vector Regression models (SVMs), one per network-health output:

    max_v_pu               highest LV node voltage (pu, any phase)
    min_v_pu               lowest  LV node voltage (pu, any phase)
    max_vuf_pct            highest voltage unbalance factor (%)
    max_line_loading_pct   most loaded line (% of its rating)
    max_trafo_loading_pct  most loaded transformer (% of its rating)

Every SVM has the same inputs: the NET EXPORT of every household,
    P_i = PV output - household demand       (kW,   + = exporting)
    Q_i = net reactive injection             (kvar, + = injecting)
so a network with N houses gives 2N features.

Use from a Jupyter notebook (run_qsts.py and run_static_doe.py must be importable):

    import importlib, svm_doe as s; importlib.reload(s)
    from run_static_doe import Constraints

    cons = Constraints(v_max_pu=1.10, v_min_pu=0.94, vuf_max_pct=2.0,
                       line_loading_max_pct=100, trafo_loading_max_pct=100)
    sim  = s.SincalSimulator(TARGET_NETWORK, cons, source_voltage_pu=1.03,
                             line_rating_a=200.0)          # talks to SINCAL
    model = s.SVMDOE(sim, cons)

    model.train(n_initial=150, n_test=100, batch_size=50, max_iterations=8)
    model.evaluation()          # per-output error, safety margin, violation recall
    model.history               # one row per training iteration
    model.predict(P, Q)         # DataFrame of the 5 outputs for new net exports
    model.predict_violations(P, Q)      # amount over each limit (0 = within)
    model.is_feasible(P, Q)             # True where every constraint is predicted OK
    model.save("svm_doe.joblib")        # later: s.SVMDOE.load("svm_doe.joblib")

No SINCAL?  s.SyntheticSimulator(n_houses=20) is a cheap stand-in for a dry run.

Training loop (active learning)
-------------------------------
    1. label a fixed TEST set and an initial TRAIN set: set every house's net
       export, run SINCAL, read the 5 outputs;
    2. fit the 5 SVMs (hyper-parameters tuned by cross-validation);
    3. score them on the test set (RMSE, R2, violation recall ...);
    4. stop if every output is within its tolerance, else
    5. draw a pool of candidate operating points and label a batch of them -
       half close to the constraint limits (where accuracy matters), half
       filling the gaps in the input space - add them to the training set,
       and go to 2.

Notes on the design
-------------------
* The SVMs regress the raw maxima/minima (e.g. max voltage in pu), not the
  clipped "violation".  A violation is 0 almost everywhere, which is hard to
  learn and throws information away; the violation amount is derived from the
  prediction:  max(0, predicted - limit)  (or limit - predicted for the minimum
  voltage).  `safety_margin` in the evaluation table is the 99th-percentile
  under-prediction on the test set, to add when you need a conservative answer.
* Load and PV at a node only matter through their net injection, so net export
  is a sufficient input.  It is applied as: P >= 0 -> PV output, load 0;
  P < 0 -> load, PV 0.  Q is applied on the load element (negative load kvar
  = injection) so the converter's own Q limits do not interfere.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from run_static_doe import Constraints

METRICS = ("max_v_pu", "min_v_pu", "max_vuf_pct",
           "max_line_loading_pct", "max_trafo_loading_pct")

SHORT = {"max_v_pu": "vmax", "min_v_pu": "vmin", "max_vuf_pct": "vuf",
         "max_line_loading_pct": "line", "max_trafo_loading_pct": "trafo"}

DEFAULT_TOL = {"max_v_pu": 0.002, "min_v_pu": 0.002, "max_vuf_pct": 0.10,
               "max_line_loading_pct": 2.0, "max_trafo_loading_pct": 2.0}


def constraint_limits(cons: Constraints) -> dict:
    """metric -> (limit or None, 'upper' | 'lower')."""
    return {
        "max_v_pu": (cons.v_max_pu, "upper"),
        "min_v_pu": (cons.v_min_pu, "lower"),
        "max_vuf_pct": (cons.vuf_max_pct, "upper"),
        "max_line_loading_pct": (cons.line_loading_max_pct, "upper"),
        "max_trafo_loading_pct": (cons.trafo_loading_max_pct, "upper"),
    }


# ---------------------------------------------------------------------------
# Network side: houses + simulators
# ---------------------------------------------------------------------------

@dataclass
class House:
    name: str
    node: str
    phase: str
    load: str | None        # Load element name
    pv: str | None          # PV element name (VarShunt or converter)
    p_export_max: float     # kW   (0 if no PV)
    p_import_max: float     # kW   (0 if no load)
    q_max: float            # kvar (+-)


class SincalSimulator:
    """
    Labels operating points with SINCAL.  `simulate(P, Q)` sets every house's net
    export, solves the unbalanced load flow and returns the five outputs.

    max_export_kw : float | {pv: kW} | None  upper bound of a house's net export
                    (None -> the converter's Pmax, else 5 kW)
    max_import_kw : float | {load: kW}       lower bound (largest demand sampled)
    q_frac        : |Q| bound as a fraction of max(P bounds)  (0.33 ~ pf 0.95)
    """

    def __init__(self, sin_file: str, constraints: Constraints | None = None, *,
                 max_export_kw=None, max_import_kw=8.0, q_frac: float = 0.33,
                 source_voltage_pu: float | None = None, line_rating_a=None,
                 vuf_source: str = "auto", converter_control: str = "off"):
        import run_static_doe as d
        from run_qsts import SincalNetwork, list_loads, list_pv
        self._d = d
        self.sin_file = sin_file
        self.cons = constraints or Constraints()
        self.vuf_source = vuf_source
        self.net = SincalNetwork(sin_file)
        if source_voltage_pu is not None:
            d.set_source_voltage(sin_file, source_voltage_pu)
        if converter_control not in ("off", "as_built"):
            raise ValueError("converter_control must be 'off' or 'as_built'")
        d.set_converter_control(sin_file, converter_control == "as_built")
        self.info = d.read_network_info(sin_file, line_rating_a)
        self.line_rating_a = line_rating_a
        self.source_voltage_pu = source_voltage_pu

        def frame(x):
            return x if hasattr(x, "columns") else pd.DataFrame(x)
        ld, pv = frame(list_loads(sin_file)), frame(list_pv(sin_file))

        rating = {}
        try:
            con = d._ro(sin_file)
            for nm, pmax in con.execute(
                    "SELECT e.Name, c.Pmax FROM DCInfeeder c JOIN Element e "
                    "ON e.Element_ID=c.Element_ID AND e.Flag_Variant=1 WHERE c.Flag_Variant=1"):
                if pmax and pmax > 0:
                    rating[nm] = pmax * 1000.0
            con.close()
        except Exception:
            pass

        def bound(spec, key, default):
            if spec is None:
                return default
            return float(spec.get(key, default)) if isinstance(spec, dict) else float(spec)

        pv_at_node = {}
        for r in pv.to_dict("records"):
            pv_at_node.setdefault(r["node"], r["name"])
        used_pv, houses = set(), []
        for r in ld.to_dict("records"):
            pvn = pv_at_node.get(r["node"])
            if pvn in used_pv:
                pvn = None
            if pvn:
                used_pv.add(pvn)
            exp = 0.0
            if pvn:
                exp = bound(max_export_kw, pvn, rating.get(pvn, 5.0))
                if pvn in rating:
                    exp = min(exp, rating[pvn])
            imp = bound(max_import_kw, r["name"], 8.0)
            houses.append(House(r["name"], r["node"], str(r["phase"]), r["name"], pvn,
                                exp, imp, q_frac * max(exp, imp)))
        for r in pv.to_dict("records"):          # PVs on nodes without a load
            if r["name"] not in used_pv:
                exp = bound(max_export_kw, r["name"], rating.get(r["name"], 5.0))
                exp = min(exp, rating.get(r["name"], exp))
                houses.append(House(r["name"], r["node"], str(r["phase"]), None, r["name"],
                                    exp, 0.0, q_frac * exp))
        if not houses:
            raise RuntimeError("no loads or PV found in the network")
        self.houses = houses
        self.gen_nodes = {h.pv: h.node for h in houses if h.pv}

        # which outputs exist in this network (e.g. no line ratings -> no line loading)
        probe = self.simulate(np.zeros(len(houses)), np.zeros(len(houses)), _probe=True)
        if probe is None:
            raise RuntimeError("the power flow does not converge with every house at 0 kW")
        self.metrics = [m for m in METRICS if np.isfinite(probe.get(m, np.nan))]
        missing = [m for m in METRICS if m not in self.metrics]
        if missing:
            print(f"note: no data for {missing} in this network - those SVMs are skipped")

    def simulate(self, P, Q, _probe: bool = False):
        loads, pvs = {}, {}
        for h, p, q in zip(self.houses, P, Q):
            lk, lq, gk, gq = 0.0, 0.0, 0.0, 0.0
            if p >= 0:
                gk = p
            else:
                lk = -p
            if h.load:
                lq = -q                       # negative load kvar = injection
            else:
                gq = q
            if h.load:
                loads[h.load] = (lk, lq)
            if h.pv:
                pvs[h.pv] = (gk, gq)
        self.net.set_powers(loads=loads, pvs=pvs)
        if not self.net.solve(verbose=False):
            return None
        from run_qsts import read_node_voltages, read_branch_flows
        nodes, flows = read_node_voltages(self.sin_file), read_branch_flows(self.sin_file)
        if nodes is None or len(nodes) == 0:
            return None
        m, _ = self._d.evaluate_network(nodes, flows, self.info, self.cons,
                                        self.gen_nodes, self.vuf_source)
        return m

    def bounds(self):
        return (np.array([-h.p_import_max for h in self.houses]),
                np.array([h.p_export_max for h in self.houses]),
                np.array([h.q_max for h in self.houses]))


class SyntheticSimulator:
    """
    Cheap stand-in for SINCAL (a radial feeder with a simple non-linear voltage
    model) so the workflow can be tried without the licence.  Same interface.
    """

    def __init__(self, n_houses: int = 20, export_kw: float = 8.0, import_kw: float = 8.0,
                 q_frac: float = 0.33, seed: int = 0):
        self.rng = np.random.default_rng(seed)
        self.n = n_houses
        self.houses = [House(f"H{i}", f"N{i}", "L%d" % (i % 3 + 1), f"Ld{i}", f"PV{i}",
                             export_kw, import_kw, q_frac * max(export_kw, import_kw))
                       for i in range(n_houses)]
        self.phase = np.arange(n_houses) % 3
        self.metrics = list(METRICS)

    def bounds(self):
        return (np.full(self.n, -self.houses[0].p_import_max),
                np.full(self.n, self.houses[0].p_export_max),
                np.full(self.n, self.houses[0].q_max))

    def simulate(self, P, Q, _probe: bool = False):
        P, Q = np.asarray(P, float), np.asarray(Q, float)
        n = self.n
        pos = np.arange(1, n + 1)
        # voltage rise along the feeder: each house raises everything downstream of it
        up = np.minimum.outer(pos, pos)
        dv = (up @ (0.00045 * P + 0.00018 * Q)) / 10.0
        v = 1.03 + dv - 0.0000015 * np.abs(dv * 1e4) ** 2
        ph_p = np.array([P[self.phase == k].sum() for k in range(3)])
        vuf = 100 * (ph_p.max() - ph_p.min()) / (abs(ph_p.sum()) + 60.0)
        seg = np.abs(np.cumsum((P + 0.3j * Q)[::-1])[::-1])
        return dict(max_v_pu=float(v.max()), min_v_pu=float(v.min()),
                    max_vuf_pct=float(vuf), max_line_loading_pct=float(seg.max() / 0.4),
                    max_trafo_loading_pct=float(abs(P.sum() + 0.3j * Q.sum()) / 0.7))


# ---------------------------------------------------------------------------
# Sampling operating points
# ---------------------------------------------------------------------------

class Sampler:
    """
    Random net-export vectors, a mixture of
      uniform : every house independent in [-import, +export]
      common  : all houses exporting (or all importing) with a shared scale -
                the extreme states that independent sampling almost never hits
      sparse  : only a random subset of houses active
    """

    def __init__(self, p_lo, p_hi, q_hi, include_q: bool, rng,
                 weights=(0.4, 0.4, 0.2)):
        self.lo, self.hi, self.qh = p_lo, p_hi, q_hi
        self.include_q, self.rng = include_q, rng
        self.w = np.array(weights, float) / np.sum(weights)

    def sample(self, n: int):
        H, rng = len(self.lo), self.rng
        P = np.zeros((n, H))
        Q = np.zeros((n, H))
        mode = rng.choice(3, size=n, p=self.w)
        for k in range(n):
            if mode[k] == 0:
                P[k] = self.lo + (self.hi - self.lo) * rng.random(H)
            elif mode[k] == 1:
                s = rng.uniform(0.1, 1.0)
                end = self.hi if rng.random() < 0.6 else self.lo
                P[k] = end * s * rng.uniform(0.6, 1.0, H)
            else:
                f = rng.uniform(0.1, 0.6)
                on = rng.random(H) < f
                P[k] = on * (self.lo + (self.hi - self.lo) * rng.random(H))
            if self.include_q:
                Q[k] = self.qh * rng.uniform(-1, 1, H) * rng.random()
        return P, Q


# ---------------------------------------------------------------------------
# The five SVMs
# ---------------------------------------------------------------------------

class SVMDOE:
    def __init__(self, simulator, constraints: Constraints | None = None, *,
                 include_q: bool = True, kernels=("rbf",), random_state: int = 0):
        self.sim = simulator
        self.cons = constraints or getattr(simulator, "cons", None) or Constraints()
        self.limits = constraint_limits(self.cons)
        self.metrics = list(getattr(simulator, "metrics", METRICS))
        self.houses = list(simulator.houses)
        self.line_rating_a = getattr(simulator, "line_rating_a", None)
        self.vuf_source = getattr(simulator, "vuf_source", "auto")
        self.source_voltage_pu = getattr(simulator, "source_voltage_pu", None)
        self.p_lo, self.p_hi, self.q_hi = simulator.bounds()
        self.include_q = include_q
        self.kernels = tuple(kernels)
        self.rng = np.random.default_rng(random_state)
        self.random_state = random_state
        self.sampler = Sampler(self.p_lo, self.p_hi, self.q_hi, include_q, self.rng)
        self.H = len(self.houses)
        self.X = np.empty((0, self._nfeat))
        self.Y = np.empty((0, len(self.metrics)))
        self.Xt = np.empty((0, self._nfeat))
        self.Yt = np.empty((0, len(self.metrics)))
        self.models: dict = {}
        self.best_params: dict = {}
        self.history = pd.DataFrame()
        self._eval = pd.DataFrame()
        self.failed = 0

    # -- features ----------------------------------------------------------
    @property
    def _nfeat(self) -> int:
        return len(self.houses) * (2 if self.include_q else 1)

    def features(self, P, Q=None) -> np.ndarray:
        P = np.atleast_2d(np.asarray(P, float))
        if not self.include_q:
            return P
        Q = np.zeros_like(P) if Q is None else np.atleast_2d(np.asarray(Q, float))
        return np.hstack([P, Q])

    def feature_names(self) -> list:
        n = [f"P_{h.name}" for h in self.houses]
        return n + [f"Q_{h.name}" for h in self.houses] if self.include_q else n

    # -- labelling ----------------------------------------------------------
    def _label(self, P, Q, verbose=False):
        """Run the simulator on every row; -> (features, outputs) of the converged ones."""
        X, Y = [], []
        n = len(P)
        for k in range(n):
            m = self.sim.simulate(P[k], Q[k])
            ok = m is not None and all(np.isfinite(m.get(c, np.nan)) for c in self.metrics)
            if not ok:
                self.failed += 1
                continue
            X.append(self.features(P[k], Q[k])[0])
            Y.append([m[c] for c in self.metrics])
            if verbose and (k + 1) % 25 == 0:
                print(f"    labelled {k + 1}/{n}")
        if not X:
            return np.empty((0, self._nfeat)), np.empty((0, len(self.metrics)))
        return np.array(X), np.array(Y)

    # -- models ------------------------------------------------------------
    def _estimator(self, **params):
        from sklearn.compose import TransformedTargetRegressor
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from sklearn.svm import SVR
        pipe = Pipeline([("sc", StandardScaler()), ("svr", SVR(**params))])
        return TransformedTargetRegressor(regressor=pipe, transformer=StandardScaler())

    def _fit(self, tune: bool, tune_iter: int):
        from sklearn.model_selection import RandomizedSearchCV
        g0 = 1.0 / self._nfeat
        dist = {"regressor__svr__kernel": list(self.kernels),
                "regressor__svr__C": [1, 3, 10, 30, 100, 300, 1000],
                "regressor__svr__epsilon": [0.005, 0.01, 0.03, 0.1],
                "regressor__svr__gamma": [g0 * k for k in (0.03, 0.1, 0.3, 1, 3)]}
        cv = max(2, min(3, len(self.X) // 15))
        for j, m in enumerate(self.metrics):
            y = self.Y[:, j]
            if tune or m not in self.best_params:
                search = RandomizedSearchCV(self._estimator(), dist, n_iter=tune_iter, cv=cv,
                                            scoring="neg_root_mean_squared_error",
                                            random_state=self.random_state, n_jobs=1)
                search.fit(self.X, y)
                self.models[m] = search.best_estimator_
                self.best_params[m] = {k.split("__")[-1]: v
                                       for k, v in search.best_params_.items()}
            else:
                self.models[m] = self._estimator(**self.best_params[m]).fit(self.X, y)

    def predict_array(self, X) -> np.ndarray:
        return np.column_stack([self.models[m].predict(X) for m in self.metrics])

    # -- public prediction API ---------------------------------------------
    def predict(self, P, Q=None) -> pd.DataFrame:
        """The five outputs for net exports P (kW) and Q (kvar), one row per case."""
        if not self.models:
            raise RuntimeError("train() first")
        return pd.DataFrame(self.predict_array(self.features(P, Q)), columns=self.metrics)

    def _margin(self, m) -> float:
        if self._eval.empty or m not in self._eval.index:
            return 0.0
        return float(self._eval.loc[m, "safety_margin"])

    def predict_violations(self, P, Q=None, conservative: bool = False) -> pd.DataFrame:
        """Amount by which each output is beyond its limit (0 = within the limit)."""
        pred = self.predict(P, Q)
        out = pd.DataFrame(index=pred.index)
        for m in self.metrics:
            lim, sense = self.limits[m]
            if lim is None:
                continue
            v = pred[m] + (self._margin(m) if sense == "upper" else -self._margin(m)) \
                if conservative else pred[m]
            out[m + "_violation"] = np.maximum(0.0, v - lim if sense == "upper" else lim - v)
        return out

    def is_feasible(self, P, Q=None, conservative: bool = True) -> np.ndarray:
        """True where no constraint is predicted to be violated."""
        v = self.predict_violations(P, Q, conservative)
        return (v.to_numpy() <= 0).all(axis=1)

    # -- evaluation --------------------------------------------------------
    def _evaluate(self) -> pd.DataFrame:
        pred = self.predict_array(self.Xt)
        rows = {}
        for j, m in enumerate(self.metrics):
            t, p = self.Yt[:, j], pred[:, j]
            err = p - t
            lim, sense = self.limits[m]
            ss = float(np.sum((t - t.mean()) ** 2))
            r = dict(rmse=float(np.sqrt(np.mean(err ** 2))), mae=float(np.mean(np.abs(err))),
                     max_abs_err=float(np.max(np.abs(err))),
                     r2=1.0 - float(np.sum(err ** 2)) / ss if ss > 0 else np.nan)
            under = (t - p) if sense == "upper" else (p - t)       # >0 : prediction too optimistic
            r["safety_margin"] = float(max(0.0, np.quantile(under, 0.99)))
            if lim is not None:
                tv = t > lim if sense == "upper" else t < lim
                pv_ = p > lim if sense == "upper" else p < lim
                r.update(n_violating=int(tv.sum()), missed=int((tv & ~pv_).sum()),
                         false_alarm=int((~tv & pv_).sum()),
                         recall=float((tv & pv_).sum() / tv.sum()) if tv.sum() else np.nan)
            rows[m] = r
        return pd.DataFrame(rows).T

    def evaluation(self) -> pd.DataFrame:
        """Test-set errors per output from the latest training iteration.
        safety_margin : 99th-percentile optimistic error (same units as the output)
        missed / false_alarm / recall : classification of 'limit exceeded' at the limit"""
        return self._eval.copy()

    # -- active learning ---------------------------------------------------
    @staticmethod
    def _farthest(cand: np.ndarray, ref: np.ndarray, k: int) -> list:
        """Greedy max-min selection of k rows of `cand` far from `ref` and each other."""
        from scipy.spatial.distance import cdist
        if k <= 0 or len(cand) == 0:
            return []
        k = min(k, len(cand))
        d = cdist(cand, ref).min(axis=1) if len(ref) else np.full(len(cand), np.inf)
        chosen = []
        for _ in range(k):
            if d.max() > 0:
                i = int(np.argmax(d))
            else:                       # everything coincides: take any unchosen row
                i = next(c for c in range(len(cand)) if c not in chosen)
            chosen.append(i)
            d = np.minimum(d, cdist(cand, cand[i:i + 1])[:, 0])
            d[chosen] = -1.0
        return chosen

    def _select(self, batch_size: int, pool_size: int, boundary_frac: float):
        from sklearn.preprocessing import StandardScaler
        Pp, Qp = self.sampler.sample(pool_size)
        Xp = self.features(Pp, Qp)
        pred = self.predict_array(Xp)
        sc = StandardScaler().fit(self.X)
        Z, Zp = sc.transform(self.X), sc.transform(Xp)

        # closeness to a limit, in units of the model's own test error
        score = np.full(len(Xp), np.inf)
        for j, m in enumerate(self.metrics):
            lim, _ = self.limits[m]
            if lim is None:
                continue
            scale = max(float(self._eval.loc[m, "rmse"]), 0.25 * DEFAULT_TOL.get(m, 1e-3), 1e-9)
            score = np.minimum(score, np.abs(pred[:, j] - lim) / scale)
        n_b = int(round(batch_size * boundary_frac)) if np.isfinite(score).any() else 0
        picked = []
        if n_b:
            near = np.argsort(score)[:max(5 * n_b, n_b)]
            picked += [int(near[i]) for i in self._farthest(Zp[near], Z, n_b)]
        rest = np.array([i for i in range(len(Xp)) if i not in set(picked)])
        ref = np.vstack([Z, Zp[picked]]) if picked else Z
        picked += [int(rest[i]) for i in self._farthest(Zp[rest], ref, batch_size - len(picked))]
        return Pp[picked], Qp[picked]

    # -- the loop ----------------------------------------------------------
    def train(self, n_initial: int = 150, n_test: int = 100, batch_size: int = 50,
              max_iterations: int = 8, tolerances: dict | None = None,
              tune_every: int = 1, tune_iter: int = 20, pool_size: int = 2000,
              boundary_frac: float = 0.5, verbose: bool = True) -> "SVMDOE":
        """
        Label -> fit -> evaluate -> (label more where it matters) ... until every
        output's test RMSE is within `tolerances` (same units as the output:
        pu, %, %, %, %) or `max_iterations` is reached.  Calling train() again
        continues from the existing data.

        n_initial / n_test : first training set / fixed hold-out test set (SINCAL runs)
        batch_size         : SINCAL runs added per iteration
        tune_every         : re-tune hyper-parameters every n iterations (else refit only)
        boundary_frac      : share of each batch placed near the limits
        """
        say = print if verbose else (lambda *a, **k: None)
        tol = {**DEFAULT_TOL, **(tolerances or {})}
        t0 = time.time()
        if len(self.Xt) == 0:
            say(f"labelling {n_test} test points ...")
            self.Xt, self.Yt = self._label(*self.sampler.sample(n_test), verbose=verbose)
        if len(self.X) == 0:
            say(f"labelling {n_initial} initial training points ...")
            self.X, self.Y = self._label(*self.sampler.sample(n_initial), verbose=verbose)
        if len(self.X) < 20 or len(self.Xt) < 10:
            raise RuntimeError(f"too few converged runs (train {len(self.X)}, test {len(self.Xt)}); "
                               "check the network solves for random operating points")

        start_it = 0 if self.history.empty else int(self.history["iteration"].max())
        for it in range(start_it + 1, start_it + max_iterations + 1):
            ts = time.time()
            self._fit(tune=(it - start_it - 1) % max(1, tune_every) == 0, tune_iter=tune_iter)
            self._eval = self._evaluate()
            row = {"iteration": it, "n_train": len(self.X), "n_test": len(self.Xt)}
            for m in self.metrics:
                row[f"{m}_rmse"] = self._eval.loc[m, "rmse"]
                row[f"{m}_r2"] = self._eval.loc[m, "r2"]
                if "missed" in self._eval.columns and not pd.isna(self._eval.loc[m, "missed"]):
                    row[f"{m}_missed"] = self._eval.loc[m, "missed"]
            row["fit_s"] = time.time() - ts
            self.history = pd.concat([self.history, pd.DataFrame([row])], ignore_index=True)

            ok = {m: self._eval.loc[m, "rmse"] <= tol[m] for m in self.metrics}
            say(f"it {it:2d}: n_train {len(self.X):5d}  " + "  ".join(
                f"{SHORT[m]} "
                f"{self._eval.loc[m, 'rmse']:.3g}{'*' if ok[m] else ''}" for m in self.metrics))
            if all(ok.values()):
                say("all outputs within tolerance - stopping")
                break
            if it == start_it + max_iterations:
                say("reached max_iterations (* = within tolerance)")
                break
            Pn, Qn = self._select(batch_size, pool_size, boundary_frac)
            Xn, Yn = self._label(Pn, Qn)
            self.X, self.Y = np.vstack([self.X, Xn]), np.vstack([self.Y, Yn])
        say(f"done in {time.time() - t0:.0f} s, {len(self.X)} training + {len(self.Xt)} test runs"
            + (f", {self.failed} non-converged skipped" if self.failed else ""))
        return self

    # -- data / persistence --------------------------------------------------
    @property
    def dataset(self) -> pd.DataFrame:
        """Training data: net exports (features) and the five outputs."""
        df = pd.DataFrame(self.X, columns=self.feature_names())
        return pd.concat([df, pd.DataFrame(self.Y, columns=self.metrics)], axis=1)

    def input_map(self) -> pd.DataFrame:
        """
        Which load / PV every SVM input belongs to.  One row per input column, in
        the order the SVMs expect: input_index, name, quantity (P_kW or Q_kvar),
        household, the Load and PV element names, node, phase and the sampled range.
        """
        H = len(self.houses)
        rows = []
        for q_i, (qty, lo, hi) in enumerate((
                ("P_kW", self.p_lo, self.p_hi),
                ("Q_kvar", -np.asarray(self.q_hi), np.asarray(self.q_hi)))):
            if qty == "Q_kvar" and not self.include_q:
                continue
            for i, h in enumerate(self.houses):
                rows.append(dict(input_index=q_i * H + i, name=f"{qty[0]}_{h.name}", quantity=qty,
                                 household=h.name, load=h.load, pv=h.pv, node=h.node,
                                 phase=h.phase, range_low=float(lo[i]), range_high=float(hi[i])))
        return pd.DataFrame(rows).sort_values("input_index").reset_index(drop=True)

    def check_network(self, sin_file: str) -> bool:
        """Raise if `sin_file` does not contain exactly the loads / PVs this model was
        trained on (the input order is tied to them)."""
        from run_qsts import list_loads, list_pv
        def names(x):
            x = x if hasattr(x, "columns") else pd.DataFrame(x)
            return set(x["name"]) if len(x) else set()
        have_l, have_p = names(list_loads(sin_file)), names(list_pv(sin_file))
        need_l = {h.load for h in self.houses if h.load}
        need_p = {h.pv for h in self.houses if h.pv}
        bad = []
        if need_l - have_l: bad.append(f"loads missing in the network: {sorted(need_l - have_l)[:5]}")
        if have_l - need_l: bad.append(f"loads the model has no input for: {sorted(have_l - need_l)[:5]}")
        if need_p - have_p: bad.append(f"PV missing in the network: {sorted(need_p - have_p)[:5]}")
        if have_p - need_p: bad.append(f"PV the model has no input for: {sorted(have_p - need_p)[:5]}")
        if bad:
            raise ValueError("this SVM model was trained on a different network - " + "; ".join(bad))
        return True

    def export_weights(self, folder: str) -> dict:
        """
        Write the fitted SVM parameters as plain files (no pickle) into `folder`:
        inputs.csv (which load / PV every input column is) and, for every output,
        <output>.npz with the input scaler, support vectors, dual coefficients,
        intercept, gamma, epsilon, C and the target scaler.  With them
            y = target_mean + target_scale * ( sum_k dual_k * exp(-gamma*||z - sv_k||^2) + b )
        where z = (x - x_mean) / x_scale (RBF kernel).  Returns {output: path}.
        """
        import os
        os.makedirs(folder, exist_ok=True)
        self.input_map().to_csv(os.path.join(folder, "inputs.csv"), index=False)
        out = {}
        for m, est in self.models.items():
            pipe = est.regressor_
            sc, svr, ty = pipe.named_steps["sc"], pipe.named_steps["svr"], est.transformer_
            p = os.path.join(folder, f"{m}.npz")
            np.savez(p, x_mean=sc.mean_, x_scale=sc.scale_, support_vectors=svr.support_vectors_,
                     dual_coef=svr.dual_coef_.ravel(), intercept=svr.intercept_,
                     gamma=float(svr._gamma), epsilon=float(svr.epsilon), C=float(svr.C),
                     kernel=str(svr.kernel), y_mean=ty.mean_, y_scale=ty.scale_,
                     input_names=np.array(self.feature_names()))
            out[m] = p
        return out

    def save(self, path: str) -> None:
        """Save the model (all fitted SVMs, training data and the load <-> input map).
        Also writes <path without extension>_inputs.csv with the input map."""
        import joblib, os
        self.input_map().to_csv(os.path.splitext(path)[0] + "_inputs.csv", index=False)
        joblib.dump(dict(houses=self.houses, p_lo=self.p_lo, p_hi=self.p_hi, q_hi=self.q_hi,
                         cons=self.cons, metrics=self.metrics, include_q=self.include_q,
                         kernels=self.kernels, models=self.models, best_params=self.best_params,
                         X=self.X, Y=self.Y, Xt=self.Xt, Yt=self.Yt,
                         history=self.history, eval=self._eval,
                         line_rating_a=self.line_rating_a, vuf_source=self.vuf_source,
                         source_voltage_pu=self.source_voltage_pu,
                         random_state=self.random_state), path)

    @classmethod
    def load(cls, path: str, simulator=None) -> "SVMDOE":
        """Reload a saved model.  Prediction works without SINCAL; pass a simulator
        to keep training."""
        import joblib
        s = joblib.load(path)

        class _Stub:
            houses, metrics = s["houses"], s["metrics"]
            cons = s["cons"]

            @staticmethod
            def bounds():
                return s["p_lo"], s["p_hi"], s["q_hi"]

            @staticmethod
            def simulate(P, Q):
                raise RuntimeError("loaded without a simulator - pass simulator= to keep training")
        obj = cls(simulator or _Stub, s["cons"], include_q=s["include_q"],
                  kernels=s["kernels"], random_state=s["random_state"])
        obj.models, obj.best_params = s["models"], s["best_params"]
        obj.X, obj.Y, obj.Xt, obj.Yt = s["X"], s["Y"], s["Xt"], s["Yt"]
        obj.history, obj._eval = s["history"], s["eval"]
        obj.line_rating_a = s.get("line_rating_a")
        obj.vuf_source = s.get("vuf_source", "auto")
        obj.source_voltage_pu = s.get("source_voltage_pu")
        return obj

    # -- plots -------------------------------------------------------------
    def plot_history(self):
        """Test RMSE of every output against the training-set size."""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(1, len(self.metrics), figsize=(3.2 * len(self.metrics), 3), sharex=True)
        for a, m in zip(np.atleast_1d(ax), self.metrics):
            a.plot(self.history["n_train"], self.history[f"{m}_rmse"], "o-")
            a.axhline(DEFAULT_TOL[m], ls="--", c="grey")
            a.set_title(m, fontsize=9)
            a.set_xlabel("training runs")
        np.atleast_1d(ax)[0].set_ylabel("test RMSE")
        fig.tight_layout()
        return fig

    def plot_parity(self):
        """Predicted vs SINCAL value on the test set, with the constraint limit."""
        import matplotlib.pyplot as plt
        pred = self.predict_array(self.Xt)
        fig, ax = plt.subplots(1, len(self.metrics), figsize=(3.2 * len(self.metrics), 3.2))
        for j, (a, m) in enumerate(zip(np.atleast_1d(ax), self.metrics)):
            a.scatter(self.Yt[:, j], pred[:, j], s=8, alpha=0.6)
            lo, hi = min(self.Yt[:, j].min(), pred[:, j].min()), max(self.Yt[:, j].max(), pred[:, j].max())
            a.plot([lo, hi], [lo, hi], "k-", lw=0.8)
            lim = self.limits[m][0]
            if lim is not None:
                a.axvline(lim, c="r", ls="--", lw=0.8)
                a.axhline(lim, c="r", ls="--", lw=0.8)
            a.set_title(m, fontsize=9)
            a.set_xlabel("SINCAL")
        np.atleast_1d(ax)[0].set_ylabel("SVM")
        fig.tight_layout()
        return fig


# ---------------------------------------------------------------------------
# QSTS: SVM vs SINCAL on real household profiles
# ---------------------------------------------------------------------------

def qsts_compare(model: SVMDOE, sin_file: str, profile_folder: str, *, start=None, end=None,
                 step: int = 1, mapping: dict | None = None, include: tuple = ("demand",),
                 power_factor: float = 1.0, export_limits=None,
                 limit_basis: str = "net_export", run_sincal: bool = True,
                 verbose: bool = True) -> pd.DataFrame:
    """
    Quasi-static time series with the trained SVMs, optionally next to SINCAL.

    For every time step the household CSVs give each house's demand and PV; the
    net export P = PV - demand (and Q = P * tan(acos(power_factor))) is fed to
    the SVMs and, if run_sincal=True, to SINCAL (model.sim) as well, so both
    see identical inputs.  Same profile pairing as run_qsts.run_qsts.

    export_limits / limit_basis : as in run_qsts.run_qsts - clip each PV to its
        static export limit (a dict {pv_name: kW}, `doe.limits` or a DOEResult),
        so SVM and SINCAL can be compared on a DOE-limited day too.

    Returns one row per time step: svm_<output>, sincal_<output>, err_<output>
    (svm - sincal), in_range (False if any net export lies outside the range the
    SVMs were trained on - extrapolation, so treat those predictions with care).
    Use summarize_qsts(df, model) for the per-output error table.
    """
    from run_qsts import load_profiles, assign_profiles, common_index
    profiles = load_profiles(profile_folder, include)
    assign = assign_profiles(sin_file, profiles, mapping)

    limits = None
    if export_limits is not None:
        el = getattr(export_limits, "limits", export_limits)
        if hasattr(el, "to_dict"):
            col = "export_limit_kW" if "export_limit_kW" in el.columns else el.columns[0]
            limits = {str(k): float(v) for k, v in el[col].to_dict().items()}
        else:
            limits = {str(k): float(v) for k, v in dict(el).items()}

    hh = [assign[h.load] for h in model.houses if h.load]
    idx = common_index(profiles, set(hh))
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    idx = idx[::step]
    if len(idx) == 0:
        raise ValueError("no common timestamps across the assigned households")
    tanphi = math.tan(math.acos(power_factor))
    if verbose:
        print(f"{len(model.houses)} houses, {len(idx)} steps: {idx[0]} -> {idx[-1]}")

    Pm = np.zeros((len(idx), len(model.houses)))
    for k, t in enumerate(idx):
        for i, h in enumerate(model.houses):
            row = profiles[assign[h.load]].loc[t] if h.load else None
            load = float(row.kW_load) if row is not None else 0.0
            pv = max(float(row.kW_pv), 0.0) if (row is not None and h.pv) else 0.0
            if limits is not None and h.pv in limits:
                cap = limits[h.pv] + (load if limit_basis == "net_export" else 0.0)
                pv = min(pv, max(cap, 0.0))
            Pm[k, i] = pv - load
    Qm = Pm * tanphi

    in_range = ((Pm >= model.p_lo - 1e-9) & (Pm <= model.p_hi + 1e-9)).all(axis=1)
    if model.include_q:
        in_range &= (np.abs(Qm) <= model.q_hi + 1e-9).all(axis=1)
    pred = model.predict(Pm, Qm)
    out = pd.DataFrame({"time": idx, "in_range": in_range})
    for m in model.metrics:
        out[f"svm_{m}"] = pred[m].to_numpy()

    if run_sincal:
        t0 = time.time()
        rows = []
        for k in range(len(idx)):
            r = model.sim.simulate(Pm[k], Qm[k])
            rows.append({m: (r[m] if r is not None else np.nan) for m in model.metrics})
            if verbose and (k + 1) % 10 == 0:
                print(f"  SINCAL {k + 1}/{len(idx)}")
        ref = pd.DataFrame(rows)
        for m in model.metrics:
            out[f"sincal_{m}"] = ref[m].to_numpy()
            out[f"err_{m}"] = out[f"svm_{m}"] - out[f"sincal_{m}"]
        if verbose:
            print(f"SINCAL {time.time() - t0:.0f} s for {len(idx)} steps")
    if verbose and (~in_range).any():
        print(f"warning: {int((~in_range).sum())} step(s) lie outside the trained input range")
    return out


def summarize_qsts(df: pd.DataFrame, model: SVMDOE) -> pd.DataFrame:
    """Per-output error of qsts_compare(): RMSE / MAE / max error, plus whether the
    SVM and SINCAL agree on 'limit exceeded' (missed = SVM said fine, SINCAL violates)."""
    rows = {}
    for m in model.metrics:
        if f"err_{m}" not in df:
            continue
        ok = df[f"err_{m}"].notna()
        e, t, p = df.loc[ok, f"err_{m}"], df.loc[ok, f"sincal_{m}"], df.loc[ok, f"svm_{m}"]
        lim, sense = model.limits[m]
        r = dict(rmse=float(np.sqrt((e ** 2).mean())), mae=float(e.abs().mean()),
                 max_abs_err=float(e.abs().max()),
                 sincal_max=float(t.max()), svm_max=float(p.max()))
        if lim is not None:
            tv = t > lim if sense == "upper" else t < lim
            pv_ = p > lim if sense == "upper" else p < lim
            r.update(steps_violating=int(tv.sum()), missed=int((tv & ~pv_).sum()),
                     false_alarm=int((~tv & pv_).sum()))
        rows[m] = r
    return pd.DataFrame(rows).T


# ---------------------------------------------------------------------------
# SVM-guided QSTS: curtail PV with the SVMs, then verify with a real load flow
# ---------------------------------------------------------------------------

@dataclass
class SVMQSTSResult:
    """volts / flows : same long tables as run_qsts.run_qsts(return_flows=True)
    dispatch         : same table as run_qsts(return_dispatch=True) (limit_kW is NaN here)
    log              : one row per step - what the SVMs predicted before / after curtailing,
                       how many reductions it took, and the real SINCAL values afterwards"""
    volts: pd.DataFrame
    flows: pd.DataFrame
    dispatch: pd.DataFrame
    log: pd.DataFrame

    def as_tuple(self):
        return self.volts, self.flows, self.dispatch


def run_svm_qsts(model: SVMDOE, sin_file: str, profile_folder: str, *, start=None, end=None,
                 step: int = 1, mapping: dict | None = None, include: tuple = ("demand",),
                 power_factor: float = 1.0, conservative: bool = False,
                 line_rating_a=None, vuf_source: str | None = None,
                 sensitivity_kw: float = 0.25, min_step_kw: float = 0.1,
                 max_step_kw: float = 2.0, max_iterations: int = 500,
                 pv_scale: float = 1.0, load_scale: float = 1.0,
                 verbose: bool = True) -> SVMQSTSResult:
    """
    Quasi-static time series in which the trained SVMs decide the PV curtailment.
    Same profiles, time index, household pairing, power factor and load-flow
    settings as run_qsts.run_qsts, so it can be compared with a run of that
    function that applies the static DOE limits (export_limits=...).

    For every time step:
      1. read each house's demand (kW, kvar = kW*tan(acos(pf))) and available PV
         from the profiles; start with every PV at its available output, i.e.
         net export P_i = PV_i - demand_i, Q_i = P_i*tan(acos(pf));
      2. ask the five SVMs for the network outputs.  While one is predicted to be
         beyond its limit (worst one first):
           - lower each PV in turn by `sensitivity_kw` and see how much that SVM's
             output improves; the PV with the biggest improvement per kW is the
             one that contributes most,
           - reduce that PV by (excess / sensitivity), at least `min_step_kw` and
             at most `max_step_kw`,
           - predict again;
         an output that no PV can improve (e.g. a low-voltage violation caused by
         demand) is left alone and reported in the log as `unresolved`;
      3. run the real SINCAL load flow with the resulting PV set-points (demand
         unchanged) and store the results;
      4. next step.

    conservative : add each SVM's safety margin (model.evaluation()) to its
                   prediction, so the SVMs err on the side of curtailing
    line_rating_a, vuf_source : as in the static DOE; default to what the model
                   was trained with
    A PV is never set above the net-export range the SVMs were trained on (the
    converter's Pmax); the log flags steps where demand lay outside it.
    """
    import run_qsts as q
    import run_static_doe as d
    if not model.models:
        raise RuntimeError("train() or load a trained model first")
    model.check_network(sin_file)
    houses = model.houses
    H = len(houses)
    has_pv = np.array([h.pv is not None for h in houses])
    unpaired = [h.pv for h in houses if h.pv and not h.load]
    if unpaired and verbose:
        print(f"note: {len(unpaired)} PV have no load/profile and are held at 0 kW: {unpaired[:3]}")

    profiles = q.load_profiles(profile_folder, include)
    if pv_scale != 1.0 or load_scale != 1.0:      # what-if, same as run_qsts
        profiles = {h: df.assign(kW_pv=df["kW_pv"] * pv_scale, kW_load=df["kW_load"] * load_scale)
                    for h, df in profiles.items()}
    assign = q.assign_profiles(sin_file, profiles, mapping)
    idx = q.common_index(profiles, {assign[h.load] for h in houses if h.load})
    if start is not None:
        idx = idx[idx >= pd.Timestamp(start)]
    if end is not None:
        idx = idx[idx <= pd.Timestamp(end)]
    idx = idx[::step]
    if len(idx) == 0:
        raise ValueError("no common timestamps across the assigned households")
    if verbose:
        print(f"{H} houses, {len(idx)} steps: {idx[0]} -> {idx[-1]}")

    tanphi = math.tan(math.acos(power_factor))
    Ld = np.zeros((len(idx), H))
    PVa = np.zeros((len(idx), H))
    for i, h in enumerate(houses):
        if h.load:
            df = profiles[assign[h.load]].loc[idx]
            Ld[:, i] = df["kW_load"].to_numpy(float)
            if h.pv:
                PVa[:, i] = np.maximum(df["kW_pv"].to_numpy(float), 0.0)
    cap = np.where(has_pv, model.p_hi, 0.0)               # inverter rating = top of trained range

    info = d.read_network_info(sin_file, line_rating_a if line_rating_a is not None
                               else model.line_rating_a)
    vsrc = vuf_source or model.vuf_source
    net = q.SincalNetwork(sin_file)
    ms = list(model.metrics)
    mi = {m: j for j, m in enumerate(ms)}

    def feats(PVs):                                       # PVs (n, H) kW -> SVM inputs
        P = np.clip(PVs - cur_load, model.p_lo, model.p_hi)
        Qm = np.clip(P * tanphi, -model.q_hi, model.q_hi)
        return model.features(P, Qm)

    def excess(pred):                                     # {output: amount beyond limit}
        out = {}
        for m in ms:
            lim, sense = model.limits[m]
            if lim is None:
                continue
            v = pred[mi[m]]
            if conservative:
                v += model._margin(m) if sense == "upper" else -model._margin(m)
            ex = (v - lim) if sense == "upper" else (lim - v)
            if ex > 0:
                out[m] = ex
        return out

    volts_f, flow_f, disp_rows, logs = [], [], [], []
    t0 = time.time()
    for k, t in enumerate(idx):
        cur_load = Ld[k]
        pv = np.minimum(PVa[k], cap)                      # step 1: everything at maximum
        pre = model.predict_array(feats(pv[None, :]))[0]
        pred, unresolved, n_red, touched = pre, set(), 0, set()
        while n_red < max_iterations:                     # step 2: SVM-guided curtailment
            viol = {m: e for m, e in excess(pred).items() if m not in unresolved}
            if not viol:
                break
            m = max(viol, key=lambda x: viol[x] / max(abs(model.limits[x][0]), 1e-9))
            cand = np.where(pv > 1e-6)[0]
            if len(cand) == 0:
                unresolved.add(m)
                continue
            delta = np.minimum(sensitivity_kw, pv[cand])
            trial = np.tile(pv, (len(cand), 1))
            trial[np.arange(len(cand)), cand] -= delta
            pm = model.models[m].predict(feats(trial))
            gain = (pred[mi[m]] - pm) if model.limits[m][1] == "upper" else (pm - pred[mi[m]])
            slope = gain / delta                          # output improvement per kW curtailed
            b = int(np.argmax(slope))
            if slope[b] <= 1e-9:
                unresolved.add(m)
                continue
            i = cand[b]
            pv[i] -= min(pv[i], float(np.clip(viol[m] / slope[b], min_step_kw, max_step_kw)))
            touched.add(i)
            n_red += 1
            pred = model.predict_array(feats(pv[None, :]))[0]
        extrap = bool(((cur_load - pv) > -model.p_lo + 1e-9).any())

        ld = {h.load: (float(cur_load[i]), float(cur_load[i]) * tanphi)
              for i, h in enumerate(houses) if h.load}
        pvd = {h.pv: (float(pv[i]), float(pv[i]) * tanphi)
               for i, h in enumerate(houses) if h.pv}
        net.set_powers(loads=ld, pvs=pvd)                 # step 3: real steady state
        ok = net.solve(verbose=False)
        row = dict(time=t, converged=bool(ok), n_reductions=n_red, houses_curtailed=len(touched),
                   pv_available_kW=float(np.minimum(PVa[k], cap).sum()),
                   pv_dispatched_kW=float(pv.sum()),
                   svm_violating_before=",".join(m for m in excess(pre)),
                   unresolved=",".join(m for m in excess(pred)),
                   demand_outside_trained_range=extrap)
        for m in ms:
            row[f"svm_before_{m}"], row[f"svm_{m}"] = pre[mi[m]], pred[mi[m]]
        for i, h in enumerate(houses):
            if h.pv:
                disp_rows.append((t, h.pv, float(cur_load[i]), float(PVa[k, i]), float(pv[i]),
                                  float(PVa[k, i] - pv[i]), np.nan))
        if ok:
            nodes, flows = q.read_node_voltages(sin_file), q.read_branch_flows(sin_file)
            if nodes is not None and len(nodes):
                mm, vv = d.evaluate_network(nodes, flows, info, model.cons, {}, vsrc)
                for m in ms:
                    row[f"real_{m}"] = mm.get(m, np.nan)
                row["real_violations"] = ",".join(sorted({v.kind for v in vv}))
                nodes = nodes.copy()
                nodes.insert(0, "time", t)
                volts_f.append(nodes.drop(columns="Result_ID", errors="ignore"))
                flows = flows.copy()
                flows.insert(0, "time", t)
                flow_f.append(flows.drop(columns="Result_ID", errors="ignore"))
        else:
            print(f"  step {k} ({t}): no convergence / calc failed, skipped")
        logs.append(row)
        if verbose and (k + 1) % 10 == 0:
            print(f"  {k + 1}/{len(idx)}  ({time.time() - t0:.0f} s)")
    disp_cols = ["time", "pv", "load_kW", "pv_available_kW", "pv_dispatched_kW",
                 "curtailed_kW", "limit_kW"]
    res = SVMQSTSResult(
        volts=pd.concat(volts_f, ignore_index=True) if volts_f else pd.DataFrame(),
        flows=pd.concat(flow_f, ignore_index=True) if flow_f else pd.DataFrame(),
        dispatch=pd.DataFrame(disp_rows, columns=disp_cols),
        log=pd.DataFrame(logs))
    if verbose:
        lg = res.log
        print(f"done: {len(lg)} steps in {time.time() - t0:.0f} s; curtailed in "
              f"{int((lg['n_reductions'] > 0).sum())} steps; real violations in "
              f"{int((lg.get('real_violations', pd.Series(dtype=str)) != '').sum())} steps")
    return res


def plot_dispatch(result, max_pv: int = 8):
    """
    Plot what a QSTS run did with the PV: accepts run_svm_qsts() results,
    run_qsts.run_qsts(..., return_dispatch=True) tuples, or a dispatch DataFrame.

    Top panel    : total PV available vs dispatched (shaded = curtailed).
    Bottom panel : curtailment (kW) of the `max_pv` most curtailed PV systems.
    Returns the matplotlib Figure.
    """
    import matplotlib.pyplot as plt
    if hasattr(result, "dispatch"):
        disp = result.dispatch
    elif isinstance(result, tuple):
        disp = result[-1]
    else:
        disp = result
    tot = disp.groupby("time")[["pv_available_kW", "pv_dispatched_kW"]].sum()
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    a1.plot(tot.index, tot["pv_available_kW"], label="available")
    a1.plot(tot.index, tot["pv_dispatched_kW"], label="dispatched")
    a1.fill_between(tot.index, tot["pv_dispatched_kW"], tot["pv_available_kW"], alpha=0.3,
                    label="curtailed")
    a1.set_ylabel("total PV (kW)")
    a1.legend(loc="upper right")
    cur = disp.pivot_table(index="time", columns="pv", values="curtailed_kW", aggfunc="sum")
    for pv_name in cur.sum().sort_values(ascending=False).index[:max_pv]:
        a2.plot(cur.index, cur[pv_name], lw=1, label=pv_name)
    a2.set_ylabel("curtailed (kW)")
    if len(cur.columns):
        a2.legend(loc="upper right", fontsize=7, ncol=2)
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------
# One set of metrics for any QSTS result, so methods are compared like for like
# ---------------------------------------------------------------------------

def qsts_metrics(result, sin_file: str, constraints: Constraints, *, line_rating_a=None,
                 vuf_source: str = "auto", step_hours: float | None = None,
                 tol: float = 1e-4) -> pd.Series:
    """
    Metrics of a QSTS run - works on run_svm_qsts() results and on
    run_qsts.run_qsts(..., return_flows=True, return_dispatch=True) tuples
    (volts, flows, dispatch).

    line_losses_kWh        energy lost in the lines (loss_kW summed over lines x step length)
    trafo_losses_kWh       same for the transformer(s)
    energy_export_kWh      sum over houses of max(0, PV dispatched - demand) x step length
    pv_energy_kWh / pv_curtailed_kWh / curtailment_pct
    <kind>_viol_steps      time steps with at least one violation
    <kind>_viol_events     violating (node or element, step) pairs
      kinds: line_loading, trafo_loading, v_max, v_min, vuf; voltage_viol_steps = v_max or v_min
    max_* / min_*          worst value over the run
    The same evaluate_network() as the static DOE decides what a violation is; `tol` lets a
    value sitting exactly on its limit (e.g. 100.0000%) pass.
    """
    from dataclasses import replace
    import run_static_doe as d
    volts, flows, dispatch = (result.as_tuple() if hasattr(result, "as_tuple") else result)
    info = d.read_network_info(sin_file, line_rating_a)
    c = constraints
    ce = replace(c, **{k: (None if getattr(c, k) is None else getattr(c, k) * (1 + tol))
                       for k in ("v_max_pu", "vuf_max_pct", "line_loading_max_pct",
                                 "trafo_loading_max_pct")},
                 v_min_pu=None if c.v_min_pu is None else c.v_min_pu * (1 - tol))
    times = sorted(volts["time"].unique())
    if step_hours is None:
        step_hours = (float(np.median(np.diff(pd.to_datetime(times)).astype("timedelta64[s]")
                                      .astype(float))) / 3600.0) if len(times) > 1 else 1.0
    kinds = ("line_loading", "trafo_loading", "v_max", "v_min", "vuf")
    steps = {k: 0 for k in kinds}
    events = {k: 0 for k in kinds}
    volt_steps = 0
    worst = dict(max_v_pu=np.nan, min_v_pu=np.nan, max_vuf_pct=np.nan,
                 max_line_loading_pct=np.nan, max_trafo_loading_pct=np.nan)
    vg = dict(tuple(volts.groupby("time")))
    fg = dict(tuple(flows.groupby("time")))
    for t in times:
        if t not in fg:
            continue
        m, v = d.evaluate_network(vg[t].drop(columns="time"), fg[t].drop(columns="time"),
                                  info, ce, {}, vuf_source)
        got = {x.kind for x in v}
        for k in kinds:
            steps[k] += k in got
            events[k] += sum(x.kind == k for x in v)
        volt_steps += bool(got & {"v_max", "v_min"})
        for k in worst:
            val = m.get(k, np.nan)
            if np.isfinite(val):
                worst[k] = (min if k == "min_v_pu" else max)(worst[k], val) \
                    if np.isfinite(worst[k]) else val
    lines = {e[0] for e in info.edges if e[3] == "line"}
    trafos = {e[0] for e in info.edges if e[3] == "trafo"}
    out = {"n_steps": len(times), "step_hours": step_hours}
    if "loss_kW" in flows.columns:
        loss = flows.groupby(["time", "element"])["loss_kW"].first().reset_index()
        out["line_losses_kWh"] = float(loss[loss["element"].isin(lines)]["loss_kW"].sum() * step_hours)
        out["trafo_losses_kWh"] = float(loss[loss["element"].isin(trafos)]["loss_kW"].sum() * step_hours)
    else:
        out["line_losses_kWh"] = out["trafo_losses_kWh"] = np.nan
    if dispatch is not None and len(dispatch):
        net_kw = dispatch["pv_dispatched_kW"] - dispatch["load_kW"]
        out["energy_export_kWh"] = float(net_kw.clip(lower=0).sum() * step_hours)
        out["pv_energy_kWh"] = float(dispatch["pv_dispatched_kW"].sum() * step_hours)
        out["pv_curtailed_kWh"] = float((dispatch["pv_available_kW"] - dispatch["pv_dispatched_kW"]).sum()
                                        * step_hours)
        avail = float(dispatch["pv_available_kW"].sum() * step_hours)
        out["curtailment_pct"] = 100.0 * out["pv_curtailed_kWh"] / avail if avail > 0 else 0.0
    for k in ("line_loading", "trafo_loading"):
        out[f"{k}_viol_steps"], out[f"{k}_viol_events"] = steps[k], events[k]
    out["voltage_viol_steps"] = volt_steps
    for k in ("v_max", "v_min", "vuf"):
        out[f"{k}_viol_steps"], out[f"{k}_viol_events"] = steps[k], events[k]
    out.update(worst)
    return pd.Series(out)


def compare_qsts(results: dict, sin_file: str, constraints: Constraints, **kw) -> pd.DataFrame:
    """Table of qsts_metrics() with one column per method:
    compare_qsts({"static DOE": (volts, flows, dispatch), "SVM": svm_result}, sin, cons)"""
    return pd.DataFrame({name: qsts_metrics(r, sin_file, constraints, **kw)
                         for name, r in results.items()})



# ---------------------------------------------------------------------------
# Export limits (envelopes) from the SVMs, verified in SINCAL
# ---------------------------------------------------------------------------

def _margin_vec(model: SVMDOE, conservative: bool, extra: dict | None):
    """Per-output (limit, sign, margin): predicted value + sign*margin must stay within limit."""
    out = []
    for m in model.metrics:
        lim, sense = model.limits[m]
        if lim is None:
            out.append((None, 0, 0.0))
            continue
        mar = (model._margin(m) if conservative else 0.0) + float((extra or {}).get(m, 0.0))
        out.append((lim, +1 if sense == "upper" else -1, mar))
    return out


def svm_envelope(model: SVMDOE, *, conservative: bool = True, extra_margin: dict | None = None,
                 q_kvar_per_kw: float = 0.0, refine: bool = True, step_kw: float = 0.5,
                 min_step_kw: float = 0.05, seed: int = 0) -> pd.Series:
    """
    Largest static export limit (kW) per PV, found with the SVMs only (no SINCAL).
    Same snapshot as the static DOE: every household's net export equals its
    limit (zero demand).

      1. uniform stage - bisection on one common limit L (each PV capped at its
         own maximum) so that no constraint is predicted to be violated;
      2. refine stage  - round-robin: try to raise each PV by `step_kw` in turn,
         keep it if the joint state is still predicted feasible; when a round
         raises nothing, halve the step, down to `min_step_kw`.

    conservative : add the 99th-percentile test error (model.evaluation()
                   'safety_margin') to every prediction
    extra_margin : {output: amount} tightening on top, in the output's units
    """
    if not model.models:
        raise RuntimeError("train() first")
    H = len(model.houses)
    cap = model.p_hi.copy()
    has_pv = np.array([h.pv is not None for h in model.houses])
    cap[~has_pv] = 0.0
    spec = _margin_vec(model, conservative, extra_margin)

    def ok(P):                                    # P: (n, H) net export, kW
        P = np.atleast_2d(P)
        X = model.features(P, P * q_kvar_per_kw)
        pred = model.predict_array(X)
        good = np.ones(len(P), bool)
        for j, (lim, sgn, mar) in enumerate(spec):
            if lim is not None:
                good &= (pred[:, j] + sgn * mar) <= lim if sgn > 0 else (pred[:, j] - mar) >= lim
        return good

    lo, hi = 0.0, float(cap.max())
    if not ok(np.zeros(H))[0]:
        return pd.Series(np.zeros(int(has_pv.sum())), index=[h.pv for h in model.houses if h.pv],
                         name="export_limit_kW")
    if ok(np.minimum(hi, cap))[0]:
        lim_vec = cap.copy()
    else:
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if ok(np.minimum(mid, cap))[0] else (lo, mid)
        lim_vec = np.minimum(lo, cap)

    if refine:
        rng = np.random.default_rng(seed)
        step = step_kw
        while step >= min_step_kw:
            raised = False
            for i in rng.permutation(np.where(has_pv)[0]):
                if lim_vec[i] >= cap[i] - 1e-9:
                    continue
                trial = lim_vec.copy()
                trial[i] = min(cap[i], trial[i] + step)
                if ok(trial)[0]:
                    lim_vec, raised = trial, True
            if not raised:
                step /= 2.0
    return pd.Series(lim_vec[has_pv], index=[h.pv for h in model.houses if h.pv],
                     name="export_limit_kW")


def svm_doe_verified(model: SVMDOE, *, n_iterations: int = 6, conservative: bool = True,
                     q_kvar_per_kw: float = 0.0, learn: bool = True, retune: bool = False,
                     refine: bool = True, step_kw: float = 0.5, min_step_kw: float = 0.05,
                     stop_when_feasible: bool = True, verbose: bool = True):
    """
    For every iteration:
      1. the SVMs propose the highest export limit for every PV (svm_envelope);
      2. the limits are applied to the network and SINCAL actually simulates it
         (every house at net export = its limit, zero demand - the static DOE's snapshot);
      3. the true outputs are compared with the limits: if any constraint is
         violated, that output's margin is raised by the observed overshoot and
         the next iteration is more cautious;
      4. (learn=True) the simulated point is added to the training data and the
         SVMs are refitted, so they improve exactly where the limits sit.
    Stops at the first iteration SINCAL confirms feasible (stop_when_feasible).

    Returns (limits, history): `limits` is the final Series (kW per PV, with
    `.attrs['verified']` True if the last SINCAL run met every constraint) and
    `history` has one row per iteration (limits summary, SVM-predicted and SINCAL
    outputs, breaches, margins, training-set size).
    """
    say = print if verbose else (lambda *a, **k: None)
    extra = {m: 0.0 for m in model.metrics}
    rows, last = [], None
    pv_index = [i for i, h in enumerate(model.houses) if h.pv]
    for it in range(1, n_iterations + 1):
        t0 = time.time()
        limits = svm_envelope(model, conservative=conservative, extra_margin=extra,
                              q_kvar_per_kw=q_kvar_per_kw, refine=refine,
                              step_kw=step_kw, min_step_kw=min_step_kw)
        P = np.zeros(len(model.houses))
        for i, g in zip(pv_index, limits.index):
            P[i] = limits[g]
        Q = P * q_kvar_per_kw
        pred = model.predict(P, Q).iloc[0]
        true = model.sim.simulate(P, Q)
        if true is None:
            say(f"it {it}: SINCAL did not converge at the proposed limits - tightening")
            extra = {m: v + 0.5 * DEFAULT_TOL[m] * 5 for m, v in extra.items()}
            rows.append(dict(iteration=it, total_kW=float(limits.sum()), converged=False))
            continue
        breach = {}
        for m in model.metrics:
            lim, sense = model.limits[m]
            if lim is None:
                continue
            over = true[m] - lim if sense == "upper" else lim - true[m]
            if over > 1e-9:
                breach[m] = over
        row = dict(iteration=it, total_kW=float(limits.sum()), min_kW=float(limits.min()),
                   mean_kW=float(limits.mean()), max_kW=float(limits.max()), converged=True,
                   n_train=len(model.X), violated=",".join(breach) or "-", seconds=0.0)
        for m in model.metrics:
            row[f"svm_{m}"], row[f"sincal_{m}"] = float(pred[m]), float(true[m])
            row[f"margin_{m}"] = extra[m]
        row["seconds"] = time.time() - t0
        rows.append(row)
        last = (limits, not breach)
        say(f"it {it}: total {limits.sum():7.2f} kW (min {limits.min():.2f}, max {limits.max():.2f})  "
            + ("SINCAL: all constraints met" if not breach else
               "SINCAL violations: " + ", ".join(f"{SHORT[m]} +{v:.3g}" for m, v in breach.items())))
        if not breach and stop_when_feasible:
            break
        for m, v in breach.items():                      # be more cautious next time
            extra[m] += v * 1.2 + 0.1 * DEFAULT_TOL[m]
        if learn:
            model.X = np.vstack([model.X, model.features(P, Q)])
            model.Y = np.vstack([model.Y, [true[m] for m in model.metrics]])
            model._fit(tune=retune, tune_iter=20)
            model._eval = model._evaluate()
    if last is None:
        raise RuntimeError("SINCAL never converged at the proposed limits")
    limits, verified = last
    limits = limits.copy()
    limits.attrs["verified"] = bool(verified)
    if not verified:
        say("warning: the last limits still violate a constraint in SINCAL - raise n_iterations "
            "or train the SVMs on more data")
    return limits, pd.DataFrame(rows)