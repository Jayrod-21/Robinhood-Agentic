"""Errors the ML wrappers raise."""

from __future__ import annotations


class UntrainedModelError(RuntimeError):
    """predict() on a model that was never fitted.

    RAISED, not answered with a placeholder. Every wrapper here used to log a warning and return
    0.5 — a real, defensible probability, which is what made it dangerous: the validator averages
    whatever it gets, so a model that never trained scored an accuracy near 50% and read as "no
    better than chance". The truth is "this never ran", and the Testing Lab exists to tell those
    apart. src/ml/validation.py counts these instead of averaging them.
    """
