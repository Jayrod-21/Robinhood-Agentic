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
    validation.py and feature_engineer.py came across from the Special-Sprinkle-Sauce repo with no
    logic changes — only the logger namespace. Everything else was changed on the way in, and each
    file's docstring says what and why. The through-line is one defect: the original library
    answered "we could not measure this" with a number. A failed prediction scored as a wrong one,
    an untrained model predicted 0.5, ARIMA returned 0.5 from three separate failure paths, the
    orchestrator substituted 0.5 for any model it could not run, and the leaderboard filled a
    missing accuracy with 0.5 before computing the spread the caller reads as real disagreement.
    0.5 is not "unknown" — it is a confident coin flip that drags a composite toward neutral while
    SHRINKING the panel's dispersion, so the tickers with the least information came out looking
    like the tickers with the most model agreement. Nothing here fabricates a score; a model that
    cannot answer abstains by name, and the metrics are taken over what actually ran.

    random_forest_model.py is the one file with no ancestor: Joe asked for a sklearn RandomForest
    sibling, and it implements the same interface and the same manifest shape as the ported three.
"""
