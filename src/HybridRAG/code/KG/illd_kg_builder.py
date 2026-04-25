"""
ILLD Knowledge Graph Builder
============================

Transforms parser outputs into Neo4j nodes and edges for the ILLD profile.
Uses the ontology.yaml v2.0.0 ILLD profile as schema reference.

All 6 data sources are supported:
    1. HW Spec (hw_spec_parser)    → HardwareRegister, RegisterField, Interrupt, Error
    2. Requirements (JamaConnector) → Requirement
    3. SWA Header (illd_swa_parser) → Function, Struct, StructMember, Enum, EnumValue,
                                       Typedef, Parameter, ReturnType
    4. Source Code (c_parser)       → Function call graph (CALLS_INTERNALLY)
    5. SFR (sfr_parser)             → Register, BitField
    6. PlantUML (puml_parser)       → enriches Function nodes with sequence patterns

Connection is managed via neo4j_manager.py (storage_config.yaml).

Usage::

    from KG.illd_kg_builder import ILLDKGBuilder

    builder = ILLDKGBuilder(module="CXPI")
    builder.ingest_swa(swa_data)
    builder.ingest_sfr(sfr_data)
    builder.ingest_hw_spec(hw_spec_data)
    builder.ingest_requirements(requirements)
    builder.ingest_source(c_data)
    builder.ingest_puml(puml_data)
    builder.create_cross_source_relationships()
    builder.print_summary()
    builder.close()
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from neo4j import GraphDatabase
from neo4j.exceptions import ServiceUnavailable, AuthError, TransientError

logger = logging.getLogger("illd_kg_builder")

# ---------------------------------------------------------------------------
# Batch size for UNWIND operations
# ---------------------------------------------------------------------------
BATCH_SIZE = 500


class ILLDKGBuilder:
    """
    Builds the ILLD Neo4j knowledge graph from parser outputs.

    Each ``ingest_*`` method accepts the dict returned by its
    corresponding parser and creates the appropriate nodes + edges.
    Call ``create_cross_source_relationships`` after all sources are
    ingested to wire up inter-source links (e.g. Function → Register).
    """

    def __init__(
        self,
        module: str,
        neo4j_cfg: Optional[dict] = None,
        dry_run: bool = False,
        clear_db: bool = False,
    ):
        self.module = module.upper()
        self.dry_run = dry_run
        self.clear_db = clear_db
        self.stats: Dict[str, int] = Counter()
        self._driver = None

        # Resolve config from storage_config.yaml when not provided
        if neo4j_cfg is None:
            neo4j_cfg = self._load_neo4j_config()
        self.neo4j_cfg = neo4j_cfg

        if not dry_run:
            self._connect()
            if clear_db:
                self._clear_database()

    # -- Configuration ------------------------------------------------------

    @staticmethod
    def _load_neo4j_config() -> dict:
        """Load ILLD Neo4j settings from storage_config.yaml."""
        import sys
        script_dir = Path(__file__).resolve().parent.parent  # .../HybridRAG/code
        if str(script_dir) not in sys.path:
            sys.path.insert(0, str(script_dir))
        from env_config import load_yaml_with_env

        config_path = script_dir.parent / "config" / "storage_config.yaml"
        cfg = load_yaml_with_env(config_path)
        return cfg["neo4j"]["illd"]

    # -- Connection ---------------------------------------------------------

    def _connect(self):
        cfg = self.neo4j_cfg
        uri = cfg["uri"]
        logger.info("Connecting to ILLD Neo4j at %s …", uri)

        # bolt+ssc / bolt+s / neo4j+ssc / neo4j+s URIs handle encryption
        # via the scheme itself — do NOT pass encrypted= with those schemes.
        driver_kwargs = dict(
            auth=(cfg["username"], cfg["password"]),
            max_connection_lifetime=cfg.get("max_connection_lifetime", 3600),
            max_connection_pool_size=cfg.get("max_connection_pool_size", 50),
        )
        scheme = uri.split("://")[0].lower()
        if "+" not in scheme:
            # Plain bolt:// or neo4j:// — honour explicit encrypted flag
            driver_kwargs["encrypted"] = cfg.get("encrypted", False)

        self._driver = GraphDatabase.driver(uri, **driver_kwargs)
        self._driver.verify_connectivity()
        logger.info("Connected (database: %s)", cfg["database"])

    def close(self):
        if self._driver:
            self._driver.close()
            self._driver = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- Low-level Neo4j helpers --------------------------------------------

    def _write(self, cypher: str, parameters: Optional[dict] = None):
        """Execute a write transaction with retry."""
        if self.dry_run:
            return
        db = self.neo4j_cfg["database"]
        for attempt in range(1, 4):
            try:
                with self._driver.session(database=db) as session:
                    session.execute_write(
                        lambda tx: tx.run(cypher, parameters or {})
                    )
                return
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= 3:
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient error (attempt %d/3), retrying in %ds: %s",
                               attempt, wait, exc)
                time.sleep(wait)

    def _read(self, cypher: str, parameters: Optional[dict] = None) -> list:
        """Execute a read query with retry."""
        if self.dry_run:
            return []
        db = self.neo4j_cfg["database"]
        for attempt in range(1, 4):
            try:
                with self._driver.session(database=db) as session:
                    result = session.run(cypher, parameters or {})
                    return [rec.data() for rec in result]
            except (ServiceUnavailable, TransientError, OSError) as exc:
                if attempt >= 3:
                    raise
                wait = min(2 ** attempt, 8)
                logger.warning("Transient error (attempt %d/3), retrying in %ds: %s",
                               attempt, wait, exc)
                time.sleep(wait)
        return []

    def _clear_database(self):
        logger.warning("Clearing ALL data in database '%s' …", self.neo4j_cfg["database"])
        self._write("MATCH (n) DETACH DELETE n")
        logger.info("Database cleared.")

    # -- Batch helpers ------------------------------------------------------

    @staticmethod
    def _chunked(items: list, size: int = BATCH_SIZE):
        for i in range(0, len(items), size):
            yield items[i : i + size]

    def _merge_nodes(self, label: str, uid_prop: str, items: List[dict]):
        """UNWIND-batch MERGE nodes by unique property."""
        if not items:
            return
        # Force the pipeline module on every node (parsers may set their own)
        for item in items:
            item["module"] = self.module
        logger.info("  Merging %d :%s nodes …", len(items), label)
        for chunk in self._chunked(items):
            cypher = (
                f"UNWIND $items AS props "
                f"MERGE (n:{label} {{{uid_prop}: props.{uid_prop}}}) "
                f"ON CREATE SET n.global_id = randomUUID() "
                f"SET n += props"
            )
            self._write(cypher, {"items": chunk})
        self.stats[f"nodes:{label}"] += len(items)

    def _merge_edges(self, rel_type: str, from_label: str, from_uid: str,
                     to_label: str, to_uid: str, edges: List[dict],
                     edge_props: Optional[List[str]] = None):
        """UNWIND-batch MERGE edges between nodes.

        Each dict in *edges* must have ``from_key`` and ``to_key``.
        Optional extra properties are listed in *edge_props*.
        """
        if not edges:
            return
        logger.info("  Merging %d :%s edges …", len(edges), rel_type)

        # Build SET clause for edge properties
        set_parts = []
        if edge_props:
            for p in edge_props:
                set_parts.append(f"r.{p} = e.{p}")
        set_clause = "SET " + ", ".join(set_parts) if set_parts else ""

        for chunk in self._chunked(edges):
            cypher = (
                f"UNWIND $edges AS e "
                f"MATCH (a:{from_label} {{{from_uid}: e.from_key}}) "
                f"MATCH (b:{to_label} {{{to_uid}: e.to_key}}) "
                f"MERGE (a)-[r:{rel_type}]->(b) "
                f"{set_clause}"
            )
            self._write(cypher, {"edges": chunk})
        self.stats[f"rel:{rel_type}"] += len(edges)

    @staticmethod
    def _safe_str(value) -> Optional[str]:
        """Convert value to string, return None for empty/None."""
        if value is None:
            return None
        s = str(value).strip()
        return s if s else None

    @staticmethod
    def _serialize_complex(value) -> Optional[str]:
        """JSON-serialize lists/dicts for Neo4j storage."""
        if value is None:
            return None
        if isinstance(value, (list, dict, set, frozenset)):
            return json.dumps(value, default=str)
        return str(value)

    # =====================================================================
    # 1. SWA Header ingestion (illd_swa_parser output)
    # =====================================================================

    def ingest_swa(self, swa_data: dict, source_file: str = None):
        """
        Ingest SWA parser output → Function, Struct, StructMember, Enum,
        EnumValue, Typedef, Parameter, ReturnType nodes + edges.

        *source_file* — the originating header filename (e.g. IfxLcss_Pwm_swa.h).
        Stored on every top-level node for traceability.
        """
        if not swa_data:
            logger.warning("No SWA data to ingest.")
            return

        logger.info("Ingesting SWA data for module %s (file=%s) …",
                    self.module, source_file or "default")
        mod = self.module  # Always use pipeline module, not parser-derived

        # --- Functions → Function nodes ---
        functions = swa_data.get("functions", [])
        func_nodes = []
        param_nodes = []
        ret_nodes = []
        func_param_edges = []
        func_ret_edges = []
        func_dep_edges = []

        for func in functions:
            if not isinstance(func, dict):
                continue
            fname = func.get("name", "")
            if not fname:
                continue

            fid = f"FUNC_{fname}"
            node = {
                "id": fid,
                "name": fname,
                "brief": self._safe_str(func.get("brief")),
                "purpose": self._safe_str(func.get("purpose") or func.get("detailed_description")),
                "return_type": self._safe_str(func.get("return_type")),
                "source": "SWA_Functions",
                "module": mod,
                "label": fname,
            }
            if source_file:
                node["source_file"] = source_file
            func_nodes.append(node)

            # Parameters
            params = func.get("parameters") or []
            for idx, p in enumerate(params):
                if not isinstance(p, dict):
                    continue
                pname = p.get("name", f"param{idx}")
                ptype = p.get("type", "unknown")
                pid = f"PARAM_{fname}_{pname}"
                param_nodes.append({
                    "id": pid,
                    "name": pname,
                    "type": ptype,
                    "label": f"{pname}: {ptype}",
                })
                func_param_edges.append({
                    "from_key": fid,
                    "to_key": pid,
                    "position": idx,
                })

            # Return type
            rtype = func.get("return_type")
            if rtype and rtype != "void":
                rid = f"RETTYPE_{rtype}"
                ret_nodes.append({"id": rid, "name": rtype, "label": rtype})
                func_ret_edges.append({"from_key": fid, "to_key": rid})

            # Dependencies (function→function)
            deps = func.get("dependencies") or []
            for dep in deps:
                if dep and isinstance(dep, str):
                    func_dep_edges.append({
                        "from_key": fid,
                        "to_key": f"FUNC_{dep}",
                        "dependency_type": "Calls",
                    })

        # Deduplicate return type nodes
        seen_ret = set()
        unique_ret = []
        for r in ret_nodes:
            if r["id"] not in seen_ret:
                seen_ret.add(r["id"])
                unique_ret.append(r)

        self._merge_nodes("Function", "id", func_nodes)
        self._merge_nodes("Parameter", "id", param_nodes)
        self._merge_nodes("ReturnType", "id", unique_ret)
        self._merge_edges("HAS_PARAMETER", "Function", "id", "Parameter", "id",
                          func_param_edges, ["position"])
        self._merge_edges("RETURN_TYPE", "Function", "id", "ReturnType", "id",
                          func_ret_edges)
        self._merge_edges("DEPENDS_ON", "Function", "id", "Function", "id",
                          func_dep_edges, ["dependency_type"])

        # --- Structs → Struct + StructMember nodes ---
        structs = swa_data.get("structs", [])
        struct_nodes = []
        member_nodes = []
        struct_member_edges = []

        for s in structs:
            if not isinstance(s, dict):
                continue
            sname = s.get("name", "")
            if not sname:
                continue

            sid = f"STRUCT_{sname}"
            snode = {
                "id": sid,
                "name": sname,
                "brief": self._safe_str(s.get("brief")),
                "purpose": self._safe_str(s.get("purpose")),
                "source": "SWA_Structs",
                "module": mod,
                "label": sname,
            }
            if source_file:
                snode["source_file"] = source_file
            struct_nodes.append(snode)

            members = s.get("members") or []
            for m in members:
                if not isinstance(m, dict):
                    continue
                mname = m.get("name", "")
                mtype = m.get("type", "unknown")
                if not mname:
                    continue
                mid = f"MEMBER_{sname}_{mname}"
                member_nodes.append({
                    "id": mid,
                    "name": mname,
                    "type": mtype,
                    "description": self._safe_str(m.get("description")),
                    "label": f"{mname}: {mtype}",
                })
                struct_member_edges.append({"from_key": sid, "to_key": mid})

        self._merge_nodes("Struct", "id", struct_nodes)
        self._merge_nodes("StructMember", "id", member_nodes)
        self._merge_edges("HAS_MEMBER", "Struct", "id", "StructMember", "id",
                          struct_member_edges)

        # --- Enums → Enum + EnumValue nodes ---
        enums = swa_data.get("enums", [])
        enum_nodes = []
        eval_nodes = []
        enum_val_edges = []

        for e in enums:
            if not isinstance(e, dict):
                continue
            ename = e.get("name", "")
            if not ename:
                continue

            eid = f"ENUM_{ename}"
            enode = {
                "id": eid,
                "name": ename,
                "brief": self._safe_str(e.get("brief")),
                "purpose": self._safe_str(e.get("purpose")),
                "source": "SWA_Enums",
                "module": mod,
                "label": ename,
            }
            if source_file:
                enode["source_file"] = source_file
            enum_nodes.append(enode)

            values = e.get("values") or []
            for v in values:
                if not isinstance(v, dict):
                    continue
                vname = v.get("name", "")
                if not vname:
                    continue
                vid = f"ENUMVAL_{vname}"
                eval_nodes.append({
                    "id": vid,
                    "name": vname,
                    "value": self._safe_str(v.get("value")),
                    "description": self._safe_str(v.get("description")),
                    "label": vname,
                })
                enum_val_edges.append({"from_key": eid, "to_key": vid})

        self._merge_nodes("Enum", "id", enum_nodes)
        self._merge_nodes("EnumValue", "id", eval_nodes)
        self._merge_edges("HAS_VALUE", "Enum", "id", "EnumValue", "id",
                          enum_val_edges)

        # --- Typedefs → Typedef nodes ---
        typedefs = swa_data.get("typedefs", [])
        typedef_nodes = []
        for td in typedefs:
            if not isinstance(td, dict):
                continue
            tname = td.get("name", "")
            if not tname:
                continue
            tdnode = {
                "id": f"TYPEDEF_{tname}",
                "name": tname,
                "brief": self._safe_str(td.get("brief")),
                "purpose": self._safe_str(td.get("purpose")),
                "underlying_type": self._safe_str(td.get("type")),
                "source": "SWA_Typedefs",
                "module": mod,
                "label": tname,
            }
            if source_file:
                tdnode["source_file"] = source_file
            typedef_nodes.append(tdnode)

        self._merge_nodes("Typedef", "id", typedef_nodes)

        # ----------------------------------------------------------------
        # Secondary nodes derived from SWA data
        # ----------------------------------------------------------------

        # --- PrimitiveType nodes + ALIASES edges (Typedef → PrimitiveType) ---
        PRIMITIVE_TYPES = {
            "uint8", "uint16", "uint32", "uint64",
            "int8", "int16", "int32", "int64",
            "uint8_t", "uint16_t", "uint32_t", "uint64_t",
            "int8_t", "int16_t", "int32_t", "int64_t",
            "float32", "float64", "boolean", "sint8", "sint16", "sint32",
            "void", "char", "int", "unsigned", "float", "double",
        }
        primitive_nodes = []
        aliases_edges = []
        seen_primitives = set()

        for td in typedef_nodes:
            utype = (td.get("underlying_type") or "").strip().rstrip("*").strip()
            if not utype:
                continue
            # Normalise: strip pointer/const qualifiers
            clean = utype.replace("const ", "").replace("volatile ", "").strip().rstrip("*").strip()
            if clean.lower() in {p.lower() for p in PRIMITIVE_TYPES}:
                pid = f"PRIMITIVE_{clean}"
                if pid not in seen_primitives:
                    seen_primitives.add(pid)
                    primitive_nodes.append({
                        "id": pid,
                        "name": clean,
                        "label": clean,
                    })
                aliases_edges.append({
                    "from_key": td["id"],
                    "to_key": pid,
                })

        self._merge_nodes("PrimitiveType", "id", primitive_nodes)
        self._merge_edges("ALIASES", "Typedef", "id", "PrimitiveType", "id",
                          aliases_edges)

        # --- OF_TYPE edges (Parameter → Struct/Typedef/Enum/PrimitiveType) ---
        # Build lookup sets for type resolution
        struct_names = {s["name"] for s in struct_nodes}
        typedef_names = {t["name"] for t in typedef_nodes}
        enum_names = {e["name"] for e in enum_nodes}

        of_type_edges_param = []
        for p in param_nodes:
            ptype_raw = (p.get("type") or "").strip().rstrip("*").strip()
            ptype_clean = ptype_raw.replace("const ", "").replace("volatile ", "").strip().rstrip("*").strip()
            if not ptype_clean:
                continue
            # Match against known types
            if ptype_clean in struct_names:
                of_type_edges_param.append({
                    "from_key": p["id"],
                    "to_key": f"STRUCT_{ptype_clean}",
                    "target_label": "Struct",
                })
            elif ptype_clean in typedef_names:
                of_type_edges_param.append({
                    "from_key": p["id"],
                    "to_key": f"TYPEDEF_{ptype_clean}",
                    "target_label": "Typedef",
                })
            elif ptype_clean in enum_names:
                of_type_edges_param.append({
                    "from_key": p["id"],
                    "to_key": f"ENUM_{ptype_clean}",
                    "target_label": "Enum",
                })

        # OF_TYPE for Parameter → Struct
        param_struct = [e for e in of_type_edges_param if e["target_label"] == "Struct"]
        self._merge_edges("OF_TYPE", "Parameter", "id", "Struct", "id", param_struct)
        # OF_TYPE for Parameter → Typedef
        param_typedef = [e for e in of_type_edges_param if e["target_label"] == "Typedef"]
        self._merge_edges("OF_TYPE", "Parameter", "id", "Typedef", "id", param_typedef)
        # OF_TYPE for Parameter → Enum
        param_enum = [e for e in of_type_edges_param if e["target_label"] == "Enum"]
        self._merge_edges("OF_TYPE", "Parameter", "id", "Enum", "id", param_enum)

        # --- OF_TYPE edges (StructMember → Typedef) ---
        of_type_member = []
        for m in member_nodes:
            mtype_raw = (m.get("type") or "").strip().rstrip("*").strip()
            mtype_clean = mtype_raw.replace("const ", "").replace("volatile ", "").strip().rstrip("*").strip()
            if not mtype_clean:
                continue
            if mtype_clean in typedef_names:
                of_type_member.append({
                    "from_key": m["id"],
                    "to_key": f"TYPEDEF_{mtype_clean}",
                })

        self._merge_edges("OF_TYPE", "StructMember", "id", "Typedef", "id",
                          of_type_member)

        # --- USED_BY edges (Struct → Function) ---
        # A struct is "used by" a function if the function has a parameter of that struct type
        # Build param→function lookup from func_param_edges
        param_to_func = {e["to_key"]: e["from_key"] for e in func_param_edges}
        used_by_edges = []
        for e in param_struct:
            # e has from_key=PARAM_xxx, to_key=STRUCT_xxx
            func_id = param_to_func.get(e["from_key"])
            if func_id:
                used_by_edges.append({
                    "from_key": e["to_key"],  # STRUCT_xxx
                    "to_key": func_id,        # FUNC_xxx
                    "usage_context": "Parameter",
                })

        self._merge_edges("USED_BY", "Struct", "id", "Function", "id",
                          used_by_edges, ["usage_context"])

        # --- USED_IN edges (Typedef → Struct) ---
        # A typedef is "used in" a struct if any struct member type matches the typedef
        # Build member→struct lookup from struct_member_edges
        member_to_struct = {e["to_key"]: e["from_key"] for e in struct_member_edges}
        used_in_edges = []
        for m in member_nodes:
            mtype_raw = (m.get("type") or "").strip().rstrip("*").strip()
            mtype_clean = mtype_raw.replace("const ", "").replace("volatile ", "").strip().rstrip("*").strip()
            if mtype_clean in typedef_names:
                struct_id = member_to_struct.get(m["id"])
                if struct_id:
                    used_in_edges.append({
                        "from_key": f"TYPEDEF_{mtype_clean}",
                        "to_key": struct_id,
                        "member_name": m["name"],
                    })

        self._merge_edges("USED_IN", "Typedef", "id", "Struct", "id",
                          used_in_edges, ["member_name"])

        logger.info("SWA ingestion complete: %d functions, %d structs, "
                     "%d enums, %d typedefs, %d primitive types",
                     len(func_nodes), len(struct_nodes),
                     len(enum_nodes), len(typedef_nodes), len(primitive_nodes))

    # =====================================================================
    # 2. SFR ingestion (sfr_parser output)
    # =====================================================================

    def ingest_sfr(self, sfr_data: dict):
        """
        Ingest SFR parser output → Register + BitField nodes + HAS_BITFIELD edges.
        """
        if not sfr_data:
            logger.warning("No SFR data to ingest.")
            return

        logger.info("Ingesting SFR data …")
        registers_dict = sfr_data.get("registers", {})
        mod = self.module  # Always use pipeline module, not parser-derived

        reg_nodes = []
        bf_nodes = []
        reg_bf_edges = []

        for reg_name, bitfields in registers_dict.items():
            if not isinstance(bitfields, list):
                continue

            reg_id = f"REG_{reg_name}"
            reg_nodes.append({
                "register_name": reg_name,
                "module": mod,
            })

            for bf in bitfields:
                if not isinstance(bf, dict):
                    continue
                bfname = bf.get("name", "")
                if not bfname:
                    continue

                bfid = f"BITFIELD_{reg_name}_{bfname}"
                bf_nodes.append({
                    "id": bfid,
                    "name": bfname,
                    "bit_range": self._safe_str(bf.get("bit_range")),
                    "width": self._safe_str(bf.get("width")),
                    "description": self._safe_str(bf.get("description")),
                    "label": self._safe_str(bf.get("label") or f"{bfname} {bf.get('bit_range', '')}"),
                })
                reg_bf_edges.append({
                    "from_key": reg_name,
                    "to_key": bfid,
                })

        self._merge_nodes("Register", "register_name", reg_nodes)
        self._merge_nodes("BitField", "id", bf_nodes)
        self._merge_edges("HAS_BITFIELD", "Register", "register_name",
                          "BitField", "id", reg_bf_edges)

        logger.info("SFR ingestion complete: %d registers, %d bitfields",
                     len(reg_nodes), len(bf_nodes))

    # =====================================================================
    # 3. HW Spec ingestion (hw_spec_parser output)
    # =====================================================================

    def ingest_hw_spec(self, hw_data: dict):
        """
        Ingest HW spec parser output → HardwareRegister, RegisterField,
        Interrupt, Error nodes + HAS_FIELD edges.
        """
        if not hw_data:
            logger.warning("No HW spec data to ingest.")
            return

        logger.info("Ingesting HW spec data …")

        # --- HardwareRegister nodes ---
        hw_regs = hw_data.get("registers", [])
        hwreg_nodes = []
        for reg in hw_regs:
            if not isinstance(reg, dict):
                continue
            rname = reg.get("name", "")
            if not rname:
                continue
            hwreg_nodes.append({
                "id": f"HWREG_{rname}",
                "name": rname,
                "description": self._safe_str(reg.get("long_name")),
                "label": rname,
            })

        self._merge_nodes("HardwareRegister", "id", hwreg_nodes)

        # --- RegisterField nodes + HAS_FIELD edges ---
        fields = hw_data.get("fields", [])
        field_nodes = []
        has_field_edges = []
        for f in fields:
            if not isinstance(f, dict):
                continue
            fname = f.get("name", "")
            parent = f.get("parent_register", "")
            if not fname or not parent:
                continue

            fid = f"REGFIELD_{parent}_{fname}"
            field_nodes.append({
                "id": fid,
                "name": fname,
                "bits": self._safe_str(f.get("bits")),
                "access": self._safe_str(f.get("type")),
                "description": self._safe_str(f.get("description")),
                "register": parent,
                "label": f"{fname} [{f.get('bits', '')}]",
            })
            has_field_edges.append({
                "from_key": f"HWREG_{parent}",
                "to_key": fid,
            })

        self._merge_nodes("RegisterField", "id", field_nodes)
        self._merge_edges("HAS_FIELD", "HardwareRegister", "id",
                          "RegisterField", "id", has_field_edges)

        # --- Interrupt nodes ---
        interrupts = hw_data.get("interrupts", [])
        int_nodes = []
        for intr in interrupts:
            if not isinstance(intr, dict):
                continue
            iname = intr.get("name", "")
            if not iname:
                continue
            int_nodes.append({
                "id": f"INT_{iname}",
                "name": iname,
                "bit": self._safe_str(intr.get("bit")),
                "register": self._safe_str(intr.get("register")),
                "description": self._safe_str(intr.get("description")),
                "module": self.module,
                "label": iname,
            })

        self._merge_nodes("Interrupt", "id", int_nodes)

        # --- Error nodes ---
        errors = hw_data.get("errors", [])
        err_nodes = []
        for err in errors:
            if not isinstance(err, dict):
                continue
            ename = err.get("name", "")
            if not ename:
                continue
            err_nodes.append({
                "id": f"ERROR_{ename}",
                "name": ename,
                "type": self._safe_str(err.get("type")),
                "register": self._safe_str(err.get("detected_in")),
                "description": self._safe_str(err.get("description")),
                "module": self.module,
                "label": ename,
            })

        self._merge_nodes("Error", "id", err_nodes)

        # ----------------------------------------------------------------
        # Secondary HW-spec nodes derived from parser data
        # ----------------------------------------------------------------

        # --- AccessMode nodes + HAS_ACCESS_TYPE edges (RegisterField → AccessMode) ---
        access_nodes = []
        access_edges = []
        seen_access = set()

        ACCESS_DESCRIPTIONS = {
            "rw": "Read-Write",
            "r": "Read-Only",
            "w": "Write-Only",
            "rh": "Read-Hardware (cleared on read)",
            "wh": "Write-Hardware",
            "rwh": "Read-Write-Hardware",
        }

        for f in fields:
            if not isinstance(f, dict):
                continue
            fname_f = f.get("name", "")
            parent = f.get("parent_register", "")
            access_type = (f.get("type") or "").strip().lower()
            if not fname_f or not parent or not access_type:
                continue

            aid = f"ACCESS_{access_type}"
            if aid not in seen_access:
                seen_access.add(aid)
                access_nodes.append({
                    "id": aid,
                    "name": access_type,
                    "description": ACCESS_DESCRIPTIONS.get(access_type, access_type),
                    "label": access_type,
                })

            fid = f"REGFIELD_{parent}_{fname_f}"
            access_edges.append({
                "from_key": fid,
                "to_key": aid,
            })

        self._merge_nodes("AccessMode", "id", access_nodes)
        self._merge_edges("HAS_ACCESS_TYPE", "RegisterField", "id",
                          "AccessMode", "id", access_edges)

        # --- MemoryLocation nodes + LOCATED_AT edges (HardwareRegister → MemoryLocation) ---
        memloc_nodes = []
        located_edges = []
        seen_memloc = set()

        for reg in hw_regs:
            if not isinstance(reg, dict):
                continue
            rname = reg.get("name", "")
            offset = reg.get("offset", "")
            if not rname or not offset:
                continue

            mlid = f"MEMLOC_{rname}"
            if mlid not in seen_memloc:
                seen_memloc.add(mlid)
                memloc_nodes.append({
                    "id": mlid,
                    "name": f"{rname}_addr",
                    "address": offset,
                    "memory_type": "SFR",
                    "description": f"Memory location for register {rname} at offset {offset}",
                    "label": f"{rname} @ {offset}",
                })

            located_edges.append({
                "from_key": f"HWREG_{rname}",
                "to_key": mlid,
                "offset": offset,
            })

        self._merge_nodes("MemoryLocation", "id", memloc_nodes)
        self._merge_edges("LOCATED_AT", "HardwareRegister", "id",
                          "MemoryLocation", "id", located_edges, ["offset"])

        # --- Event nodes + INTERRUPT_TRIGGERS edges (Interrupt → Event) ---
        event_nodes = []
        int_event_edges = []
        seen_events = set()

        for intr in interrupts:
            if not isinstance(intr, dict):
                continue
            iname = intr.get("name", "")
            if not iname:
                continue
            # Derive event from interrupt name: e.g. TX_COMPLETE → TransmitEvent
            ename = iname.replace("_INT", "").replace("_IRQ", "")
            eid = f"EVENT_{ename}"
            if eid not in seen_events:
                seen_events.add(eid)
                category = "hardware"
                if any(kw in iname.upper() for kw in ("ERR", "FAULT")):
                    category = "error"
                event_nodes.append({
                    "id": eid,
                    "name": ename,
                    "category": category,
                    "label": ename,
                })
            int_event_edges.append({
                "from_key": f"INT_{iname}",
                "to_key": eid,
            })

        # Also derive events from hw_data relationships if available
        op_trig_int = (hw_data.get("relationships") or {}).get("operation_triggers_interrupt", [])
        for oti in op_trig_int:
            if not isinstance(oti, dict):
                continue
            op_name = oti.get("operation", "")
            int_name = oti.get("interrupt", "")
            if op_name:
                eid = f"EVENT_{op_name}"
                if eid not in seen_events:
                    seen_events.add(eid)
                    event_nodes.append({
                        "id": eid,
                        "name": op_name,
                        "category": "hardware",
                        "label": op_name,
                    })
                if int_name:
                    int_event_edges.append({
                        "from_key": f"INT_{int_name}",
                        "to_key": eid,
                    })

        self._merge_nodes("Event", "id", event_nodes)
        self._merge_edges("INTERRUPT_TRIGGERS", "Interrupt", "id",
                          "Event", "id", int_event_edges)

        # --- Mechanism nodes + DETECTED_BY edges (Error → Mechanism) ---
        mechanism_nodes = []
        detected_edges = []
        seen_mechanisms = set()

        for err in errors:
            if not isinstance(err, dict):
                continue
            ename_err = err.get("name", "")
            err_type = (err.get("type") or "").strip()
            detected_in = err.get("detected_in", "")
            if not ename_err:
                continue

            # Derive mechanism from error type / detection register
            if err_type:
                mech_name = f"{err_type}_detection"
            elif detected_in:
                mech_name = f"register_{detected_in}_detection"
            else:
                mech_name = "automatic_detection"

            mid = f"MECHANISM_{mech_name}"
            if mid not in seen_mechanisms:
                seen_mechanisms.add(mid)
                mechanism_nodes.append({
                    "id": mid,
                    "name": mech_name,
                    "category": "error_detection",
                    "label": mech_name,
                })

            detected_edges.append({
                "from_key": f"ERROR_{ename_err}",
                "to_key": mid,
                "detection_bitfield": detected_in,
            })

        self._merge_nodes("Mechanism", "id", mechanism_nodes)
        self._merge_edges("DETECTED_BY", "Error", "id",
                          "Mechanism", "id", detected_edges, ["detection_bitfield"])

        # --- Operation nodes + BITFIELD_CONTROLS edges (BitField → Operation) ---
        # Derived from field_enables_feature relationships in HW data
        operation_nodes = []
        bf_op_edges = []
        seen_operations = set()

        field_enables = (hw_data.get("relationships") or {}).get("field_enables_feature", [])
        for fe in field_enables:
            if not isinstance(fe, dict):
                continue
            feature = fe.get("feature", "")
            field_name = fe.get("field", "")
            register = fe.get("register", "")
            if not feature or not field_name:
                continue

            opid = f"OPERATION_{feature}"
            if opid not in seen_operations:
                seen_operations.add(opid)
                # Infer operation type from feature name
                op_type = "Configure"
                feat_lower = feature.lower()
                for kw, ot in [("enable", "Enable"), ("disable", "Disable"),
                               ("reset", "Reset"), ("trigger", "Trigger"),
                               ("transfer", "Transfer"), ("convert", "Convert")]:
                    if kw in feat_lower:
                        op_type = ot
                        break
                operation_nodes.append({
                    "id": opid,
                    "name": feature,
                    "description": fe.get("description", ""),
                    "operation_type": op_type,
                    "label": feature,
                    "module": self.module,
                })

            # Link bitfield → operation
            if register:
                bfid = f"BITFIELD_{register}_{field_name}"
                enable_val = fe.get("enable_value", "")
                bf_op_edges.append({
                    "from_key": bfid,
                    "to_key": opid,
                    "control_semantics": f"{enable_val}=Enable" if enable_val != "" else "",
                })

        # Also derive operations from bitfield descriptions that imply enable/disable
        # For bitfields without explicit field_enables_feature, we use naming heuristics
        for f in fields:
            if not isinstance(f, dict):
                continue
            fname_f = f.get("name", "")
            parent = f.get("parent_register", "")
            desc = (f.get("description") or "").lower()
            if not fname_f:
                continue
            # Heuristic: fields named *_EN, *_DIS, *_CLR, *_SET imply operations
            for suffix, op_type in [("_EN", "Enable"), ("_DIS", "Disable"),
                                     ("_CLR", "Reset"), ("_SET", "Configure"),
                                     ("_TRIG", "Trigger")]:
                if fname_f.upper().endswith(suffix):
                    opid = f"OPERATION_{fname_f}"
                    if opid not in seen_operations:
                        seen_operations.add(opid)
                        operation_nodes.append({
                            "id": opid,
                            "name": fname_f,
                            "description": f.get("description", ""),
                            "operation_type": op_type,
                            "label": fname_f,
                            "module": self.module,
                        })
                    bfid_key = f"BITFIELD_{parent}_{fname_f}" if parent else None
                    if bfid_key:
                        bf_op_edges.append({
                            "from_key": bfid_key,
                            "to_key": opid,
                            "control_semantics": f"1={op_type}",
                        })
                    break  # Only match first suffix

        self._merge_nodes("Operation", "id", operation_nodes)
        self._merge_edges("BITFIELD_CONTROLS", "BitField", "id",
                          "Operation", "id", bf_op_edges, ["control_semantics"])

        logger.info("HW spec ingestion complete: %d HW registers, %d fields, "
                     "%d interrupts, %d errors, %d access modes, "
                     "%d memory locations, %d events, %d mechanisms, %d operations",
                     len(hwreg_nodes), len(field_nodes),
                     len(int_nodes), len(err_nodes), len(access_nodes),
                     len(memloc_nodes), len(event_nodes),
                     len(mechanism_nodes), len(operation_nodes))

    # =====================================================================
    # 4. Requirements ingestion (JamaConnector output)
    # =====================================================================

    def ingest_requirements(self, requirements: list):
        """
        Ingest Jama requirements (list of JamaItem-like dicts or objects)
        → Requirement nodes.
        """
        if not requirements:
            logger.warning("No requirements to ingest.")
            return

        logger.info("Ingesting %d requirements …", len(requirements))
        req_nodes = []

        for req in requirements:
            # Support both dict and JamaItem dataclass
            if hasattr(req, "document_key"):
                # JamaItem object
                req_id = req.document_key or f"REQ_{req.id}"
                req_nodes.append({
                    "requirement_id": req_id,
                    "global_id": str(req.id),
                    "name": req.name,
                    "description": req.description,
                    "status": self._safe_str(getattr(req, "status", None)),
                    "last_modified": self._safe_str(req.modified_date),
                })
            elif isinstance(req, dict):
                req_id = req.get("document_key") or req.get("requirement_id") or f"REQ_{req.get('id', '')}"
                req_nodes.append({
                    "requirement_id": req_id,
                    "global_id": self._safe_str(req.get("id")),
                    "name": req.get("name", ""),
                    "description": req.get("description", ""),
                    "status": self._safe_str(req.get("status")),
                    "last_modified": self._safe_str(req.get("modified_date")),
                })

        self._merge_nodes("Requirement", "requirement_id", req_nodes)
        logger.info("Requirements ingestion complete: %d nodes", len(req_nodes))

    # =====================================================================
    # 5. Source Code ingestion (c_parser output)
    # =====================================================================

    def ingest_source(self, c_data: dict, source_file: str = None):
        """
        Ingest C parser output → enrich Function nodes with call graph.

        Creates:
        - CALLS_INTERNALLY edges (Function → Function)

        *source_file* — the originating .c filename for traceability.
        """
        if not c_data:
            logger.warning("No C source data to ingest.")
            return

        logger.info("Ingesting C source analysis (file=%s) …",
                    source_file or "default")
        functions = c_data.get("functions", {})

        call_edges = []

        for func_name, func_info in functions.items():
            if not isinstance(func_info, dict):
                continue
            fid = f"FUNC_{func_name}"

            # Internal calls
            internal_calls = func_info.get("internal_calls") or []
            for call in internal_calls:
                if isinstance(call, dict):
                    callee = call.get("function", "")
                    if callee:
                        call_edges.append({
                            "from_key": fid,
                            "to_key": f"FUNC_{callee}",
                            "call_site_line": call.get("line"),
                        })

        self._merge_edges("CALLS_INTERNALLY", "Function", "id", "Function", "id",
                          call_edges, ["call_site_line"])

        logger.info("Source ingestion complete: %d call edges", len(call_edges))

    # =====================================================================
    # 6. PlantUML ingestion (puml_parser output)
    # =====================================================================

    def ingest_puml(self, puml_data: dict):
        """
        Ingest PUML parser output → enrich existing Function nodes with
        sequence-diagram metadata.  No separate SequenceDiagram label is
        created (not present in the live Neo4j schema).
        """
        if not puml_data:
            logger.warning("No PUML data to ingest.")
            return

        logger.info("Ingesting PUML pattern library …")

        core = puml_data.get("core_functions", {})
        phases = puml_data.get("phase_patterns", {})

        # Attach sequence-pattern metadata to each Function node that
        # appears in the PlantUML diagrams.
        all_participants = (
            (core.get("always_present") or [])
            + (core.get("frequently_present") or [])
        )
        enriched = 0
        for participant in all_participants:
            if not participant or not isinstance(participant, str):
                continue
            fid = f"FUNC_{participant}"
            cypher = (
                "MATCH (f:Function {id: $fid}) "
                "SET f.puml_participant = true, "
                "    f.puml_phases = $phases"
            )
            self._write(cypher, {
                "fid": fid,
                "phases": self._serialize_complex(phases),
            })
            enriched += 1

        self.stats["puml_enriched_functions"] += enriched
        logger.info("PUML ingestion complete: enriched %d function nodes", enriched)

    # =====================================================================
    # 8. Cross-source relationships
    # =====================================================================

    def create_cross_source_relationships(self):
        """
        Create relationships that span multiple data sources.

        Only creates relationship types that exist in the live Neo4j DB:
        - IMPLEMENTS (Function → Requirement) via naming/trace heuristics
        - IMPLEMENTED_BY (Requirement → Function) – reverse of IMPLEMENTS
        - RELATES_TO (Requirement → Requirement) via shared keywords
        - INDICATES (EnumValue → Condition) – derive Condition nodes
        - HAS_CASE (Function → EnumValue) via switch-case in C source
        """
        logger.info("Creating cross-source relationships …")

        # --- IMPLEMENTS + IMPLEMENTED_BY ---
        # Match functions to requirements via \\trace{} references stored on functions
        # or via Jama requirement IDs that appear in function names/descriptions
        implements_cypher = (
            "MATCH (f:Function) WHERE f.module = $module "
            "MATCH (r:Requirement) "
            "WHERE f.name CONTAINS r.name OR r.description CONTAINS f.name "
            "MERGE (f)-[:IMPLEMENTS]->(r) "
            "MERGE (r)-[:IMPLEMENTED_BY]->(f)"
        )
        self._write(implements_cypher, {"module": self.module})

        # Count IMPLEMENTS
        impl_count = self._read(
            "MATCH (f:Function)-[:IMPLEMENTS]->(r:Requirement) "
            "WHERE f.module = $module RETURN count(*) AS cnt",
            {"module": self.module},
        )
        cnt_impl = impl_count[0]["cnt"] if impl_count else 0
        self.stats["rel:IMPLEMENTS"] += cnt_impl
        self.stats["rel:IMPLEMENTED_BY"] += cnt_impl

        # --- RELATES_TO (Requirement → Requirement) ---
        # Link requirements that share similar names (common keyword overlap)
        relates_cypher = (
            "MATCH (r1:Requirement), (r2:Requirement) "
            "WHERE r1.requirement_id < r2.requirement_id "
            "AND r1.name IS NOT NULL AND r2.name IS NOT NULL "
            "AND (r1.description CONTAINS r2.name OR r2.description CONTAINS r1.name) "
            "MERGE (r1)-[:RELATES_TO]->(r2)"
        )
        self._write(relates_cypher)
        rel_count = self._read(
            "MATCH (:Requirement)-[r:RELATES_TO]->(:Requirement) RETURN count(r) AS cnt"
        )
        self.stats["rel:RELATES_TO"] += (rel_count[0]["cnt"] if rel_count else 0)

        # --- INDICATES (EnumValue → Condition) ---
        # Derive Condition nodes from enum values that represent status conditions
        # Heuristics: enum values containing SUCCESS, FAIL, ERROR, OK, NOK, BUSY, IDLE, etc.
        STATUS_KEYWORDS = {
            "OK": ("OperationSuccess", "status"),
            "SUCCESS": ("OperationSuccess", "status"),
            "NOK": ("OperationFailure", "error"),
            "NOT_OK": ("OperationFailure", "error"),
            "FAIL": ("OperationFailure", "error"),
            "ERROR": ("ErrorOccurred", "error"),
            "BUSY": ("DeviceBusy", "status"),
            "IDLE": ("DeviceIdle", "status"),
            "TIMEOUT": ("TimeoutExpired", "error"),
            "OVERFLOW": ("BufferOverflow", "error"),
            "UNDERFLOW": ("BufferUnderflow", "error"),
            "COMPLETE": ("TransferComplete", "status"),
            "DONE": ("OperationDone", "status"),
            "READY": ("DeviceReady", "status"),
            "PENDING": ("OperationPending", "status"),
        }

        # Read enum values from DB
        enum_vals = self._read(
            "MATCH (ev:EnumValue) RETURN ev.id AS id, ev.name AS name"
        )

        condition_nodes = []
        indicates_edges = []
        seen_conditions = set()

        for ev in enum_vals:
            ev_name = ev.get("name", "")
            ev_id = ev.get("id", "")
            if not ev_name or not ev_id:
                continue
            ev_upper = ev_name.upper()
            for keyword, (cond_name, category) in STATUS_KEYWORDS.items():
                if keyword in ev_upper:
                    cid = f"CONDITION_{cond_name}"
                    if cid not in seen_conditions:
                        seen_conditions.add(cid)
                        condition_nodes.append({
                            "id": cid,
                            "name": cond_name,
                            "category": category,
                            "label": cond_name,
                        })
                    indicates_edges.append({
                        "from_key": ev_id,
                        "to_key": cid,
                    })
                    break  # First match only

        self._merge_nodes("Condition", "id", condition_nodes)
        self._merge_edges("INDICATES", "EnumValue", "id",
                          "Condition", "id", indicates_edges)

        # --- HAS_CASE (Function → EnumValue) ---
        # If C source analysis found switch-case patterns, link functions to enum values
        # Also use heuristic: enum values whose names start with CALLS_INTERNALLY source
        has_case_cypher = (
            "MATCH (ev:EnumValue)-[:CALLS_INTERNALLY]->(f:Function) "
            "WITH ev, f "
            "MATCH (f2:Function) WHERE f2.module = $module "
            "AND EXISTS { MATCH (f2)-[:DEPENDS_ON]->(f) } "
            "MERGE (f2)-[:HAS_CASE]->(ev)"
        )
        self._write(has_case_cypher, {"module": self.module})

        logger.info("Cross-source relationships complete.")

    # =====================================================================
    # Summary
    # =====================================================================

    def print_summary(self):
        """Print build statistics."""
        print("\n" + "=" * 60)
        print(f"  ILLD KG BUILD COMPLETE – Module: {self.module}")
        print("=" * 60)

        node_stats = {k: v for k, v in self.stats.items() if k.startswith("nodes:")}
        if node_stats:
            print("\n  Nodes created/merged:")
            total_nodes = 0
            for k, v in sorted(node_stats.items()):
                label = k.split(":", 1)[1]
                print(f"    :{label:<30s}  {v:>6,d}")
                total_nodes += v
            print(f"    {'TOTAL':<31s}  {total_nodes:>6,d}")

        rel_stats = {k: v for k, v in self.stats.items() if k.startswith("rel:")}
        if rel_stats:
            print("\n  Relationships created:")
            total_rels = 0
            for k, v in sorted(rel_stats.items()):
                name = k.split(":", 1)[1]
                print(f"    :{name:<30s}  {v:>6,d}")
                total_rels += v
            print(f"    {'TOTAL':<31s}  {total_rels:>6,d}")

        if not self.dry_run:
            try:
                db_stats = self._read("MATCH (n) RETURN count(n) AS nodes")
                rel_count = self._read("MATCH ()-[r]->() RETURN count(r) AS rels")
                labels = self._read(
                    "CALL db.labels() YIELD label RETURN collect(label) AS labels"
                )
                print(f"\n  Database totals:")
                print(f"    Nodes        : {db_stats[0]['nodes']:,d}")
                print(f"    Relationships: {rel_count[0]['rels']:,d}")
                print(f"    Labels       : {', '.join(labels[0]['labels'])}")
            except Exception:
                pass

        print("=" * 60 + "\n")
        return dict(self.stats)
