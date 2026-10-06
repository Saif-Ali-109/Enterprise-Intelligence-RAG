"""Evaluation package: dataset, metrics, runner.

A run is the unit of "the pipeline was measured", and the package exists so
every number on a dashboard traces to one run's row (Principle VI). Nothing
here is allowed to produce a figure that did not come out of a question being
executed through the same chat service a user would hit.
"""
