# GraphRAG Evaluation Queries — SWA ↔ SWUD ↔ TestSpec Traceability

These 4 queries are designed to stress-test the GraphRAG system's ability to retrieve,
fuse, and reason across the SWA (Software Architecture), SWUD (Software Unit Design),
and Test Specification documents for the **Port** MCAL module. Each query targets a
different capability and includes ground-truth expected answers derived directly from the
source documents, enabling objective scoring of the LLM response.

---

## Query 1 — End-to-End Traceability Chain (SWA → SWUD → TS)

### Query

```
Trace the full lifecycle of the Port wakeup feature from architecture through design to
test verification. Specifically:
1. Which SWA architectural decision defines the wakeup status interface, and what was the
   rationale for choosing PortId + PinNum as inputs over PinId alone?
2. How does the SWUD function Port_GetWakeUpStatus implement the wakeup re-enable
   mechanism, and which HW registers does it access?
3. What safety Assumption of Use (AoU) applies to the wakeup status, and why is the
   wakeup signal considered untrusted?
4. Which product requirements (AU3GM-PRQ-*) tie the SWA decision, the SWUD
   implementation, and the safety AoU together?
```

### Expected Answer (Ground Truth)

1. **SWA Architectural Decision**: "Port: Wakeup Status of Pin" (featureID `{696721EE-0C07-49af-9F7C-95E9D9AB332F}`). Two alternatives were evaluated:
   - *Alternative 1*: `Port_GetWakeUpStatus(PinId)` — single PinId input.
   - *Alternative 2*: `Port_GetWakeUpStatus(PortId, PinNum)` — PortId + PinNum.
   - **Rationale for choosing Alternative 2**: "Integrator can get the information of Pin wakeup status." Using PortId+PinNum aligns with the Port-level granularity decision and allows the caller to query wakeup per physical port/pin combination.

2. **SWUD Port_GetWakeUpStatus** (featureID `{79B75B62-D7C2-4D14-8EFE-810A40C1BB52}`, Service ID 0x06, ASIL D, Reentrant):
   - Algorithm: Get partition index → dev/safety error check → get port config address → get port number → **if PORT_WAKEUP_REENABLE is enabled, re-enable the wakeup feature by writing to `P_WKEN(rw)`** → read wakeup status from `P_WKSTS(r)` → if triggered, capture status → return `PORT_PIN_WAKEUP_TRIGGERED` or `PORT_PIN_WAKEUP_NOT_TRIGGERED`.
   - **HW registers**: `P_WKEN(rw)` (wakeup enable — read-write) and `P_WKSTS(r)` (wakeup status — read-only). Both are marked as untrusted sources.
   - Error codes: `PORT_E_PARAM_PIN` (0x0A), `PORT_E_PARAM_PORT` (0x67), `PORT_E_UNAUTH_PARTITION` (0x97), `PORT_E_UNINIT` (0x0F).

3. **Safety AoU**: "Port: Authenticate Wakeup status are trusted" (featureID `{50270B53-95FB-4c10-A3D9-F408A7A3F3F6}`, parentID `AU3GM-PRQ-40811`). Exact text: *"The wakeup can be triggered from an untrusted domain. As a result, the provided status of wakeup from Port driver are untrusted. User shall authenticate and check the wakeup status."* Rationale: *"Trigger from an untrusted domain tamper wakeup signal and leads unintended behaviour."* The HSI constituents `P_WKEN` and `P_WKSTS` are classified as *Untrusted (Dependent peripheral — Wakeup configurations)* and *Untrusted (External Signals — Hardware trigger event)*.

4. **Unifying PRQ**: **AU3GM-PRQ-40811** is the single requirement that ties all three together:
   - SWA: parentID of the Wakeup Status architectural decision and related config params (`PortGetWakeUpStatusApi`, `PORT_WAKEUP_REENABLE`, `PortPinWakeUpEnable`).
   - SWUD: Referenced in Port_GetWakeUpStatus algorithm steps (steps 3 and 5).
   - Safety AoU: parentID of the "Authenticate Wakeup status" assumption.
   - Additional shared PRQs: `AU3GM-PRQ-29892` (dev error check), `AU3GM-PRQ-29893` (error status E_OK check), `AU3GM-PRQ-37898` (safety error).

### What This Tests

| Capability | Stress Point |
|---|---|
| **Multi-hop graph traversal** | Must follow `SWA_ArchitecturalDecision → ProductRequirement → SWUD_Function → Safety AoU` chain |
| **Vector + graph fusion** | Wakeup details span SWA markdown, SWUD markdown, and KG nodes |
| **Traceability completeness** | Must surface AU3GM-PRQ-40811 as the common thread |
| **Safety awareness** | Must identify untrusted domain classification of HW registers |

---

## Query 2 — Cross-Document Error Handling Consistency

### Query

```
For the Port_SetPinDirection API:
1. List every development and safety error code it can raise, including the hex value,
   the triggering condition, and the PRQ that mandates each error.
2. Describe the critical-section behavior when changing pin direction from Input to
   Output — specifically, how many critical section enter/exit pairs are needed, and why
   does it differ from the Output-to-Input case?
3. Which SWA safety measure protects against invalid PortId, and how does the SWUD
   error-checking algorithm implement that protection?
4. What is the maximum allowed execution time for Port_SetPinDirection, and under what
   operating conditions does that constraint apply?
```

### Expected Answer (Ground Truth)

1. **Error codes for Port_SetPinDirection**:

   | Error Code | Hex | Trigger Condition | PRQ |
   |---|---|---|---|
   | `PORT_E_PARAM_PIN` | `0x0A` | Pin ID out of valid range | AU3GM-PRQ-31484 |
   | `PORT_E_PARAM_INVALID_CURRENT_MODE` | `0x65` | Current pin mode is invalid for direction change | — |
   | `PORT_E_UNAUTH_PARTITION` | `0x97` | Caller's partition is not authorized | — |
   | `PORT_E_DIRECTION_UNCHANGEABLE` | `0x0B` | Pin configured as direction-not-changeable | AU3GM-PRQ-31485 |
   | `PORT_E_UNINIT` | `0x0F` | Port driver not initialized | AU3GM-PRQ-31489 |
   | `PORT_E_PARAM_INVALID_DIRECTION` | `0x64` | Direction parameter is not PORT_PIN_IN or PORT_PIN_OUT | — |

2. **Critical section behavior** (Input→Output vs Output→Input):
   - **Input→Output**: Requires up to **two** critical-section pairs. First, if the current mode is *not* GPIO, the driver must enter a critical section (`SchM_Enter_Port_ProtWriteSequence`), write both direction **and** mode (force GPIO) via `PORT_SFR_OS_RUNTIME_PROT_WRITE_SEQ`, then exit. If the mode is already GPIO, it enters a single critical section, updates direction only, then exits. This is because changing to Output in a non-GPIO mode could cause unintended peripheral drive behavior, so mode must be forced to GPIO first.
   - **Output→Input**: Only **one** critical-section pair is needed — enter, update direction to Input, exit. No mode change is necessary because Input is safe regardless of the current mode.
   - PRQs: `AU3GM-PRQ-29873` and `AU3GM-PRQ-30140` govern SchM enter/exit critical section. `AU3GM-PRQ-31481` governs the actual register update.
   - HW registers: `P_PADCFG_DRVCFG(rw)`, `P_ACCGPR_PROTE(rw)`, `P_PADCFG_ACCEN(r)`.

3. **SWA safety measure**: "Port: Parameter Range Check for Port Identifier" (featureID `{B1C1587D-C694-4ad4-9788-509D38C290F9}`, parentID `{696721EE-0C07-49af-9F7C-95E9D9AB332F}`). Text: *"The Port API shall verify the range of the PortId input parameter. API shall report PORT_E_PARAM_PORT error in case PortId invalid."* Rationale: prevents memory access violation. The SWUD implements this via `Port_ICheckCommonErrors` which validates PortId/PinId range as the first step of the error-checking sequence before entering any critical section.

4. **Timing constraint**: Port_SetPinDirection must complete in **< 1,500 ns** (featureID `{AF2654CA-2B5A-4fc5-A9E0-61C150C88E36}`). Operating conditions: CPU/SRI clock = 400 MHz, SPB clock = 100 MHz, non-error scenarios, excluding SchM enter/exit and callback overhead.

### What This Tests

| Capability | Stress Point |
|---|---|
| **Factual precision** | Must match all 6 error codes with correct hex values |
| **Algorithmic reasoning** | Must explain the Input→Output vs Output→Input asymmetry |
| **SWA↔SWUD cross-linking** | Safety measure in SWA → error check in SWUD |
| **Timing constraint retrieval** | Must pull from a different SWA section (3.6) and include operating conditions |

---

## Query 3 — Configuration Traceability (SWA Config → SWUD Derived Macros)

### Query

```
Explain the full configuration chain for enabling/disabling Port APIs at compile time:
1. For each of the following SWA configuration parameters, identify the corresponding
   SWUD derived macro, its default value, and the PRQ that mandates the parameter:
   - PortSetPinDirectionApi
   - PortSetPinModeApi
   - PortSetPinCharacteristicsApi
   - PortInitCheckApi
   - PortGetWakeUpStatusApi
   - PortDevErrorDetect
   - PortSafetyErrorDetect
2. Which SWA configuration parameters map to BOTH PORT_MCAL_SUPERVISOR and
   PORT_MCAL_USER1 in the SWUD, and how does the PortInitApiMode vs
   PortRuntimeApiMode distinction affect supervisor/user mode SFR access?
3. How does the SWUD derive PORT_NO_OF_PARTITIONS from the SWA PortEcucPartitionRef,
   and what is the traceability chain (featureID → parentID) between them?
```

### Expected Answer (Ground Truth)

1. **API enable configuration chain**:

   | SWA Config Parameter | SWUD Derived Macro | Default | PRQ |
   |---|---|---|---|
   | `PortSetPinDirectionApi` | `PORT_SET_PIN_DIRECTION_API` | TRUE | AU3GM-PRQ-31521 |
   | `PortSetPinModeApi` | `PORT_SET_PIN_MODE_API` | TRUE | AU3GM-PRQ-31539 |
   | `PortSetPinCharacteristicsApi` | `PORT_SET_PIN_CHARACTERISTICS_API` | TRUE | AU3GM-PRQ-38177 |
   | `PortInitCheckApi` | `PORT_INIT_CHECK_API` | TRUE (rationale: "For ensuring safe initialization default value is made true") | — |
   | `PortGetWakeUpStatusApi` | `PORT_GET_WAKEUP_STATUS_API` | TRUE | — |
   | `PortDevErrorDetect` | `PORT_DEV_ERR_CHECK` + `PORT_DEV_ERR_REPORTING` | TRUE | AU3GM-PRQ-29861 |
   | `PortSafetyErrorDetect` | `PORT_SAFETY_ERR_REPORTING` | TRUE (rationale: "Detection of safety related errors is enabled by default to ensure that safety issues are addressed during the product lifecycle") | AU3GM-PRQ-29861 |

   The SWUD derived macro's `parentID` points back to the SWA configuration parameter's `featureID`, creating a direct traceability link. For example: SWUD `PORT_SET_PIN_MODE_API` (featureID `{F4DC8338-…}`) has parentID `{9BE9EEC7-…}` which is the featureID of SWA `PortSetPinModeApi`.

2. **Supervisor/User mode mapping**:
   - `PortInitApiMode` (SWA, featureID `{E304627D-…}`) → maps to `PORT_MCAL_SUPERVISOR` (SWUD, featureID `{F0DD3937-…}`) and `PORT_PRIVILEGE_MODE` (featureID `{5281426F-…}`). Controls supervisor-mode SFR access during **initialization** (Port_Init, Port_InitCheck).
   - `PortRuntimeApiMode` (SWA, featureID `{1EED7BEF-…}`) → maps to `PORT_RUNTIME_API_MODE` (featureID `{1927BB7A-…}`) and `PORT_MCAL_USER1` (featureID `{CFA103A0-…}`). Controls SFR access during **runtime** APIs (SetPinMode, SetPinDirection, etc.).
   - SWUD design decision "Port: SFR access in supervisor mode and User mode" (featureID `{ACF1EA68-8DDB-4283-8D4B-9BC4D490E88B}`) mandates using macros as wrappers — `PORT_SFR_INIT_WRITE32` for init-time supervisor-mode writes and `PORT_SFR_OS_RUNTIME_PROT_WRITE_SEQ` for runtime user-mode writes with OS protection.

3. **Partition count derivation**:
   - SWA `PortEcucPartitionRef` (featureID `{4A8D4213-…}`) defines the number of ECUC partitions.
   - SWUD `PORT_NO_OF_PARTITIONS` (featureID `{ED425C32-…}`) is derived from it — parentID `{4A8D4213-…}` links back to the SWA parameter.
   - The SWUD macro counts the number of referenced ECUC partitions to generate the compile-time constant used for partition array sizing.

### What This Tests

| Capability | Stress Point |
|---|---|
| **Config parameter retrieval** | Must match 7+ SWA→SWUD config pairs with correct defaults |
| **featureID→parentID chain traversal** | Must demonstrate the graph edge from SWUD node back to SWA node |
| **Contextual reasoning** | Must explain *why* supervisor vs user mode distinction matters |
| **Multi-source fusion** | Config data spans SWA section 3.1.7, SWUD section 3.2.1, and safety rationales |

---

## Query 4 — Safety, ASIL, and Completeness Verification

### Query

```
Evaluate the safety completeness of the Port module:
1. List all Assumptions of Use (AoU) defined in the SWA safety view and trusted view,
   including their featureIDs, the PRQs they trace to, and the exact rationale text.
2. All Port APIs are classified as ASIL D. The SWA states that certain ASIL B PRQs are
   "not applicable" because "Port module is ASIL D driver." List at least 5 specific
   AU3GM-PRQ IDs that are marked inapplicable for this reason.
3. What are the memory constraints for the Port module (RAM, Code ROM, Data ROM, Stack,
   CSA blocks), and which SWA featureID defines them?
4. For Port_Init, what is the complete sequence of safety-relevant checks: from NULL
   pointer validation of ConfigPtr through partition authorization to the final
   PORT_E_INIT_FAILED error — citing the specific PRQs for each step?
```

### Expected Answer (Ground Truth)

1. **All Assumptions of Use (AoU)**:

   | # | AoU Title | featureID | PRQ | Rationale |
   |---|---|---|---|---|
   | 1 | Port: Access group PROT state transition protection | `{81C0B183-FF48-4fce-95AC-0DBFF25567F9}` | AU3GM-PRQ-31525 | *"Application preempts one Port API from another Port API where pin accessed from both the APIs allocated to same access group then it will lead to undefined behaviour or functional failure."* |
   | 2 | Port: Generated symbolic names usage | `{78A39218-9429-4db1-9414-4031BBBB1FBF}` | AU3GM-PRQ-31498 | *"The generated symbolic name of Pin or Port embedded with information of mapping of Port and Pin number to the partition. Hence, use of generated symbolic name while invoking the API's is mandatory for correct functionality of the driver."* |
   | 3 | Port: Group access protection | `{1E094930-4600-4e7c-894A-3BF0F44B6F23}` | AU3GM-PRQ-38175, 31505, 31519, 31537, 31527 | *"To avoid memory access violation trap due to access of PROT SFRs without enabling Read-Write access for Port Access Groups."* |
   | 4 | Port: Authenticate Wakeup status are trusted | `{50270B53-95FB-4c10-A3D9-F408A7A3F3F6}` | AU3GM-PRQ-40811 | *"Trigger from an untrusted domain tamper wakeup signal and leads unintended behaviour."* |

   Additionally, the safety measure (not an AoU but related): "Port: Parameter Range Check for Port Identifier" (`{B1C1587D-…}`, parent `{696721EE-…}`).

2. **Inapplicable ASIL B PRQs** (Port is ASIL D, so ASIL B PRQs do not apply):
   - `AU3GM-PRQ-41907`
   - `AU3GM-PRQ-40367`
   - `AU3GM-PRQ-40363`
   - `AU3GM-PRQ-40365`
   - `AU3GM-PRQ-41855`
   - `AU3GM-PRQ-40364`
   - `AU3GM-PRQ-40362`
   - `AU3GM-PRQ-40366`
   - `AU3GM-PRQ-40532`
   - `AU3GM-PRQ-37662`
   - (Plus 11 more: PRQ-40371, 41182, 40361, 40504, 40381, 29917, 40356, 40355, 40378, 40354, 40357)

3. **Memory constraints** (featureID `{45EF83D4-751D-41a6-8923-48B2975F0511}`):

   | Resource | Constraint |
   |---|---|
   | RAM | < 40 Bytes |
   | Code ROM | < 4 KB |
   | Data ROM | < 3 KB |
   | Stack | < 32 Bytes per API |
   | CSA blocks | ≤ 3 blocks per API |

4. **Port_Init safety check sequence**:

   | Step | Check | Error Code | PRQ |
   |---|---|---|---|
   | 1 | Get partition index from current execution context | — | — |
   | 2 | If dev/safety error detection enabled: validate ConfigPtr is not NULL | `PORT_E_INCORRECT_CFG_POINTER` (0x98) | AU3GM-PRQ-40353 |
   | 3 | Validate caller partition is authorized | `PORT_E_UNAUTH_PARTITION` (0x97) | AU3GM-PRQ-40389 |
   | 4 | Check general init errors (module already initialized, etc.) | `PORT_E_INIT_FAILED` (0x0C) | AU3GM-PRQ-31486 |
   | 5 | If all checks pass (E_OK): initialize port-level registers | — | AU3GM-PRQ-31512, AU3GM-PRQ-31514 |
   | 6 | Initialize pin-level registers | — | AU3GM-PRQ-31510 |
   | 7 | Store config pointer to global RAM | — | AU3GM-PRQ-29928, AU3GM-PRQ-31509 |
   | 8 | Dev/safety error checks governed by | — | AU3GM-PRQ-29892, AU3GM-PRQ-29893, AU3GM-PRQ-31515, AU3GM-PRQ-37898, AU3GM-PRQ-41906 |

### What This Tests

| Capability | Stress Point |
|---|---|
| **Exhaustive retrieval** | Must list ALL 4 AoUs — missing one = incomplete safety coverage |
| **Negative knowledge** | Must correctly identify inapplicable requirements and why |
| **Numeric precision** | Memory constraints must be exact (40B RAM, not 48B) |
| **Sequential reasoning** | Port_Init safety flow must be in correct order with correct error codes |

---

## Scoring Rubric

For each query, score the GraphRAG response on these dimensions (1–5 scale each):

| Dimension | 1 (Poor) | 3 (Adequate) | 5 (Excellent) |
|---|---|---|---|
| **Completeness** | Missing >50% of expected items | Has most items, misses 1-2 | All expected items present |
| **Accuracy** | Multiple factual errors (wrong hex codes, wrong PRQs) | Minor errors in details | All facts match ground truth |
| **Traceability** | No cross-document links shown | Some links but incomplete chains | Full featureID→parentID→PRQ chains |
| **Source Attribution** | No sources cited | Vague references ("SWA says...") | Specific section/featureID citations |
| **Reasoning Quality** | Just lists facts | Explains "what" but not "why" | Explains rationale and implications |

**Total possible score per query**: 25 points  
**Total possible score**: 100 points  
**Passing threshold**: 70 points (system provides usable, mostly-correct answers)  
**Excellence threshold**: 90 points (system rivals expert human review)
