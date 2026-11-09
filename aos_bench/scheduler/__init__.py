"""Fixed scheduling policies as baseline arms for AOS-Bench.

See :mod:`aos_bench.scheduler.fixed_policy` for the agent adapter and
``docs/experiments/fixed_policy_baselines.md`` for the experimental design.
"""

__all__ = ["FixedPolicyAgent"]


def __getattr__(name):  # pragma: no cover - lazy re-export
    if name == "FixedPolicyAgent":
        from aos_bench.scheduler.fixed_policy import FixedPolicyAgent

        return FixedPolicyAgent
    raise AttributeError(name)
