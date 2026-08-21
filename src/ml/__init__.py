"""Framework-agnostic ML for the Testing Lab: features, models, validation, comparison.

WHY THIS LIVES IN src/ AND NOT IN THE BACKEND
    Same reason as src/piotroski.py and src/screen.py — it is library code with no web framework in
    it, importable by a CLI, a notebook, or a service. The Testing Lab that will call it runs as a
    SEPARATE container (backend image 220 MB vs ~700 MB with xgboost, scipy and friends, and a
    training run must not compete with live API requests on this box).

LAZY IMPORTS ARE LOAD-BEARING
    Every model wrapper imports its heavy dependency inside the function that needs it, so this
    package can be imported — and validation.py and feature_engineer.py fully exercised — in an
    environment with only numpy and pandas. That is what lets the main backend and CI keep working
    without xgboost, scikit-learn or statsmodels installed at all.

PORTED, NOT WRITTEN
    validation.py, feature_engineer.py and model_comparison.py came across from the
    Special-Sprinkle-Sauce repo with no logic changes — only the logger namespace. The model
    wrappers were changed on the way: see their docstrings for what and why.
"""
