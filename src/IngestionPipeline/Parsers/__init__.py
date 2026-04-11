"""
IngestionPipeline.Parsers
=========================

A collection of file parsers for the ingestion pipeline.  Each parser
exposes a single ``parse(path, ...)`` function that reads a file and
returns structured content — **no file writing, no CLI, no side-effects**.

Available parsers
-----------------

====================  ==============================  ============================
Module                Input type                      Return type
====================  ==============================  ============================
``arxml_parser``      ``.arxml`` AUTOSAR XML files     ``dict`` (modules + chunks)
``c_parser``          ``.c`` source files             ``dict`` (functions + stats)
``doxygen_parser``    Doxygen-annotated files          ``list[dict]`` (requirements)
``ea_parser``         EA models / pyDump dirs          ``dict`` (components + stats)
``pdf_parser``        ``.pdf`` documents (LLM)         ``str`` (Markdown)
``puml_parser``       ``.puml`` sequence diagrams      ``dict`` (pattern library)
``sfr_parser``        ``*_regdef.h`` headers           ``dict`` (registers)
``rst_parser``        ``.rst`` documents               ``list[dict]`` (sections)
``swa_parser``        ``*_swa.h`` headers              ``dict`` (macros/enums/…)
``xlsx_parser``       ``.xlsx`` workbooks              ``dict`` (sheet → rows)
====================  ==============================  ============================

Quick start::

    from IngestionPipeline.Parsers import c_parser, rst_parser

    analysis = c_parser.parse("driver.c")
    sections = rst_parser.parse("docs.rst")
"""

from importlib import import_module

__all__ = [
    "arxml_parser",
    "c_parser",
    "doxygen_parser",
    "ea_parser",
    "hw_spec_parser",
    "illd_swa_parser",
    "pdf_parser",
    "puml_parser",
    "rst_parser",
    "sfr_parser",
    "xlsx_parser",
]


def __getattr__(name):
    if name in __all__:
        module = import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__():
    return sorted(list(globals().keys()) + __all__)
