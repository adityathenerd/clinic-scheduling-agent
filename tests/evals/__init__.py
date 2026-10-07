"""Evaluation harness tests.

When unittest discovers from ``tests/`` it imports this directory as the
top-level ``evals`` package. Extend that package path to the application
package so imports remain stable under both focused and full-suite discovery.
"""

from pathlib import Path

__path__.append(str(Path(__file__).resolve().parents[2] / "evals"))
