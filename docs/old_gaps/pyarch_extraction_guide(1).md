# PyArch MCP — Data Extraction & Traceability Guide

Repeatable recipes for extracting MCAL module architecture data from `.qeax` EA models.

---

## 1. Setup

```
# 1. Open the model (always first)
mcp_ifxpyarch_open_model(qeax_path="C:\...\model.qeax")

# 2. Discover top-level structure
mcp_ifxpyarch_get_package_tree(package_id=0, depth=2)
```

Key package IDs (for `2.20.0_tc4xx_sw_mcal.qeax`):
| Package | ID |
|---|---|
| AURIX_3G | 6 |
| AURIX_3G_MCAL | 205 |
| Product (36 modules) | 224 |
| Stubs (external BSW) | 222 |

---

## 2. Capturing Module Inventory & Inter-Module Dependencies

### 2a. List all modules
```sql
SELECT p.Package_ID, p.Name
FROM t_package p WHERE p.Parent_ID = 224
ORDER BY p.Name
```

### 2b. Inter-module dependency edges (module → module)
```sql
WITH RECURSIVE
  module_pkgs AS (
    SELECT Package_ID AS mod_pkg_id, Name AS module_name
    FROM t_package WHERE Parent_ID = 224
  ),
  all_pkgs AS (
    SELECT mod_pkg_id, module_name, mod_pkg_id AS pkg_id FROM module_pkgs
    UNION ALL
    SELECT ap.mod_pkg_id, ap.module_name, p.Package_ID
    FROM t_package p JOIN all_pkgs ap ON p.Parent_ID = ap.pkg_id
  )
SELECT
  src_mp.module_name AS source_module,
  tgt_mp.module_name AS target_module,
  c.Connector_Type,
  COUNT(*) AS weight
FROM t_connector c
  JOIN t_object src ON c.Start_Object_ID = src.Object_ID
  JOIN t_object tgt ON c.End_Object_ID = tgt.Object_ID
  JOIN all_pkgs src_mp ON src.Package_ID = src_mp.pkg_id
  JOIN all_pkgs tgt_mp ON tgt.Package_ID = tgt_mp.pkg_id
WHERE src_mp.mod_pkg_id != tgt_mp.mod_pkg_id
GROUP BY src_mp.module_name, tgt_mp.module_name, c.Connector_Type
ORDER BY weight DESC
```

### 2c. Module → Stub (external BSW) dependencies
Same query but with `tgt_mp` scoped to stubs (Parent_ID = 222).

### 2d. Internal complexity (ControlFlow count per module)
```sql
-- count of ControlFlow connectors per module = proxy for internal complexity
SELECT mp.module_name, COUNT(*) AS control_flows
FROM t_connector c
  JOIN t_object src ON c.Start_Object_ID = src.Object_ID
  JOIN all_pkgs mp ON src.Package_ID = mp.pkg_id
WHERE c.Connector_Type = 'ControlFlow'
GROUP BY mp.module_name ORDER BY control_flows DESC
```

---

## 3. Capturing Internal Module Architecture

### 3a. All API + local functions per module
```sql
SELECT mp.module_name, o.Name AS func_name, o.Stereotype
FROM t_object o JOIN all_pkgs mp ON o.Package_ID = mp.pkg_id
WHERE o.Object_Type = 'Interface'
  AND o.Stereotype IN ('generic_interface', 'local_function_interface')
ORDER BY mp.module_name, o.Stereotype, o.Name
```
- `generic_interface` = public API functions
- `local_function_interface` = private/local functions (`_l` prefix)

### 3b. Error codes per module
```sql
SELECT mp.module_name, o.Name FROM t_object o
JOIN all_pkgs mp ON o.Package_ID = mp.pkg_id
WHERE o.Stereotype = 'error_code'
ORDER BY mp.module_name, o.Name
```

### 3c. Global variables per module
```sql
SELECT mp.module_name, o.Name, o2.Name AS type_name
FROM t_object o
  JOIN all_pkgs mp ON o.Package_ID = mp.pkg_id
  LEFT JOIN t_object o2 ON o.Classifier = o2.Object_ID
WHERE o.Stereotype = 'global_variable'
ORDER BY mp.module_name
```

### 3d. Internal edges: function → error/global/critical_section
```sql
SELECT mp.module_name, src.Name, src.Stereotype,
       tgt.Name, tgt.Stereotype, c.Connector_Type
FROM t_connector c
  JOIN t_object src ON c.Start_Object_ID = src.Object_ID
  JOIN t_object tgt ON c.End_Object_ID = tgt.Object_ID
  JOIN all_pkgs mp ON src.Package_ID = mp.pkg_id
WHERE tgt.Package_ID IN (
    SELECT pkg_id FROM all_pkgs WHERE mod_pkg_id = mp.mod_pkg_id
  )
  AND src.Stereotype IN ('generic_interface', 'local_function_interface')
  AND tgt.Stereotype IN ('error_code', 'global_variable', 'critical_section')
  AND c.Connector_Type IN ('Dependency', 'Usage')
```

### 3e. Using process_request (high-level, no SQL)
```python
# Full component report (runs 15 internal tools in parallel)
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="component_report"
)

# Just local functions, globals, macros
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="component_internals"
)

# Just API docs with full parameter details
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="api_documentation"
)
```

---

## 4. Traceability

### 4a. Implements chains (requirement → design element)
```python
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="traceability"
)
```
Returns requirement-to-implementation trace links.

### 4b. Realisation connectors (design → interface)
`function_design` → `generic_interface` Realisation links show which design elements implement which API interfaces.
```sql
SELECT src.Name AS design_element, tgt.Name AS api_function
FROM t_connector c
  JOIN t_object src ON c.Start_Object_ID = src.Object_ID
  JOIN t_object tgt ON c.End_Object_ID = tgt.Object_ID
  JOIN all_pkgs mp ON src.Package_ID = mp.pkg_id
WHERE c.Connector_Type = 'Realisation'
  AND src.Stereotype = 'function_design'
  AND tgt.Stereotype = 'generic_interface'
ORDER BY mp.module_name, src.Name
```

### 4c. Design decisions and their coverage
```python
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="design_decisions"
)
```
Returns design decisions with `cover_tag` traceability.

### 4d. Safety / ASIL traceability
```python
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="safety_analysis"
)
```
Returns ASIL levels, Assumptions of Use, safety measures.

### 4e. Full traceability chain via SQL
```sql
-- requirement (Class/UseCase) → implements → design element → realises → interface
-- Step 1: Find requirement elements
SELECT o.Name, o.Stereotype, o.Object_Type
FROM t_object o JOIN all_pkgs mp ON o.Package_ID = mp.pkg_id
WHERE o.Stereotype IN ('requirement', 'solution', 'information', 'context')
  AND mp.module_name = 'Adc'

-- Step 2: Follow implements/covers connectors
SELECT src.Name AS from_element, src.Stereotype AS from_type,
       tgt.Name AS to_element, tgt.Stereotype AS to_type,
       c.Connector_Type, c.Stereotype AS link_type
FROM t_connector c
  JOIN t_object src ON c.Start_Object_ID = src.Object_ID
  JOIN t_object tgt ON c.End_Object_ID = tgt.Object_ID
  JOIN all_pkgs mp ON src.Package_ID = mp.pkg_id
WHERE c.Stereotype IN ('implements', 'covers', 'trace')
  AND mp.module_name = 'Adc'
ORDER BY src.Name
```

### 4f. File traceability (which functions go in which .c/.h files)
```python
mcp_ifxpyarch_process_request(
    component_name="Adc",
    qeax_path="...",
    task_type="file_analysis"
)
```
Returns file structure with includes and which elements map to which source files.

---

## 5. Quick Reference — All process_request Task Types

| Task Type | What It Returns |
|---|---|
| `component_report` | Full report (runs all 15 tools) |
| `api_documentation` | API functions with params, ASIL, service IDs |
| `component_internals` | Local functions, globals, macros, collections |
| `config_analysis` | ECUC configuration parameters |
| `safety_analysis` | ASIL levels, AoUs, safety measures |
| `error_analysis` | Error codes and DET reporting flows |
| `dependency_analysis` | Dependencies and includes |
| `file_analysis` | File structure and source mappings |
| `design_decisions` | Design decisions with cover tags |
| `hsi_analysis` | Register access and HW interfaces |
| `traceability` | Requirement → implementation chains |

---

## 6. Key EA Model Stereotypes

| Stereotype | Object_Type | Meaning |
|---|---|---|
| `generic_interface` | Interface | Public API function |
| `local_function_interface` | Interface | Private local function |
| `function_design` | Class | Design-level function description |
| `error_code` | Class | DET/DEM error code |
| `global_variable` | Object | Module-level global variable |
| `critical_section` | Class | SchM exclusive area |
| `design_decision` | Class | Architectural decision |
| `rationale` | Class | Decision rationale |
| `cover_tag` | Class | Traceability coverage tag |
| `information` | Class | Design information note |
| `config_macros` | Class | Configuration macro |
| `ifx_config_parameter` | Object | ECUC config parameter |
| `module` | Component | Top-level MCAL module |
| `plugin` | Artifact | Source file (.c / .h) |

## 7. Important Notes

- **Always scope SQL with Package_ID IN (...)** — never query the entire model unscoped.
- **Use recursive CTEs** to collect all descendant packages of a module.
- **`process_request` is the easiest path** — it orchestrates multiple tools in parallel and returns structured JSON.
- **For ad-hoc exploration**, use `get_package_tree`, `search_elements`, `get_element`, `get_connectors`.
- **For raw SQL**, call `get_db_schema` first to discover exact column names.
- **Connector types that matter**: Dependency (62K), ControlFlow (42K), Realisation (10K), Association (14K), Sequence (5K), Aggregation (8K).
