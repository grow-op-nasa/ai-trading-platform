"""Core configuration layer.

This package is the application's "brain": it knows where every directory
lives (settings.py) and how logging is wired up (logging.py). Everything
else in the codebase should read configuration from here rather than
hardcoding paths or constants -- that's what makes it possible to change
the watchlist, the data lookback period, or the log location in one place.
"""
