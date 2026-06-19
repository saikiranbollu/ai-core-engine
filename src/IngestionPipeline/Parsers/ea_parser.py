"""
Enterprise Architect Model Parser
=================================

Parses Enterprise Architect (EA) software architecture models using the
``object_model`` library (ifxPyArch) and flattens the rich object graph
into structured dicts suitable for the ingestion pipeline.

Two operating modes are supported:

* **Direct EA mode** — opens an ``.eap`` / ``.eaps`` / ``.qeax`` file (or
  a MySQL connection string) via EA COM automation.  Requires a Windows
  machine with EA installed.
* **JSON read mode** — reads a pre-exported *pyDump* directory containing
  ``jsonpickle``-serialized component data.  No EA dependency at runtime.

The mode is auto-detected from the *path* argument: if *path* is an
existing directory it is treated as a pyDump folder; otherwise it is
treated as an EA model file / connection string.

Usage::

    from IngestionPipeline.parsers import ea_parser

    # From a pre-exported pyDump directory (no EA required)
    result = ea_parser.parse(
        "C:/temp/pyDump",
        project="mcal",
        components=["Adc", "Spi"],
        configuration="path/to/pyArch_configuration.json",
    )

    # Directly from an EA model file (Windows + EA COM required)
    result = ea_parser.parse(
        "C:/models/MCAL.eap",
        project="mcal",
        components=["Adc"],
        configuration="path/to/pyArch_configuration.json",
    )
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported EA model file extensions
# ---------------------------------------------------------------------------
_EA_FILE_EXTENSIONS = {".eap", ".eaps", ".eapx", ".qeax"}


# ---------------------------------------------------------------------------
# Safe attribute access helpers
# ---------------------------------------------------------------------------

def _safe_str(obj: Any, attr: str, default: str = "") -> str:
    """Return ``str(getattr(obj, attr))`` or *default* on failure."""
    try:
        val = getattr(obj, attr, None)
        return str(val) if val is not None else default
    except Exception:
        return default


def _safe_attr(obj: Any, attr: str, default: Any = None) -> Any:
    """Return ``getattr(obj, attr)`` or *default* on failure."""
    try:
        return getattr(obj, attr, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Flattening helpers — convert object_model objects into plain dicts
# ---------------------------------------------------------------------------

class _EAModelFlattener:
    """Converts ``object_model`` ``Component`` / ``Common`` objects into
    pipeline-friendly ``dict`` structures.
    """

    # -- functions / interfaces -------------------------------------------

    @staticmethod
    def _flatten_param(param: Any) -> Dict[str, Any]:
        return {
            "name": _safe_str(param, "Name"),
            "type": _safe_str(param, "Type"),
            "direction": _safe_str(param, "Direction"),
            "description": _safe_str(param, "Description"),
            "guid": _safe_str(param, "GUID"),
            "is_const_ptr": _safe_attr(param, "IsConstPtr"),
            "const": _safe_attr(param, "Const"),
            "transient": _safe_attr(param, "Transient"),
            "reference_type": _safe_str(param, "ReferenceType"),
            "containment": _safe_str(param, "Containment"),
            "range": _safe_attr(param, "Range"),
        }

    @staticmethod
    def _flatten_error_handling(err: Any) -> Dict[str, str]:
        return {
            "name": _safe_str(err, "ErrName"),
            "type": _safe_str(err, "ErrType"),
            "description": _safe_str(err, "ErrDescription"),
        }

    @classmethod
    def _flatten_function(cls, func: Any) -> Dict[str, Any]:
        params = []
        for p in (_safe_attr(func, "Parameters") or []):
            params.append(cls._flatten_param(p))

        errors = []
        for e in (_safe_attr(func, "ErrorHandling") or []):
            errors.append(cls._flatten_error_handling(e))

        config_deps = list(_safe_attr(func, "ConfigDependencies") or [])

        design_decisions = []
        for d in (_safe_attr(func, "DesignDecisions") or []):
            design_decisions.append(cls._flatten_decision(d))

        mem_section = None
        ms = _safe_attr(func, "MemorySection")
        if ms:
            mem_section = _safe_str(ms, "Name")

        preprocessor = cls._flatten_preprocessor(_safe_attr(func, "PreprocessorFragment"))

        return {
            "name": _safe_str(func, "Name"),
            "guid": _safe_str(func, "GUID"),
            "fqn": _safe_str(func, "FQN"),
            "description": _safe_str(func, "Description"),
            "source": _safe_str(func, "Source"),
            "file": _safe_attr(func, "File"),
            "asil": _safe_str(func, "ASIL"),
            "service_id": _safe_str(func, "ServiceID"),
            "synchronous": _safe_str(func, "Synchronous"),
            "reentrant": _safe_str(func, "Reentrant"),
            "reentrant_desc": _safe_str(func, "ReentrantDesc"),
            "function_type": _safe_str(func, "FunctionType"),
            "call_type": _safe_str(func, "CallType"),
            "return_type": _safe_str(func, "ReturnType"),
            "return_type_reference": _safe_str(func, "ReturnType_Reference"),
            "return_type_description": _safe_str(func, "ReturnType_Description"),
            "body": _safe_str(func, "Body"),
            "algorithm": _safe_str(func, "Algorithm"),
            "comments": _safe_str(func, "Comments"),
            "events": _safe_str(func, "Events"),
            "user_hint": _safe_str(func, "UserHint"),
            "range": _safe_attr(func, "Range"),
            "memory_section": mem_section,
            "parameters": params,
            "error_handling": errors,
            "config_dependencies": config_deps,
            "design_decisions": design_decisions,
            "preprocessor": preprocessor,
        }

    @classmethod
    def _flatten_interface(cls, iface: Any) -> Dict[str, Any]:
        """Flatten a single interface (which wraps a Function)."""
        func = _safe_attr(iface, "Function")
        if func is None:
            return {}
        return cls._flatten_function(func)

    # -- datatypes --------------------------------------------------------

    @classmethod
    def _flatten_structure(cls, struct: Any) -> Dict[str, Any]:
        members = []
        for m in (_safe_attr(struct, "StructureMembers") or []):
            members.append({
                "name": _safe_str(m, "Name"),
                "type": _safe_str(m, "Type"),
                "is_const": _safe_attr(m, "IsConst"),
                "is_volatile": _safe_attr(m, "IsVolatile"),
                "containment": _safe_str(m, "Containment"),
                "multiplicity": _safe_str(m, "Multiplicity"),
                "range": _safe_attr(m, "Range"),
                "preprocessor": cls._flatten_preprocessor(
                    _safe_attr(m, "PreprocessorFragment")
                ),
            })

        return {
            "name": _safe_str(struct, "Name"),
            "guid": _safe_str(struct, "GUID"),
            "fqn": _safe_str(struct, "FQN"),
            "file": _safe_attr(struct, "File"),
            "source": _safe_str(struct, "Source"),
            "description": _safe_str(struct, "Description"),
            "members": members,
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(struct, "DesignDecisions") or [])
            ],
            "preprocessor": cls._flatten_preprocessor(
                _safe_attr(struct, "PreprocessorFragment")
            ),
        }

    @classmethod
    def _flatten_typedef(cls, td: Any) -> Dict[str, Any]:
        return {
            "name": _safe_str(td, "Name"),
            "guid": _safe_str(td, "GUID"),
            "fqn": _safe_str(td, "FQN"),
            "file": _safe_attr(td, "File"),
            "source": _safe_str(td, "Source"),
            "datatype": _safe_str(td, "Datatype"),
            "range": _safe_attr(td, "Range"),
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(td, "DesignDecisions") or [])
            ],
            "preprocessor": cls._flatten_preprocessor(
                _safe_attr(td, "PreprocessorFragment")
            ),
        }

    @classmethod
    def _flatten_enumeration(cls, enum: Any) -> Dict[str, Any]:
        literals = []
        for r in (_safe_attr(enum, "EnumRange") or []):
            literals.append({
                "name": _safe_str(r, "Name"),
                "value": _safe_str(r, "Value"),
                "preprocessor": cls._flatten_preprocessor(
                    _safe_attr(r, "PreprocessorFragment")
                ),
            })
        return {
            "name": _safe_str(enum, "Name"),
            "guid": _safe_str(enum, "GUID"),
            "fqn": _safe_str(enum, "FQN"),
            "file": _safe_attr(enum, "File"),
            "source": _safe_str(enum, "Source"),
            "type": _safe_str(enum, "Type"),
            "literals": literals,
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(enum, "DesignDecisions") or [])
            ],
            "preprocessor": cls._flatten_preprocessor(
                _safe_attr(enum, "PreprocessorFragment")
            ),
        }

    @classmethod
    def _flatten_function_pointer(cls, fptr: Any) -> Dict[str, Any]:
        params = []
        for p in (_safe_attr(fptr, "Parameters") or []):
            params.append(cls._flatten_param(p))
        return {
            "name": _safe_str(fptr, "Name"),
            "guid": _safe_str(fptr, "GUID"),
            "fqn": _safe_str(fptr, "FQN"),
            "file": _safe_attr(fptr, "File"),
            "source": _safe_str(fptr, "Source"),
            "return_type": _safe_str(fptr, "ReturnType"),
            "return_type_reference": _safe_str(fptr, "ReturnType_Reference"),
            "reference_function": _safe_str(fptr, "ReferenceFunction"),
            "parameters": params,
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(fptr, "DesignDecisions") or [])
            ],
            "preprocessor": cls._flatten_preprocessor(
                _safe_attr(fptr, "PreprocessorFragment")
            ),
        }

    @classmethod
    def _flatten_datatypes(cls, datatypes: Any) -> Dict[str, Any]:
        if datatypes is None:
            return {"structures": [], "typedefs": [], "enumerations": [], "function_pointers": []}
        return {
            "structures": [
                cls._flatten_structure(s) for s in (_safe_attr(datatypes, "Structures") or [])
            ],
            "typedefs": [
                cls._flatten_typedef(t) for t in (_safe_attr(datatypes, "Typedefs") or [])
            ],
            "enumerations": [
                cls._flatten_enumeration(e) for e in (_safe_attr(datatypes, "Enumerations") or [])
            ],
            "function_pointers": [
                cls._flatten_function_pointer(f) for f in (_safe_attr(datatypes, "FunctionPointers") or [])
            ],
        }

    # -- macros -----------------------------------------------------------

    @classmethod
    def _flatten_macro(cls, mc: Any) -> Dict[str, Any]:
        return {
            "name": _safe_str(mc, "Name"),
            "guid": _safe_str(mc, "GUID"),
            "fqn": _safe_str(mc, "FQN"),
            "file": _safe_attr(mc, "File"),
            "source": _safe_str(mc, "Source"),
            "type": _safe_str(mc, "Type"),
            "value": _safe_str(mc, "Value"),
            "algorithm": _safe_str(mc, "Algorithm"),
            "asserts": _safe_str(mc, "Asserts"),
            "multiplicity": _safe_str(mc, "Multiplicity"),
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(mc, "DesignDecisions") or [])
            ],
            "preprocessor": cls._flatten_preprocessor(
                _safe_attr(mc, "PreprocessorFragment")
            ),
        }

    # -- globals ----------------------------------------------------------

    @classmethod
    def _flatten_global(cls, gv: Any) -> Dict[str, Any]:
        mem_section = None
        ms = _safe_attr(gv, "MemorySection")
        if ms:
            mem_section = _safe_str(ms, "Name")
        return {
            "name": _safe_str(gv, "Name"),
            "guid": _safe_str(gv, "GUID"),
            "fqn": _safe_str(gv, "FQN"),
            "file": _safe_attr(gv, "File"),
            "source": _safe_str(gv, "Source"),
            "type": _safe_str(gv, "Type"),
            "scope": _safe_str(gv, "Scope"),
            "is_const": _safe_attr(gv, "IsConst"),
            "is_volatile": _safe_attr(gv, "IsVolatile"),
            "containment": _safe_str(gv, "Containment"),
            "multiplicity": _safe_str(gv, "Multiplicity"),
            "memory_section": mem_section,
            "range": _safe_attr(gv, "Range"),
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(gv, "DesignDecisions") or [])
            ],
            "preprocessor": cls._flatten_preprocessor(
                _safe_attr(gv, "PreprocessorFragment")
            ),
        }

    # -- error codes ------------------------------------------------------

    @staticmethod
    def _flatten_error_code(ec: Any) -> Dict[str, Any]:
        return {
            "name": _safe_str(ec, "Name"),
            "guid": _safe_str(ec, "GUID"),
            "fqn": _safe_str(ec, "FQN"),
            "file": _safe_attr(ec, "File"),
            "source": _safe_str(ec, "Source"),
            "error_value": _safe_str(ec, "ErrorValue"),
            "error_type": _safe_str(ec, "ErrorType"),
        }

    # -- memory sections --------------------------------------------------

    @staticmethod
    def _flatten_memory_section(ms: Any) -> Dict[str, str]:
        return {
            "name": _safe_str(ms, "Name"),
            "guid": _safe_str(ms, "GUID"),
            "fqn": _safe_str(ms, "FQN"),
            "source": _safe_str(ms, "Source"),
            "description": _safe_str(ms, "Description"),
        }

    # -- config interface -------------------------------------------------

    @classmethod
    def _flatten_config_param(cls, param: Any) -> Dict[str, Any]:
        return {
            "name": _safe_str(param, "Name"),
            "guid": _safe_str(param, "GUID"),
            "description": _safe_str(param, "Description"),
            "source": _safe_str(param, "Source"),
            "scope": _safe_str(param, "Scope"),
            "type": _safe_str(param, "Type"),
            "default": _safe_str(param, "Default"),
            "range": _safe_attr(param, "Range"),
            "lower_multiplicity": _safe_str(param, "LowerMultiplicity"),
            "upper_multiplicity": _safe_str(param, "UpperMultiplicity"),
            "pb_variant_multiplicity": _safe_str(param, "PBVariantMultiplicity"),
            "pb_variant_value": _safe_str(param, "PBVariantValue"),
            "is_published": _safe_attr(param, "isPublished"),
            "multiplicity_config_class": _safe_str(param, "MultiplicityConfigClass"),
            "value_config_class": _safe_str(param, "ValueConfigClass"),
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(param, "DesignDecisions") or [])
            ],
        }

    @classmethod
    def _flatten_config_container(cls, container: Any) -> Dict[str, Any]:
        params = [
            cls._flatten_config_param(p)
            for p in (_safe_attr(container, "ConfigParameters") or [])
        ]
        return {
            "name": _safe_str(container, "Name"),
            "guid": _safe_str(container, "GUID"),
            "description": _safe_str(container, "Description"),
            "source": _safe_str(container, "Source"),
            "scope": _safe_str(container, "Scope"),
            "type": _safe_str(container, "Type"),
            "lower_multiplicity": _safe_str(container, "LowerMultiplicity"),
            "upper_multiplicity": _safe_str(container, "UpperMultiplicity"),
            "pb_variant_multiplicity": _safe_str(container, "PBVariantMultiplicity"),
            "is_published": _safe_attr(container, "isPublished"),
            "sub_containers": list(_safe_attr(container, "ConfigSubContainers") or []),
            "config_dependencies": list(_safe_attr(container, "ConfigDependencies") or []),
            "config_parameters": params,
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(container, "DesignDecisions") or [])
            ],
        }

    @classmethod
    def _flatten_config_interface(cls, cfg: Any) -> List[Dict[str, Any]]:
        if cfg is None:
            return []
        containers = _safe_attr(cfg, "ConfigContainers") or []
        return [cls._flatten_config_container(c) for c in containers]

    # -- config structs ---------------------------------------------------

    @classmethod
    def _flatten_config_struct(cls, csv: Any) -> Dict[str, Any]:
        mem_section = None
        ms = _safe_attr(csv, "MemorySection")
        if ms:
            mem_section = _safe_str(ms, "Name")
        return {
            "name": _safe_str(csv, "Name"),
            "guid": _safe_str(csv, "GUID"),
            "fqn": _safe_str(csv, "FQN"),
            "file": _safe_attr(csv, "File"),
            "source": _safe_str(csv, "Source"),
            "type": _safe_str(csv, "Type"),
            "scope": _safe_str(csv, "Scope"),
            "is_const": _safe_attr(csv, "IsConst"),
            "is_volatile": _safe_attr(csv, "IsVolatile"),
            "containment": _safe_str(csv, "Containment"),
            "multiplicity": _safe_str(csv, "Multiplicity"),
            "memory_section": mem_section,
            "range": _safe_attr(csv, "Range"),
            "design_decisions": [
                cls._flatten_decision(d)
                for d in (_safe_attr(csv, "DesignDecisions") or [])
            ],
        }

    # -- decisions / AoUs -------------------------------------------------

    @classmethod
    def _flatten_decision(cls, dec: Any) -> Dict[str, Any]:
        children = []
        for child in (_safe_attr(dec, "Decisions") or []):
            children.append(cls._flatten_decision(child))
        return {
            "name": _safe_str(dec, "Name"),
            "guid": _safe_str(dec, "GUID"),
            "fqn": _safe_str(dec, "FQN"),
            "type": _safe_str(dec, "Type"),
            "description": _safe_str(dec, "Description"),
            "children": children,
        }

    @classmethod
    def _flatten_decisions_list(cls, decisions: Any) -> List[Dict[str, Any]]:
        if decisions is None:
            return []
        return [cls._flatten_decision(d) for d in decisions]

    # -- file structure ---------------------------------------------------

    @classmethod
    def _flatten_file_entry(cls, f: Any) -> Dict[str, Any]:
        deps = []
        for d in (_safe_attr(f, "Dependencies") or []):
            deps.append({
                "name": _safe_str(d, "Name"),
                "include_preference": _safe_str(d, "IncludePreference"),
            })
        return {
            "name": _safe_str(f, "Name"),
            "guid": _safe_str(f, "GUID"),
            "fqn": _safe_str(f, "FQN"),
            "source": _safe_str(f, "Source"),
            "description": _safe_str(f, "Description"),
            "version": _safe_str(f, "Version"),
            "generate": _safe_attr(f, "Generate"),
            "dependencies": deps,
        }

    @classmethod
    def _flatten_file_structure(cls, fs: Any) -> Dict[str, Any]:
        if fs is None:
            return {"header_files": [], "source_files": [], "ext_header_files": [], "plugin_files": []}
        result: Dict[str, Any] = {
            "header_files": [],
            "source_files": [],
            "ext_header_files": [],
            "plugin_files": [],
        }
        cfs = _safe_attr(fs, "CFileStructure")
        if cfs:
            result["header_files"] = [
                cls._flatten_file_entry(f) for f in (_safe_attr(cfs, "Headerfiles") or [])
            ]
            result["source_files"] = [
                cls._flatten_file_entry(f) for f in (_safe_attr(cfs, "Sourcefiles") or [])
            ]
            result["ext_header_files"] = [
                cls._flatten_file_entry(f) for f in (_safe_attr(cfs, "ExtHeaderfiles") or [])
            ]
        pfs = _safe_attr(fs, "PluginFileStructure")
        if pfs:
            result["plugin_files"] = [
                cls._flatten_file_entry(f) for f in (_safe_attr(pfs, "Plugins") or [])
            ]
        return result

    # -- HW/SW and SW/SW interfaces --------------------------------------

    @staticmethod
    def _flatten_hw_sw_interface(hwsw: Any) -> Dict[str, Any]:
        if hwsw is None:
            return {}
        return {
            "description": _safe_str(hwsw, "Description"),
        }

    @staticmethod
    def _flatten_sw_sw_interface(swsw: Any) -> Dict[str, Any]:
        if swsw is None:
            return {}
        return {
            "description": _safe_str(swsw, "Description"),
        }

    # -- user manual ------------------------------------------------------

    @classmethod
    def _flatten_um_element(cls, elem: Any) -> Dict[str, Any]:
        children = []
        for child in (_safe_attr(elem, "Elements") or []):
            children.append(cls._flatten_um_element(child))
        return {
            "name": _safe_str(elem, "Name"),
            "type": _safe_str(elem, "Type"),
            "description": _safe_str(elem, "Description"),
            "is_bullet": _safe_attr(elem, "IsBullet"),
            "is_code": _safe_attr(elem, "IsCode"),
            "date": _safe_str(elem, "Date"),
            "children": children,
        }

    @classmethod
    def _flatten_um_section(cls, section: Any) -> Dict[str, Any]:
        elements = []
        for e in (_safe_attr(section, "Elements") or []):
            elements.append(cls._flatten_um_element(e))
        sub_sections = []
        for s in (_safe_attr(section, "Sections") or []):
            sub_sections.append(cls._flatten_um_section(s))
        return {
            "name": _safe_str(section, "Name"),
            "description": _safe_str(section, "Description"),
            "elements": elements,
            "sections": sub_sections,
        }

    @classmethod
    def _flatten_user_manual(cls, um: Any) -> List[Dict[str, Any]]:
        if um is None:
            return []
        sections = []
        for s in (_safe_attr(um, "Sections") or []):
            sections.append(cls._flatten_um_section(s))
        return sections

    # -- external dependencies --------------------------------------------

    @classmethod
    def _flatten_external_dependencies(cls, ext: Any) -> Dict[str, Any]:
        if ext is None:
            return {"external_interfaces": {}, "external_local_interfaces": {}, "external_datatypes": {}}

        ext_ifaces: Dict[str, Any] = {}
        for comp, ifaces in (_safe_attr(ext, "ExternalInterfaces") or {}).items():
            ext_ifaces[comp] = [cls._flatten_interface(i) for i in (ifaces or [])]

        ext_local: Dict[str, Any] = {}
        for comp, ifaces in (_safe_attr(ext, "ExternalLocalInterface") or {}).items():
            ext_local[comp] = [cls._flatten_interface(i) for i in (ifaces or [])]

        ext_dt: Dict[str, Any] = {}
        for comp, datatypes in (_safe_attr(ext, "ExternalDatatypes") or {}).items():
            ext_dt[comp] = cls._flatten_datatypes(datatypes)

        return {
            "external_interfaces": ext_ifaces,
            "external_local_interfaces": ext_local,
            "external_datatypes": ext_dt,
        }

    # -- static data (glossary) -------------------------------------------

    @staticmethod
    def _flatten_static_data(sd: Any) -> Dict[str, Any]:
        if sd is None:
            return {}
        return {
            "acronym": _safe_str(sd, "Acronym"),
            "definition": _safe_str(sd, "Definition"),
            "references": _safe_str(sd, "References"),
        }

    # -- preprocessor -----------------------------------------------------

    @staticmethod
    def _flatten_preprocessor(ppf: Any) -> Optional[Dict[str, Any]]:
        if ppf is None:
            return None
        result: Dict[str, Any] = {}

        modeled = _safe_attr(ppf, "Modeled")
        if modeled:
            if _safe_attr(modeled, "IsExpression"):
                expressions = []
                expr_container = _safe_attr(modeled, "Expressions")
                if expr_container:
                    for expr in (_safe_attr(expr_container, "Expression") or []):
                        expressions.append({
                            "l_operand": _safe_str(expr, "LOperand"),
                            "r_operand": _safe_str(expr, "ROperand"),
                            "operator": _safe_str(expr, "Operator"),
                        })
                result["expressions"] = expressions

            if _safe_attr(modeled, "IsCompositeExpr"):
                composites = []
                expr_container = _safe_attr(modeled, "Expressions")
                if expr_container:
                    for cexpr in (_safe_attr(expr_container, "CompositeExpression") or []):
                        ce_dict: Dict[str, Any] = {
                            "constraint": _safe_str(cexpr, "Constraint"),
                            "expressions": {},
                        }
                        for key, val in (_safe_attr(cexpr, "Expression") or {}).items():
                            ce_dict["expressions"][key] = {
                                "l_operand": _safe_str(val, "LOperand"),
                                "r_operand": _safe_str(val, "ROperand"),
                                "operator": _safe_str(val, "Operator"),
                            }
                        composites.append(ce_dict)
                result["composite_expressions"] = composites

        freetext = _safe_attr(ppf, "FreeText")
        if freetext and _safe_attr(freetext, "IsAvailable"):
            result["free_text"] = {
                "pre_code_fragment": _safe_str(freetext, "PreCodeFragment"),
                "post_code_fragment": _safe_str(freetext, "PostCodeFragment"),
            }

        return result if result else None

    # -- top-level component flattening -----------------------------------

    @classmethod
    def flatten_component(cls, component: Any) -> Dict[str, Any]:
        """Flatten a ``Component`` object into a plain dict."""
        # Provided interfaces
        interfaces = []
        ifaces_obj = _safe_attr(component, "Interfaces")
        if ifaces_obj:
            provided = _safe_attr(ifaces_obj, "ProvidedInterfaces") or []
            for iface in provided:
                interfaces.append(cls._flatten_interface(iface))

        # Local interfaces
        local_interfaces = []
        for li in (_safe_attr(component, "LocalInterfaces") or []):
            local_interfaces.append(cls._flatten_interface(li))

        return {
            "component": _safe_str(component, "Name"),
            "guid": _safe_str(component, "GUID"),
            "fqn": _safe_str(component, "FQN"),
            "description": _safe_str(component, "Description"),
            "source": _safe_str(component, "Source"),
            "variant": _safe_str(component, "Variant"),
            "module_id": _safe_attr(component, "ModuleID"),
            "asil": _safe_str(component, "ASIL"),
            "interfaces": interfaces,
            "local_interfaces": local_interfaces,
            "datatypes": cls._flatten_datatypes(_safe_attr(component, "Datatypes")),
            "macros": [
                cls._flatten_macro(m)
                for m in (_safe_attr(component, "Macros") or [])
            ],
            "globals": [
                cls._flatten_global(g)
                for g in (_safe_attr(component, "Globals") or [])
            ],
            "error_codes": [
                cls._flatten_error_code(ec)
                for ec in (_safe_attr(component, "ErrorCodes") or [])
            ],
            "memory_sections": [
                cls._flatten_memory_section(ms)
                for ms in (_safe_attr(component, "MemorySections") or [])
            ],
            "file_structure": cls._flatten_file_structure(
                _safe_attr(component, "FileStructure")
            ),
            "config_interfaces": cls._flatten_config_interface(
                _safe_attr(component, "ConfigInterface")
            ),
            "config_structs": [
                cls._flatten_config_struct(cs)
                for cs in (_safe_attr(component, "ConfigStructs") or [])
            ],
            "decisions": {
                "safety_aous": cls._flatten_decisions_list(
                    _safe_attr(component, "SafetyAous")
                ),
                "security_aous": cls._flatten_decisions_list(
                    _safe_attr(component, "SecurityAous")
                ),
                "design_decisions": cls._flatten_decisions_list(
                    _safe_attr(component, "DesignDecisions")
                ),
                "design_information": cls._flatten_decisions_list(
                    _safe_attr(component, "DesignInformation")
                ),
                "architectural_decisions": cls._flatten_decisions_list(
                    _safe_attr(component, "ArchitecturalDecisions")
                ),
                "architectural_information": cls._flatten_decisions_list(
                    _safe_attr(component, "ArchitecturalInformation")
                ),
            },
            "hw_sw_interface": cls._flatten_hw_sw_interface(
                _safe_attr(component, "HwSwInterface")
            ),
            "sw_sw_interface": cls._flatten_sw_sw_interface(
                _safe_attr(component, "SwSwInterface")
            ),
            "user_manual": cls._flatten_user_manual(
                _safe_attr(component, "UserManual")
            ),
            "external_dependencies": cls._flatten_external_dependencies(
                _safe_attr(component, "ExternalDependencies")
            ),
            "static_data": cls._flatten_static_data(
                _safe_attr(component, "StaticData")
            ),
        }

    @classmethod
    def flatten_common(cls, common: Any) -> Dict[str, Any]:
        """Flatten a ``Common`` object into a plain dict."""
        sub_components = []
        for comp in (_safe_attr(common, "Components") or []):
            sub_components.append(cls.flatten_component(comp))

        mcal_usage = []
        for mu in (_safe_attr(common, "McalUsage") or []):
            hw_deps = []
            for hw in (_safe_attr(mu, "HwDependencies") or []):
                hw_deps.append({
                    "name": _safe_str(hw, "Name"),
                    "access": _safe_str(hw, "Access"),
                })
            sw_deps = []
            for sw in (_safe_attr(mu, "SwDependencies") or []):
                sw_deps.append({
                    "name": _safe_str(sw, "Name"),
                    "description": _safe_str(sw, "Description"),
                })
            mcal_usage.append({
                "module_name": _safe_str(mu, "ModuleName"),
                "asil": _safe_str(mu, "Asil"),
                "variant": _safe_str(mu, "Variant"),
                "partition_granularity": _safe_str(mu, "PartitionGranularity"),
                "hw_dependencies": hw_deps,
                "sw_dependencies": sw_deps,
            })

        return {
            "component": "Common",
            "sub_components": sub_components,
            "mcal_usage": mcal_usage,
            "user_manual": cls._flatten_user_manual(
                _safe_attr(common, "UserManual")
            ),
            "static_data": cls._flatten_static_data(
                _safe_attr(common, "StaticData")
            ),
            "decisions": {
                "safety_aous": cls._flatten_decisions_list(
                    _safe_attr(common, "SafetyAous")
                ),
                "security_aous": cls._flatten_decisions_list(
                    _safe_attr(common, "SecurityAous")
                ),
            },
        }

    # -- statistics -------------------------------------------------------

    @classmethod
    def compute_statistics(cls, components: List[Dict[str, Any]]) -> Dict[str, int]:
        """Compute aggregate statistics from a list of flattened components."""
        total_functions = 0
        total_local_functions = 0
        total_structures = 0
        total_typedefs = 0
        total_enumerations = 0
        total_function_pointers = 0
        total_macros = 0
        total_globals = 0
        total_error_codes = 0
        total_memory_sections = 0
        total_config_containers = 0

        for comp in components:
            total_functions += len(comp.get("interfaces", []))
            total_local_functions += len(comp.get("local_interfaces", []))
            dt = comp.get("datatypes", {})
            total_structures += len(dt.get("structures", []))
            total_typedefs += len(dt.get("typedefs", []))
            total_enumerations += len(dt.get("enumerations", []))
            total_function_pointers += len(dt.get("function_pointers", []))
            total_macros += len(comp.get("macros", []))
            total_globals += len(comp.get("globals", []))
            total_error_codes += len(comp.get("error_codes", []))
            total_memory_sections += len(comp.get("memory_sections", []))
            total_config_containers += len(comp.get("config_interfaces", []))

        return {
            "total_components": len(components),
            "total_functions": total_functions,
            "total_local_functions": total_local_functions,
            "total_structures": total_structures,
            "total_typedefs": total_typedefs,
            "total_enumerations": total_enumerations,
            "total_function_pointers": total_function_pointers,
            "total_macros": total_macros,
            "total_globals": total_globals,
            "total_error_codes": total_error_codes,
            "total_memory_sections": total_memory_sections,
            "total_config_containers": total_config_containers,
        }


# ---------------------------------------------------------------------------
# EA model extraction — wraps object_model parsing
# ---------------------------------------------------------------------------

class _EAModelExtractor:
    """Manages invoking object_model's ``EAParser`` in either direct EA
    mode or pyDump JSON-read mode.
    """

    def __init__(
        self,
        path: str,
        project: str,
        components: List[str],
        options: str = "all",
        configuration: Optional[str] = None,
    ):
        self._path = path
        self._project = project
        self._components = components
        self._options = options
        self._configuration = configuration

    @property
    def _is_pydump_dir(self) -> bool:
        return os.path.isdir(self._path)

    @property
    def _is_ea_file(self) -> bool:
        ext = Path(self._path).suffix.lower()
        return ext in _EA_FILE_EXTENSIONS

    def extract(self) -> List[Any]:
        """Parse the EA model and return the list of component objects.

        Returns:
            List of ``Component`` or ``Common`` objects from object_model.

        Raises:
            FileNotFoundError: If *path* does not exist.
            RuntimeError: If EA COM automation is unavailable in direct mode.
            ImportError: If object_model is not installed.
        """
        # Validate path
        if not self._is_pydump_dir and not os.path.exists(self._path):
            raise FileNotFoundError(f"Path not found: {self._path}")

        # Import object_model (raises ImportError if missing)
        try:
            from object_model import ParseEADb  # noqa: F811
            from object_model.utility.ProjectConfigurations import (
                ProjectConfigurations,
            )
        except ImportError as exc:
            raise ImportError(
                "The 'object_model' package (ifxPyArch) is required for EA "
                "model parsing.  Install it with: pip install ifxPyArch"
            ) from exc

        # Set up project configuration if provided
        if self._configuration:
            ProjectConfigurations(self._configuration, ["ea_parser"])

        # Determine mode
        if self._is_pydump_dir:
            mode = "Read"
            py_dump = self._path
            conn_string = ""
        else:
            mode = None
            py_dump = None
            conn_string = self._path

        # Parse
        options_list = (
            self._options if isinstance(self._options, list) else [self._options]
        )
        ea_parse_obj = ParseEADb(
            workerObj=None,
            project=self._project,
            connString=conn_string,
            components=self._components,
            options=options_list,
            pyDump=py_dump,
            verbose=False,
            mode=mode,
        )

        if not ea_parse_obj.Status:
            raise RuntimeError(
                "EA model parsing failed. Check logs for details."
            )

        return ea_parse_obj.ParsedModelList


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse(
    path: str,
    *,
    project: str,
    components: List[str],
    options: str = "all",
    configuration: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parse an Enterprise Architect model and return structured component data.

    The function auto-detects the operating mode from *path*:

    * If *path* is a directory, it is treated as a **pyDump** folder
      containing ``jsonpickle``-serialized component JSON files (no EA
      COM dependency).
    * If *path* is a file (e.g. ``.eap``, ``.eaps``, ``.qeax``) or a
      connection string, it is opened directly via EA COM automation
      (requires Windows with EA installed).

    Args:
        path:          Path to a pyDump directory **or** an EA model file /
                       connection string.
        project:       Project identifier used by the object_model INI
                       configuration (e.g. ``"mcal"``, ``"stl"``, ``"tle"``).
        components:    List of EA component names to parse
                       (e.g. ``["Adc", "Spi", "Common"]``).
        options:       Parse filter — ``"all"``, ``"codegen"``, ``"ut"``,
                       ``"um"``, ``"debug"``, ``"dafa"``, ``"swe"``, etc.
        configuration: Path to the ``pyArch_configuration.json`` file.
                       Required for project path resolution.

    Returns:
        A dict with keys ``project``, ``components``, ``common``, and
        ``statistics``.

    Raises:
        FileNotFoundError: If *path* does not exist (and is not a
            connection string).
        RuntimeError:      If parsing fails.
        ImportError:       If ``object_model`` is not installed.
    """
    # Validate path (allow connection strings that aren't local paths)
    p = Path(path)
    is_conn_string = not p.suffix and not p.is_dir() and not p.exists()
    if not p.exists() and not is_conn_string:
        raise FileNotFoundError(f"File or directory not found: {path}")

    extractor = _EAModelExtractor(
        path=path,
        project=project,
        components=components,
        options=options,
        configuration=configuration,
    )

    parsed_models = extractor.extract()
    flattener = _EAModelFlattener()

    flat_components: List[Dict[str, Any]] = []
    common_data: Optional[Dict[str, Any]] = None

    for model in parsed_models:
        name = _safe_str(model, "Name")
        if name == "Common":
            common_data = flattener.flatten_common(model)
        else:
            flat_components.append(flattener.flatten_component(model))

    return {
        "project": project,
        "components": flat_components,
        "common": common_data,
        "statistics": flattener.compute_statistics(flat_components),
    }
