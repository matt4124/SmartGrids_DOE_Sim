
from pathlib import Path

from run_static_doe import (
    Constraints,
    compute_static_doe,
)

root = Path(__file__).resolve().parent
sin = root / "sincal_networks" / "LV1.sin"

print("Testing Matthew's static DOE...")
print("Network:", sin)

result = compute_static_doe(
    str(sin),
    constraints=Constraints(
        v_max_pu=1.10,
        v_min_pu=0.94,
        vuf_max_pct=2.0,
        line_loading_max_pct=None,
        trafo_loading_max_pct=100.0,
    ),
    max_export_kw=5.0,
    source_voltage_pu=1.03,
    max_iterations=30,
    verbose=True,
)

result.summary()

out = root / "static_doe_test_results"
out.mkdir(exist_ok=True)

result.limits.to_csv(out / "limits.csv")
result.history.to_csv(out / "history.csv", index=False)
result.final_metrics.to_csv(out / "final_metrics.csv", index=False)

print("\nResults saved to:", out)
