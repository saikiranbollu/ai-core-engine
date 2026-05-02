# FOSS License Compliance for AI-Generated Content

**Document ID**: AICE-GOV-009
**Version**: 1.0.0
**Classification**: Internal — Infineon Technologies
**Owner**: FOSS Compliance Officer (to be assigned) + AI Governance Lead
**Last Updated**: 2026-04-20
**Applies to**: Zone B (`illd` reference SW) and Zone C (`foss-bsp` Zephyr/NuttX BSP) — as defined in AICE_SYSTEM_CARD v3.0.0 §3.3

---

## 1. Purpose

This document defines controls that prevent **FOSS license contamination** and **copyright infringement** in AI-generated content targeting iLLD reference software and Zephyr / NuttX BSP work. It covers:

1. The risk landscape (why this matters more for Zone B/C than Zone A)
2. Mandatory controls (Copilot settings, license scanners, DCO workflow, SBOM)
3. Tooling options and selection guidance (FOSSology, ScanCode, Snyk, Black Duck)
4. Roles and operational workflow
5. Upstream contribution governance (Zephyr, NuttX specifics)

**This document does NOT cover** general FOSS compliance for Infineon products (that's owned by the existing Infineon FOSS program at infineon.com/foss). It covers only the AI-specific delta.

---

## 2. Why This Matters

### 2.1 The specific risk

GitHub Copilot is trained on public code including strong-copyleft repositories (GPL, AGPL, LGPL). A Copilot suggestion can closely mirror training examples. The "close mirror" doesn't have to be verbatim — copyright covers substantial similarity. GitHub itself acknowledges ~1% of Copilot completions match public code when the "Block matching public code" setting is OFF.

Three scenarios that are real exposure for Infineon:

| Scenario | Exposure |
|---|---|
| GPL-derived snippet lands in **iLLD** (Infineon Free License, permissive) | Either (a) inadvertent copyleft taint obligating source disclosure for entire iLLD, or (b) copyright infringement claim from the original GPL author. Both are publicly visible because iLLD ships on GitHub |
| AGPL-derived snippet in iLLD network-service example | Worse — AGPL's network trigger affects downstream users |
| LGPL-derived snippet in a **Zephyr BSP upstream contribution** | Upstream maintainer rejects PR; Infineon engineer's name on a rejected PR; in worst case, if merged before detection, upstream project files a takedown |
| Apache-2.0-derived snippet contributed back as Apache-2.0 content (Zephyr) | Usually fine, but still requires attribution preservation |

### 2.2 Why Zone B/C is higher-risk than Zone A

| Factor | Zone A (mcal) | Zone B (illd) | Zone C (foss-bsp) |
|---|---|---|---|
| Primary LLM | GPT4IFX (controlled corpus) | Copilot Enterprise permitted | Copilot Enterprise permitted |
| Output visibility | Confidential, delivered per NDA | **Public** (github.com/Infineon) | **Public** (upstream or downstream) |
| Downstream verification | MISRA + AUTOSAR + Bugfinder + CodeProver + MC/DC (dense) | MISRA advisory + Bugfinder (light) | FOSS-project-native (varies) |
| License-scanning in CI today | Not needed (closed source) | **Not in place** | **Not in place** |
| If contamination lands | Contained within NDA scope; may be caught by reviewers | Externally visible; customers and public download it | Externally visible; upstream maintainers inspect |

Net: Zone A is exposed mostly to functional-quality risk from AI; Zone B/C is exposed to **legal and reputational** risk from license contamination, on top of functional risk.

### 2.3 What happens downstream of iLLD

Published iLLD source is used by Infineon customers worldwide as starting material for their MCAL-equivalent drivers. If contaminated code reaches iLLD:
- Customers inherit the contamination when they integrate
- Each customer's legal team discovers it independently during their own FOSS audit
- Infineon's FOSS reputation (and brand) takes the hit
- Legal remediation cost is very high (each customer may demand audit support)

---

## 3. Mandatory Controls

All of the following are **mandatory** for Zone B and Zone C work. Failure to apply any one is a **Major** policy violation per AI_USAGE_POLICY §12.

### 3.1 Copilot Enterprise "Block matching public code" setting

| Requirement | Detail |
|---|---|
| Setting | GitHub Copilot Enterprise "Suggestions matching public code" = **BLOCKED** |
| Scope | **Organization-level policy** for Infineon GitHub Enterprise (not per-user) — so individual users cannot disable it |
| Verification | Periodic audit of the org-level GitHub policy; quarterly report in governance review |
| Failure mode | If setting is changed (by an admin, by mistake, or by GitHub default change), the governance monitoring must detect and restore within 24h |

**Technical note:** The setting filters completions matching public code ≥150 chars verbatim. It does NOT catch paraphrased matches, so it's necessary but not sufficient. License scanning (§3.2) covers the rest.

### 3.2 License/Copyright Scanner in CI

Every pull request touching `illd/` or `foss-bsp/` repositories that contains AI-assisted content (marked via git trailer — see AI_USAGE_POLICY §6.3) triggers an automated license scan.

**Scan output drives merge decision:**

| Scanner finding | Action |
|---|---|
| Match against strong-copyleft (GPL-3.0, AGPL-3.0) ≥30 lines | **Block merge.** Author must rewrite without AI or with a different prompt |
| Match against weak-copyleft (LGPL, MPL-2.0) ≥50 lines | **Block merge.** Same |
| Match against permissive-incompatible (Apache-2.0 with patent grant in GPL context) | **Block merge.** Legal review |
| Match against permissive-compatible (MIT, BSD-3, ISC, Apache-2.0 in Apache context) ≥100 lines | **Warn.** Preserve attribution; add NOTICE entry. Legal-sign-off before merge |
| Match against Infineon-owned content | Proceed |
| No match | Proceed |

**Scanning happens on the AI-authored delta only** (via git trailer metadata) — not on the entire PR, which reduces noise.

### 3.3 FOSS License Registry per iLLD / FOSS-BSP release

Each iLLD release tag and each `foss-bsp` release tag has an associated **FOSS License Registry** listing:

- All FOSS components included (CMSIS headers, third-party crypto, example dependencies, Zephyr subsystems, NuttX modules)
- License of each component
- Version pinned
- Source location / attribution URL
- NOTICE file generated automatically

Integrated with the AIBOM (DATA_GOVERNANCE_POLICY §10).

### 3.4 SBOM Generation

SPDX 2.3 or CycloneDX 1.5 (AI/ML extension) SBOM generated per release. Required fields:

| Field | Required |
|---|---|
| Component name, version, PURL | Yes |
| License identifier (SPDX) | Yes |
| Copyright holder | Yes |
| Source download URL | Yes |
| Hash (SHA-256) | Yes |
| AI-assisted authorship declaration (new field) | Yes for AI-generated components |

SBOM published with release; retained per DATA_GOVERNANCE_POLICY §6.

### 3.5 DCO (Developer Certificate of Origin) Sign-off Awareness (Zone C only)

Zephyr and NuttX upstream projects **both require DCO sign-off** on every contribution. Infineon engineers contributing AI-assisted code upstream must:

1. Read and understand DCO v1.1 text (developercertificate.org)
2. Verify — personally — that the AI-assisted code being contributed:
   - Was created by them (they directed the AI, reviewed the output, edited it where needed)
   - Does not contain third-party code except under a compatible license that they've identified
   - Is being contributed under the project's license
3. Sign off with `Signed-off-by: Full Name <email>` in the commit message

**Critical nuance:** AI tools like Copilot are **NOT authors** under DCO — the human directing the AI is the author. But the human is responsible for what the AI produces, including ensuring license compatibility. **AI-assisted ≠ AI-authored from a DCO perspective**; the human assertion covers AI-assisted content.

**Training requirement:** Every engineer contributing to Zephyr/NuttX upstream from Infineon completes a 1-hour DCO + AI training session before their first contribution.

### 3.6 CLA Review (Zone C only)

As of April 2026:
- **Zephyr Project:** uses DCO, no CLA required
- **NuttX (Apache Software Foundation):** uses ICLA (Individual CLA) or CCLA (Corporate CLA) for substantial contributions; DCO-style sign-off suffices for smaller ones

**Before Infineon's first substantial contribution to either project, Legal reviews:**
1. Whether a CCLA should be signed with ASF on behalf of Infineon (for NuttX)
2. What counts as "substantial" under ASF policy
3. Which internal approval is required

---

## 4. Tooling Options and Selection Guidance

### 4.1 Scanner tool comparison

| Tool | Type | Strengths | Weaknesses | Cost |
|---|---|---|---|---|
| **FOSSology** | Open-source | Industry standard; Linux Foundation project; CI integration well-documented; license and copyright extraction; REST API | Requires infra hosting; UI feels dated; scan speed moderate | Free (FLA-hosted / self-host) |
| **ScanCode Toolkit** | Open-source | Fast; excellent for CI; good for snippet detection; combined with ScanCode.io for server-side | Command-line focus; no built-in UI without ScanCode.io add-on | Free |
| **Snyk Open Source** | Commercial | Cloud-native; excellent GitHub integration; covers CVE + license together; dependency-graph intelligence | Cost; cloud-hosted may be a problem for Infineon-confidential repos (iLLD is public, so this is less of an issue) | Per-developer subscription |
| **Black Duck (Synopsys)** | Commercial | Most comprehensive knowledge base; used heavily in automotive supply chain; good audit trail | Expensive; heavyweight | Enterprise license |
| **Sonatype Lifecycle** | Commercial | Strong policy engine; Nexus integration; supply-chain focus | Primarily dependency-level, less snippet-level | Enterprise license |
| **GitHub Dependency Review + Licenses** | SaaS | Native GitHub integration; zero ops | Dependency-level only, no snippet detection | Included with GitHub Enterprise |

### 4.2 Recommended selection for Infineon

**Hybrid approach:**

1. **FOSSology or ScanCode Toolkit** for snippet-level matching on AI-touched deltas in CI (primary control) — open-source, can run on-prem, works on public iLLD repos
2. **Black Duck** at the Infineon portfolio level for comprehensive SBOM and dependency intelligence (probably already in use — confirm with existing FOSS team)
3. **GitHub Dependency Review** as a passive supplementary check (comes free)

**Why not Snyk:** Snyk is excellent but its snippet detection is less mature than FOSSology/ScanCode. For the specific AI-generated-snippet risk, open-source purpose-built tools are better.

**Decision criterion:** Check with existing Infineon FOSS team — they likely already have a scanner deployed for general product FOSS compliance. If so, **extend that installation to run on AI-touched PRs in `illd` and `foss-bsp` repos**, rather than introducing a new tool.

### 4.3 Integration pattern

```
PR opened (touches illd/ or foss-bsp/)
    ↓
GitHub Actions workflow
    ↓
1. Detect AI-authored files via git trailer (AI-Generated-By:)
2. Run scanner on AI-authored delta only
3. Parse scanner output; classify findings per §3.2 table
4. Post review comment with findings
5. Set commit status:
   - Pass → PR can merge (subject to human review)
   - Warn → Require Legal sign-off to merge
   - Block → Merge blocked; author must remediate
6. Record scan result in governance_incidents or a new foss_license_scan_log table
```

---

## 5. Roles and Operational Workflow

### 5.1 FOSS Compliance Officer (NEW ROLE)

**Assignment target:** within 30 days of this document's effective date (per GOVERNANCE_IMPLEMENTATION_PLAN GAP-21).

**Candidate:** Existing Infineon FOSS lead who handles the infineon.com/foss publication work. If no single person, a primary + backup pair.

**Responsibilities:**
- Own the scanner pipeline configuration
- Quarterly audit of all AI-touched iLLD merges
- Monthly scan of the full `illd` repo against public code matching services
- Represent Infineon in FOSS compliance discussions
- Escalation point for license findings that aren't clearly classified
- Liaison with Legal on CLA / upstream contribution policy questions
- Maintain the FOSS License Registry per release

### 5.2 Workflow: normal iLLD contribution (Zone B)

```
Engineer writes iLLD code with Copilot assistance
    ↓
Copilot "Block matching public code" = ON (cannot disable; org policy)
    ↓
Engineer commits with AI-Generated-By: trailer
    ↓
PR opened
    ↓
CI runs:
  - Build (TC3xx / TC4xx target)
  - MISRA advisory scan
  - Polyspace Bugfinder
  - FOSS license scanner (NEW) on AI-authored delta
    ↓
  Scanner result:
     Pass → human reviewer approves per AI_USAGE_POLICY §5.1a
     Warn → Legal sign-off path; FOSS Compliance Officer reviews
     Block → Author rewrites; re-scans
    ↓
Merge to release branch
    ↓
Release tag created → FOSS License Registry + SBOM auto-generated
    ↓
Release published to github.com/Infineon
```

### 5.3 Workflow: Zephyr/NuttX upstream contribution (Zone C)

```
Engineer writes BSP code with Copilot assistance
    ↓
Copilot "Block matching public code" = ON
    ↓
Engineer commits with:
  - AI-Generated-By: trailer
  - Signed-off-by: trailer (DCO)
    ↓
PR opened on Infineon internal foss-bsp repo (staging)
    ↓
CI runs (same as iLLD + upstream compatibility check):
  - Scanner: Apache-2.0 compatibility (Zephyr) or BSD-3 compatibility (NuttX)
  - Style check per upstream project convention
    ↓
Internal review + FOSS Compliance Officer sign-off
    ↓
PR submitted to upstream Zephyr or NuttX
    ↓
Upstream maintainer review (outside Infineon's control)
    ↓
If accepted: upstream merge; Infineon internal mirror updated
If rejected: understand feedback; iterate
```

### 5.4 Escalation triggers

Escalate to AI Governance Lead + Legal when:
- Scanner finds a **strong-copyleft match** regardless of length (never merge without legal review)
- Any AI-generated content reaches iLLD release that is later discovered to contain matched public code
- An upstream Zephyr/NuttX maintainer raises an IP concern about an Infineon contribution
- A customer or third party alleges iLLD contains their copyrighted content
- GitHub's Copilot "public code match" analytics show sustained non-zero matches from Infineon org

---

## 6. Metrics

| Metric | Target | Source |
|---|---|---|
| Copilot org-policy "Block matching public code" uptime | 100% | GitHub org policy audit |
| AI-touched PRs in `illd` / `foss-bsp` with scanner run | 100% | CI metrics |
| Scanner merge-blocks per quarter | Track trend (target: decreasing) | Scanner log |
| License-incident reports per quarter | 0 | governance_incidents |
| DCO sign-off compliance on Zone C contributions | 100% | Git trailer audit |
| SBOM generated per iLLD release | 100% | Release process |
| Time from scanner warn → Legal sign-off | < 5 business days | Scanner + approval logs |

---

## 7. Relationship to Other Documents

| Document | Relationship |
|---|---|
| AICE_SYSTEM_CARD v2.1.0 §3.2a | Defines the zones this document operationalizes for Zone B/C |
| AI_USAGE_POLICY v2.1.0 §5.1a, §5.1b, §7 | Defines review flow and data-class routing that assumes these controls |
| DATA_GOVERNANCE_POLICY v1.2.0 §11 | Introduces License Compliance as a policy; this document operationalizes it |
| TOOL_QUALIFICATION_PLAN v1.1.0 §2.2 | AI Usage Statement (Zone B/C substitute for TCER) references this document's controls |
| GOVERNANCE_IMPLEMENTATION_PLAN v2.1.0 GAP-21 | Sprint plan for implementing the controls |
| GPAI_PROVIDER_OBLIGATIONS v1.0.0 §4.3 | EU AI Act Art. 53(1)(c) copyright policy — this document is part of that policy fulfillment |
| Existing Infineon FOSS program (infineon.com/foss) | Parent program; this document is the AI-specific delta |

---

## 8. Open Items

| Item | Owner | Due |
|---|---|---|
| Assign FOSS Compliance Officer | AI Governance Lead + existing Infineon FOSS program owner | 30 days from effective date |
| Confirm existing Infineon scanner installation (Black Duck? FOSSology?) and extend to AI scope | FOSS Compliance Officer | Sprint 12 |
| Enable Copilot org-policy "Block matching public code" | GitHub Enterprise admin (IT) | Immediate |
| Enable CI scanner on `illd` and `foss-bsp` repos | Platform Team | Sprint 12-13 |
| SBOM generator integration | FOSS Compliance Officer + Platform | Sprint 14 |
| DCO training material (1h session) | AI Governance Lead + Legal | Before first Zone C upstream contribution |
| Legal check: CCLA with ASF for NuttX? | Legal | Before substantial NuttX contribution |

---

## 9. Document Control

| Version | Date | Author | Changes |
|---|---|---|---|
| 1.0.0 | 2026-04-20 | ATV MC D SW VDF + FOSS Compliance (TBA) | Initial release — covers Zone B (iLLD) and Zone C (FOSS BSP) license compliance for AI-generated content; mandatory controls; scanner tooling selection; DCO workflow; new FOSS Compliance Officer role |

**Approval (pending FOSS Compliance Officer assignment):**

| Role | Name | Date |
|---|---|---|
| AI Governance Lead | __________ | __________ |
| FOSS Compliance Officer | __________ | __________ |
| Legal (FOSS + upstream contribution) | __________ | __________ |
| Quality Manager | __________ | __________ |
