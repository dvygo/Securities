"""Checks that run over what the pipeline wrote, rather than over its functions.

  tokens.py   counterTokenV2 against the normalized parquet and the manifests
              beside it, within a day and across consecutive days
  lineage.py  the chain itself -- raw DBN -> normalized -> plugin -- proving no
              row was silently invented, lost, or renumbered between stages
  report.py   the Check verdict and the printer both of them use

Unit tests pin the rules; these pin the artefacts. A rule test cannot see a
venue whose manifest was written but whose parquet was aborted, or a Tuesday
definition file sitting in Wednesday's directory. Import the submodule.
"""
