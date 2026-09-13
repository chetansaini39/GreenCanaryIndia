"""One-time startup imports for the data/charting stack.

Parallel dashboard requests (OHLCV + EOD charts via Plotly) can otherwise
first-import numpy from different threads and leave it partially
initialized — Plotly then fails with:
  AttributeError: ... numpy ... has no attribute 'isscalar'
"""


def eager_import_data_stack() -> None:
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    import plotly.graph_objects  # noqa: F401
    from plotly.subplots import make_subplots  # noqa: F401

    # Touch Plotly validators that rely on numpy before any worker thread runs.
    make_subplots(rows=1, cols=1)
