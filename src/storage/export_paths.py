"""Stable on-disk location for generated Excel exports.

This leaf module is shared by the notification writer and authorization
provenance checks, avoiding a dependency from auth back into notify.
"""
from pathlib import Path

from ..paths import ROOT

EXPORT_DIR = "out/exports"


def export_dir(root=None, subdir=EXPORT_DIR):
    """Return the per-install export directory."""
    return Path(root or ROOT) / subdir
