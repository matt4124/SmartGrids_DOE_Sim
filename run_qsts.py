"""
run_qsts.py

Quasi-static time series for SINCAL networks produced by
opendss_to_sincal.py.

Each load is assigned one CSV profile.

Columns expected:

interval_start,demand,solar,...

demand > 0  (kW consumption)
solar  < 0  (kW export)

Load element:
    P = demand

VarShuntElement:
    Plf = solar

Results:
    timestep CSV
    node voltages
    loadings

Requires:
    pywin32
    pandas
    PSS-SINCAL 22.5
"""

import os
import glob
import random
import pandas as pd
import win32com.client

COM_VER = "28"


class SincalQSTS:

    def __init__(self, sin_file):

        self.sin_file = os.path.abspath(sin_file)

        self.simsrv = win32com.client.Dispatch(
            f"Sincal.SimulationSrv.{COM_VER}"
        )

        self.sim = self.simsrv.GetSimulation()

        self.app = win32com.client.Dispatch(
            f"SIASincal.Application.{COM_VER}"
        )

        self.doc = self.app.OpenDocument(self.sin_file)

        if self.doc is None:
            raise RuntimeError(
                f"Failed to open {self.sin_file}"
            )

        self.db = self.sim.DB_EL()

        conn = self.doc.Database()
        self.sim.Database(conn)

    # ---------------------------------------------------
    # object lookup
    # ---------------------------------------------------

    def get_table(self, table):

        tab = self.db.GetRowObj(table)
        tab.Open()

        rows = []

        while not tab.EOF:
            rows.append(tab)
            tab.MoveNext()

        return rows

    # ---------------------------------------------------
    # create profile allocation
    # ---------------------------------------------------

    def assign_profiles(
        self,
        profile_folder,
        seed=1
    ):

        csvs = sorted(
            glob.glob(
                os.path.join(profile_folder, "*.csv")
            )
        )

        if not csvs:
            raise RuntimeError(
                "No csv profiles found"
            )

        loads = {}

        tab = self.db.GetRowObj("Load")
        tab.Open()

        rng = random.Random(seed)

        while not tab.EOF:

            load_name = tab.GetValue("Name")

            profile = rng.choice(csvs)

            loads[load_name] = profile

            tab.MoveNext()

        return loads

    # ---------------------------------------------------
    # helper
    # ---------------------------------------------------

    def build_profile_dataset(
        self,
        profile_assignment
    ):

        profiles = {}

        for fn in set(profile_assignment.values()):

            profiles[fn] = pd.read_csv(
                fn,
                parse_dates=["interval_start"]
            )

        return profiles

    # ---------------------------------------------------
    # update loads
    # ---------------------------------------------------

    def set_load_power(
        self,
        load_name,
        demand_kw
    ):

        obj = self.db.GetCommonObject(
            "Load",
            load_name
        )

        obj.SetValue(
            "P",
            float(demand_kw) / 1000.0
        )

        obj.Update()

    # ---------------------------------------------------
    # update PV
    # ---------------------------------------------------

    def set_pv_power(
        self,
        pv_name,
        solar_kw
    ):

        obj = self.db.GetCommonObject(
            "VarShuntElement",
            pv_name
        )

        obj.SetValue(
            "Plf",
            float(solar_kw) / 1000.0
        )

        obj.Update()

    # ---------------------------------------------------
    # run LF
    # ---------------------------------------------------

    def solve(self):

        self.sim.LoadDB("LF_USYM")
        self.sim.Start("LF_USYM")

        return self.sim.StatusID

    # ---------------------------------------------------
    # collect node voltages
    # ---------------------------------------------------

    def collect_voltages(self):

        results = []

        lfnode = self.sim.GetObj("Node")

        res = lfnode.Result()

        while res.MoveNext():

            try:

                results.append(
                    {
                        "node":
                            res.Item("Name"),

                        "u1":
                            res.Item("u1"),

                        "u2":
                            res.Item("u2"),

                        "u3":
                            res.Item("u3"),
                    }
                )

            except:

                pass

        return pd.DataFrame(results)

    # ---------------------------------------------------
    # QSTS
    # ---------------------------------------------------

    def run(
        self,
        profile_assignment
    ):

        profiles = self.build_profile_dataset(
            profile_assignment
        )

        first_profile = next(
            iter(profiles.values())
        )

        n_steps = len(first_profile)

        all_results = []

        for step in range(n_steps):

            timestamp = (
                first_profile.iloc[step]
                ["interval_start"]
            )

            # ---------------------------
            # update all customers
            # ---------------------------

            for load_name, csvfile in (
                profile_assignment.items()
            ):

                row = profiles[csvfile].iloc[step]

                demand_kw = row["demand"]

                solar_kw = row["solar"]

                self.set_load_power(
                    load_name,
                    demand_kw
                )

                pv_name = f"PV_{load_name}"

                try:
                    self.set_pv_power(
                        pv_name,
                        solar_kw
                    )
                except:
                    pass

            # ---------------------------
            # solve
            # ---------------------------

            status = self.solve()

            if status != 1101:

                print(
                    f"{timestamp} failed"
                )

                continue

            # ---------------------------
            # save results
            # ---------------------------

            volts = self.collect_voltages()

            volts["time"] = timestamp

            all_results.append(volts)

        return pd.concat(
            all_results,
            ignore_index=True
        )