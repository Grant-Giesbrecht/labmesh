from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.abspath(".."))

project = "labmesh"
copyright = "2026, Grant Giesbrecht"
author = "Grant Giesbrecht"

try:
    from importlib.metadata import version as _pkg_version
    release = _pkg_version("labmesh")
except Exception:
    release = "0.0.0.dev0"
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinxcontrib.mermaid",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbnails", ".DS_Store"]

autodoc_member_order = "bysource"
autodoc_typehints = "description"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

html_theme = "sphinx_rtd_theme"
html_static_path = ["_static"]
html_logo = "images/labmesh.png"
html_title = "labmesh"

mermaid_version = "10.9.0"
