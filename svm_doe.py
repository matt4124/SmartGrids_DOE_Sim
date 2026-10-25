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

    def save(self, path: str) -> None:
        import joblib
        joblib.dump(dict(houses=self.houses, p_lo=self.p_lo, p_hi=self.p_hi, q_hi=self.q_hi,
                         cons=self.cons, metrics=self.metrics, include_q=self.include_q,
                         kernels=self.kernels, models=self.models, best_params=self.best_params,
                         X=self.X, Y=self.Y, Xt=self.Xt, Yt=self.Yt,
                         history=self.history, eval=self._eval,
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