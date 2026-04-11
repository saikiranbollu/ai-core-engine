"""
Result Processors — Sprint 9
==============================
Replaces the Sprint 4 placeholder in process_results MCP tool.

Parses test/analysis results from external tools and feeds them into
the knowledge graph + feedback sink for continuous learning.

Supported result types:
  vp        — Vector Processor XML reports (Infineon VP simulation)
  polyspace — Polyspace Bug Finder / Code Prover results (CSV/XML)
  junit     — JUnit XML test reports (standard xUnit format)
  coverage  — GCOV/LCOV coverage data
  compiler  — GCC/Tasking compiler warning/error logs

Architecture position:
  process_results MCP tool (Category 8) → ResultProcessor.process()
  → Parsed results → FeedbackSink (learning) + Neo4j (graph update)

Usage:
    processor = ResultProcessor(neo4j_driver=driver, feedback_sink=sink)
    result = processor.process(
        results_dir="/path/to/results",
        result_type="junit",
        module_name="Adc",
        learn_from_failures=True,
        update_graph=True,
    )
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════
#  Parsed Result Models
# ═════════════════════════════════════════════════════════════════════════

class TestResult:
    """Unified test result from any source."""
    __slots__ = ("test_id", "test_name", "status", "duration_ms",
                 "module", "source_type", "message", "file_path",
                 "line_number", "severity", "rule_id", "extra")

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs.get(slot))

    def to_dict(self) -> Dict[str, Any]:
        return {s: getattr(self, s) for s in self.__slots__ if getattr(self, s) is not None}

    @property
    def is_failure(self) -> bool:
        return self.status in ("FAIL", "ERROR", "VIOLATION", "WARNING")


# ═════════════════════════════════════════════════════════════════════════
#  Individual Parsers
# ═════════════════════════════════════════════════════════════════════════

class JUnitParser:
    """Parse JUnit/xUnit XML test reports."""

    @staticmethod
    def parse(path: Path) -> List[TestResult]:
        results = []
        try:
            tree = ET.parse(str(path))
            root = tree.getroot()

            # Handle both <testsuites> and <testsuite> as root
            suites = root.findall(".//testsuite") if root.tag == "testsuites" else [root]

            for suite in suites:
                suite_name = suite.get("name", "unknown")
                for tc in suite.findall("testcase"):
                    name = tc.get("name", "unknown")
                    classname = tc.get("classname", suite_name)
                    duration = float(tc.get("time", "0")) * 1000  # seconds → ms

                    failure = tc.find("failure")
                    error = tc.find("error")
                    skipped = tc.find("skipped")

                    if failure is not None:
                        status = "FAIL"
                        message = failure.get("message", failure.text or "")
                    elif error is not None:
                        status = "ERROR"
                        message = error.get("message", error.text or "")
                    elif skipped is not None:
                        status = "SKIP"
                        message = skipped.get("message", "")
                    else:
                        status = "PASS"
                        message = None

                    results.append(TestResult(
                        test_id=f"{classname}.{name}",
                        test_name=name,
                        status=status,
                        duration_ms=int(duration),
                        source_type="junit",
                        message=message,
                        extra={"classname": classname, "suite": suite_name},
                    ))
        except ET.ParseError as e:
            logger.warning("[JUnitParser] XML parse error in %s: %s", path, e)
        except Exception as e:
            logger.warning("[JUnitParser] Failed to parse %s: %s", path, e)
        return results


class VPParser:
    """Parse Infineon Vector Processor (VP) simulation XML reports."""

    @staticmethod
    def parse(path: Path) -> List[TestResult]:
        results = []
        try:
            tree = ET.parse(str(path))
            root = tree.getroot()

            # VP reports use <TestCase> elements with <Result> children
            for tc in root.iter():
                if tc.tag in ("TestCase", "testcase", "Test"):
                    name = tc.get("name", tc.get("Name", "unknown"))
                    result_elem = tc.find("Result")
                    if result_elem is None:
                        result_elem = tc.find("result")

                    if result_elem is not None:
                        status_text = (result_elem.text or result_elem.get("status", "")).strip().upper()
                    else:
                        # Support both legacy (result/status) and Sprint 9 (verdict) attributes
                        status_text = tc.get("result", tc.get("status", tc.get("verdict", "UNKNOWN"))).strip().upper()

                    # Normalize VP statuses
                    if status_text in ("PASSED", "PASS", "OK", "SUCCESS"):
                        status = "PASS"
                    elif status_text in ("FAILED", "FAIL", "NOK", "FAILURE"):
                        status = "FAIL"
                    elif status_text in ("ERROR", "ABORT", "CRASH"):
                        status = "ERROR"
                    else:
                        status = "UNKNOWN"

                    # Support child elements (Duration/Time) and attribute (duration_ms)
                    duration_elem = tc.find("Duration")
                    if duration_elem is None:
                        duration_elem = tc.find("duration")
                    if duration_elem is None:
                        duration_elem = tc.find("Time")
                    duration_ms = 0
                    if duration_elem is not None and duration_elem.text:
                        try:
                            duration_ms = int(float(duration_elem.text) * 1000)
                        except ValueError:
                            pass
                    elif tc.get("duration_ms"):
                        try:
                            duration_ms = int(float(tc.get("duration_ms")))
                        except ValueError:
                            pass

                    # Prefer failure-specific elements, then fall back to description
                    # NOTE: cannot use `or` chaining — ET.Element is falsy when it has no children
                    message_elem = None
                    for _tag in ("FailureInfo", "ErrorMessage", "Message", "message", "Description"):
                        message_elem = tc.find(_tag)
                        if message_elem is not None:
                            break
                    message = message_elem.text.strip() if message_elem is not None and message_elem.text else None

                    results.append(TestResult(
                        test_id=name,
                        test_name=name,
                        status=status,
                        duration_ms=duration_ms,
                        source_type="vp",
                        message=message,
                    ))
        except ET.ParseError as e:
            logger.warning("[VPParser] XML parse error in %s: %s", path, e)
        except Exception as e:
            logger.warning("[VPParser] Failed to parse %s: %s", path, e)
        return results


class PolyspaceParser:
    """Parse Polyspace Bug Finder / Code Prover results (CSV or XML)."""

    @staticmethod
    def parse(path: Path) -> List[TestResult]:
        results = []
        ext = path.suffix.lower()

        if ext == ".csv":
            results = PolyspaceParser._parse_csv(path)
        elif ext in (".xml", ".psbf", ".pscp"):
            results = PolyspaceParser._parse_xml(path)
        else:
            logger.warning("[PolyspaceParser] Unsupported extension: %s", ext)
        return results

    @staticmethod
    def _parse_csv(path: Path) -> List[TestResult]:
        results = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if not lines:
                return results

            # Find header line (Polyspace CSVs may have metadata lines before header)
            header_idx = 0
            for i, line in enumerate(lines):
                if "Family" in line or "Check" in line or "Rule" in line:
                    header_idx = i
                    break

            header = [h.strip().strip('"') for h in lines[header_idx].split(",")]
            col_map = {h.lower(): i for i, h in enumerate(header)}

            for line_num, line in enumerate(lines[header_idx + 1:], start=header_idx + 2):
                if not line.strip():
                    continue
                cols = [c.strip().strip('"') for c in line.split(",")]
                if len(cols) < len(header):
                    continue

                def _get(key: str) -> str:
                    idx = col_map.get(key, -1)
                    return cols[idx] if 0 <= idx < len(cols) else ""

                # Map Polyspace fields
                family = _get("family") or _get("check")
                color = _get("color") or _get("status") or _get("result")
                function = _get("function") or _get("procedure")
                file_name = _get("file") or _get("source file")
                line_no = _get("line")
                rule = _get("rule") or _get("misra-c:2012")
                info = _get("information") or _get("additional information") or _get("message")

                # Map Polyspace colors to statuses
                color_upper = color.upper()
                if color_upper in ("RED", "DEFECT", "BUG", "NON-JUSTIFIED"):
                    severity = "ERROR"
                    status = "VIOLATION"
                elif color_upper in ("ORANGE", "UNPROVEN"):
                    severity = "WARNING"
                    status = "WARNING"
                elif color_upper in ("GREEN", "PROVEN", "NO DEFECT"):
                    severity = "INFO"
                    status = "PASS"
                elif color_upper in ("GRAY", "GREY", "DEAD CODE", "UNREACHABLE"):
                    severity = "INFO"
                    status = "DEAD_CODE"
                else:
                    severity = "WARNING"
                    status = "UNKNOWN"

                results.append(TestResult(
                    test_id=f"PS_{line_num}",
                    test_name=f"{family}: {function}" if function else family,
                    status=status,
                    source_type="polyspace",
                    message=info,
                    file_path=file_name,
                    line_number=int(line_no) if line_no.isdigit() else None,
                    severity=severity,
                    rule_id=rule if rule else None,
                    extra={"color": color, "family": family},
                ))
        except Exception as e:
            logger.warning("[PolyspaceParser] CSV parse error in %s: %s", path, e)
        return results

    @staticmethod
    def _parse_xml(path: Path) -> List[TestResult]:
        results = []
        try:
            tree = ET.parse(str(path))
            root = tree.getroot()
            for finding in root.iter():
                if finding.tag in ("Finding", "Result", "Defect", "Check"):
                    family = finding.get("family", finding.get("check", ""))
                    color = finding.get("color", finding.get("status", ""))
                    msg = finding.get("information", finding.get("message", ""))

                    color_upper = color.upper()
                    if color_upper in ("RED", "DEFECT"):
                        status, severity = "VIOLATION", "ERROR"
                    elif color_upper in ("ORANGE", "UNPROVEN"):
                        status, severity = "WARNING", "WARNING"
                    else:
                        status, severity = "PASS", "INFO"

                    results.append(TestResult(
                        test_id=f"PS_{finding.get('id', uuid.uuid4().hex[:6])}",
                        test_name=family or "polyspace_check",
                        status=status,
                        source_type="polyspace",
                        message=msg,
                        file_path=finding.get("file"),
                        line_number=int(finding.get("line", "0")) or None,
                        severity=severity,
                        rule_id=finding.get("rule"),
                    ))
        except Exception as e:
            logger.warning("[PolyspaceParser] XML parse error in %s: %s", path, e)
        return results


class CoverageParser:
    """Parse GCOV/LCOV coverage reports."""

    @staticmethod
    def parse(path: Path) -> List[TestResult]:
        results = []
        ext = path.suffix.lower()

        if ext == ".info" or path.name.endswith(".lcov"):
            results = CoverageParser._parse_lcov(path)
        elif ext == ".gcov":
            results = CoverageParser._parse_gcov(path)
        elif ext in (".json", ".xml"):
            results = CoverageParser._parse_json_or_xml(path)
        else:
            logger.warning("[CoverageParser] Unsupported format: %s", ext)
        return results

    @staticmethod
    def _parse_lcov(path: Path) -> List[TestResult]:
        results = []
        try:
            current_file = None
            lines_found = 0
            lines_hit = 0

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SF:"):
                        current_file = line[3:]
                    elif line.startswith("LF:"):
                        lines_found = int(line[3:])
                    elif line.startswith("LH:"):
                        lines_hit = int(line[3:])
                    elif line == "end_of_record" and current_file:
                        pct = round(lines_hit / lines_found * 100, 1) if lines_found > 0 else 0.0
                        status = "PASS" if pct >= 80.0 else ("WARNING" if pct >= 50.0 else "FAIL")

                        results.append(TestResult(
                            test_id=f"cov_{Path(current_file).stem}",
                            test_name=f"Coverage: {Path(current_file).name}",
                            status=status,
                            source_type="coverage",
                            message=f"{pct}% line coverage ({lines_hit}/{lines_found})",
                            file_path=current_file,
                            extra={"lines_found": lines_found, "lines_hit": lines_hit,
                                   "coverage_pct": pct},
                        ))
                        current_file = None
                        lines_found = lines_hit = 0
        except Exception as e:
            logger.warning("[CoverageParser] LCOV parse error in %s: %s", path, e)
        return results

    @staticmethod
    def _parse_gcov(path: Path) -> List[TestResult]:
        results = []
        try:
            total_lines = 0
            covered_lines = 0
            source_file = path.stem.replace(".gcov", "")

            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split(":", 2)
                    if len(parts) >= 2:
                        count = parts[0].strip()
                        if count == "-":
                            continue  # non-executable
                        total_lines += 1
                        if count != "#####" and count != "=====":
                            try:
                                if int(count) > 0:
                                    covered_lines += 1
                            except ValueError:
                                pass

            pct = round(covered_lines / total_lines * 100, 1) if total_lines > 0 else 0.0
            status = "PASS" if pct >= 80.0 else ("WARNING" if pct >= 50.0 else "FAIL")

            results.append(TestResult(
                test_id=f"cov_{source_file}",
                test_name=f"Coverage: {source_file}",
                status=status,
                source_type="coverage",
                message=f"{pct}% line coverage ({covered_lines}/{total_lines})",
                file_path=str(path),
                extra={"lines_found": total_lines, "lines_hit": covered_lines,
                       "coverage_pct": pct},
            ))
        except Exception as e:
            logger.warning("[CoverageParser] GCOV parse error in %s: %s", path, e)
        return results

    @staticmethod
    def _parse_json_or_xml(path: Path) -> List[TestResult]:
        """Parse Cobertura XML or JSON coverage reports."""
        results = []
        try:
            if path.suffix == ".json":
                with open(path, "r") as f:
                    data = json.load(f)
                # Common JSON coverage formats
                files = data.get("files", data.get("source_files", []))
                for fdata in files:
                    name = fdata.get("filename", fdata.get("name", "unknown"))
                    pct = fdata.get("line_rate", fdata.get("coverage", 0))
                    if isinstance(pct, float) and pct <= 1.0:
                        pct = pct * 100
                    pct = round(pct, 1)
                    status = "PASS" if pct >= 80 else ("WARNING" if pct >= 50 else "FAIL")
                    results.append(TestResult(
                        test_id=f"cov_{Path(name).stem}",
                        test_name=f"Coverage: {Path(name).name}",
                        status=status, source_type="coverage",
                        message=f"{pct}% line coverage", file_path=name,
                        extra={"coverage_pct": pct},
                    ))
            else:
                # Cobertura XML
                tree = ET.parse(str(path))
                for pkg in tree.findall(".//class") or tree.findall(".//package"):
                    name = pkg.get("filename", pkg.get("name", "unknown"))
                    rate = float(pkg.get("line-rate", "0"))
                    pct = round(rate * 100, 1)
                    status = "PASS" if pct >= 80 else ("WARNING" if pct >= 50 else "FAIL")
                    results.append(TestResult(
                        test_id=f"cov_{Path(name).stem}",
                        test_name=f"Coverage: {Path(name).name}",
                        status=status, source_type="coverage",
                        message=f"{pct}% line coverage", file_path=name,
                        extra={"coverage_pct": pct},
                    ))
        except Exception as e:
            logger.warning("[CoverageParser] JSON/XML parse error in %s: %s", path, e)
        return results


class CompilerParser:
    """Parse GCC/Tasking compiler warning/error logs."""

    # GCC:   file.c:42:10: warning: implicit declaration of function 'foo' [-Wimplicit-function-declaration]
    # Tasking: file.c 42/10 warning: ...
    _GCC_RE = re.compile(
        r"^(?P<file>[^:\s]+):(?P<line>\d+):(?P<col>\d+):\s*"
        r"(?P<severity>warning|error|note):\s*(?P<message>.+?)(?:\s*\[(?P<flag>-W[\w-]+)\])?\s*$"
    )
    _TASKING_RE = re.compile(
        r"^(?P<file>[^:\s]+)\s+(?P<line>\d+)/(?P<col>\d+)\s+"
        r"(?P<severity>warning|error|note):\s*(?P<message>.+)$"
    )

    @staticmethod
    def parse(path: Path) -> List[TestResult]:
        results = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, start=1):
                    line = line.strip()
                    if not line:
                        continue

                    m = CompilerParser._GCC_RE.match(line) or CompilerParser._TASKING_RE.match(line)
                    if m:
                        sev = m.group("severity").upper()
                        status = "ERROR" if sev == "ERROR" else ("WARNING" if sev == "WARNING" else "INFO")

                        results.append(TestResult(
                            test_id=f"cc_{line_num}",
                            test_name=f"Compiler {sev}: {m.group('file')}:{m.group('line')}",
                            status=status,
                            source_type="compiler",
                            message=m.group("message"),
                            file_path=m.group("file"),
                            line_number=int(m.group("line")),
                            severity=sev.lower(),
                            rule_id=m.groupdict().get("flag"),
                        ))
        except Exception as e:
            logger.warning("[CompilerParser] Parse error in %s: %s", path, e)
        return results


# ═════════════════════════════════════════════════════════════════════════
#  Result Processor — Orchestrator
# ═════════════════════════════════════════════════════════════════════════

# File extension → parser mapping
_PARSER_MAP = {
    "junit":     (JUnitParser,     [".xml"]),
    "vp":        (VPParser,        [".xml"]),
    "polyspace": (PolyspaceParser, [".csv", ".xml", ".psbf", ".pscp"]),
    "coverage":  (CoverageParser,  [".info", ".lcov", ".gcov", ".xml", ".json"]),
    "compiler":  (CompilerParser,  [".log", ".txt", ".warnings"]),
}


class ResultProcessor:
    """
    Orchestrates result parsing, graph update, and learning feedback.

    Usage from MCP process_results tool:
        processor = ResultProcessor(neo4j_driver=driver, feedback_sink=sink)
        summary = processor.process(
            results_dir="/path/to/results",
            result_type="junit",
            module_name="Adc",
        )
    """

    def __init__(self, neo4j_driver=None, feedback_sink=None, postgres_client=None):
        self._neo4j = neo4j_driver
        self._sink = feedback_sink
        self._pg = postgres_client

    def process(
        self,
        results_dir: str,
        result_type: str,
        module_name: Optional[str] = None,
        learn_from_failures: bool = True,
        update_graph: bool = True,
        workspace_id: str = "illd",
    ) -> Dict[str, Any]:
        """Parse results and optionally update graph + learning loop."""
        t0 = time.time()

        if result_type not in _PARSER_MAP:
            return {
                "status": "error",
                "message": f"Unknown result_type '{result_type}'. "
                           f"Supported: {list(_PARSER_MAP.keys())}",
            }

        parser_cls, extensions = _PARSER_MAP[result_type]
        results_path = Path(results_dir)

        if not results_path.exists():
            return {"status": "error", "message": f"Path not found: {results_dir}"}

        # ── Discover files ──
        files = []
        if results_path.is_file():
            files = [results_path]
        else:
            for ext in extensions:
                files.extend(results_path.rglob(f"*{ext}"))
            files = sorted(set(files))

        if not files:
            return {
                "status": "warning",
                "message": f"No {result_type} files found in {results_dir} "
                           f"(extensions: {extensions})",
                "files_searched": 0,
            }

        # ── Parse all files ──
        all_results: List[TestResult] = []
        parse_errors = 0
        for fp in files:
            try:
                parsed = parser_cls.parse(fp)
                for r in parsed:
                    r.module = module_name
                all_results.extend(parsed)
            except Exception as e:
                logger.warning("[ResultProcessor] Error parsing %s: %s", fp, e)
                parse_errors += 1

        # ── Compute summary stats ──
        total = len(all_results)
        passed = sum(1 for r in all_results if r.status == "PASS")
        failed = sum(1 for r in all_results if r.status == "FAIL")
        errors = sum(1 for r in all_results if r.status == "ERROR")
        warnings = sum(1 for r in all_results if r.status == "WARNING")
        violations = sum(1 for r in all_results if r.status == "VIOLATION")
        skipped = sum(1 for r in all_results if r.status == "SKIP")

        # ── Update knowledge graph ──
        graph_nodes_created = 0
        if update_graph and self._neo4j and all_results:
            graph_nodes_created = self._update_graph(
                all_results, module_name, workspace_id, result_type
            )

        # ── Feed failures into learning loop ──
        failures_learned = 0
        if learn_from_failures and self._sink:
            failures = [r for r in all_results if r.is_failure]
            for f in failures:
                try:
                    self._sink.submit_feedback(
                        response_id=f"result_{f.test_id}",
                        decision="REJECT",
                        reviewer_id="automated_result_processor",
                        issues_found=1,
                        correction_notes=f"[{result_type}] {f.test_name}: {f.message or f.status}",
                    )
                    failures_learned += 1
                except Exception as e:
                    logger.warning("[ResultProcessor] Failed to record failure: %s", e)

        # ── PostgreSQL audit ──
        if self._pg:
            try:
                self._pg.log_audit(
                    tool_name="process_results",
                    parameters={"result_type": result_type, "module": module_name,
                                "files": len(files), "total_results": total},
                    response_code="ok",
                    duration_ms=int((time.time() - t0) * 1000),
                )
            except Exception:
                pass

        elapsed = round(time.time() - t0, 2)

        return {
            "status": "completed",
            "result_type": result_type,
            "module": module_name,
            "files_processed": len(files),
            "parse_errors": parse_errors,
            "total_results": total,
            "summary": {
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "warnings": warnings,
                "violations": violations,
                "skipped": skipped,
            },
            "pass_rate": round(passed / total * 100, 1) if total > 0 else 0.0,
            "graph_nodes_created": graph_nodes_created,
            "failures_learned": failures_learned,
            "elapsed_seconds": elapsed,
        }

    def _update_graph(
        self,
        results: List[TestResult],
        module_name: Optional[str],
        workspace_id: str,
        result_type: str,
    ) -> int:
        """Create TestResult/AnalysisResult nodes in Neo4j."""
        count = 0
        db = workspace_id if workspace_id in ("illd", "mcal") else "neo4j"

        # Batch Cypher for efficiency
        batch_params = []
        for r in results:
            batch_params.append({
                "result_id": f"{result_type}_{r.test_id}_{uuid.uuid4().hex[:6]}",
                "test_name": r.test_name,
                "status": r.status,
                "source_type": r.source_type,
                "module": module_name or "unknown",
                "message": r.message,
                "file_path": r.file_path,
                "line_number": r.line_number,
                "severity": r.severity,
                "rule_id": r.rule_id,
                "duration_ms": r.duration_ms,
                "timestamp": time.time(),
            })

        if not batch_params:
            return 0

        cypher = """
        UNWIND $batch AS r
        MERGE (tr:TestResult {result_id: r.result_id})
        SET tr.test_name    = r.test_name,
            tr.status       = r.status,
            tr.source_type  = r.source_type,
            tr.module       = r.module,
            tr.message      = r.message,
            tr.file_path    = r.file_path,
            tr.line_number  = r.line_number,
            tr.severity     = r.severity,
            tr.rule_id      = r.rule_id,
            tr.duration_ms  = r.duration_ms,
            tr.ingested_at  = r.timestamp
        WITH tr, r
        OPTIONAL MATCH (ns:NodeSet {module: r.module})
        FOREACH (_ IN CASE WHEN ns IS NOT NULL THEN [1] ELSE [] END |
            MERGE (tr)-[:BELONGS_TO_MODULE]->(ns)
        )
        RETURN count(tr) AS created
        """

        try:
            with self._neo4j.session(database=db) as session:
                result = session.run(cypher, {"batch": batch_params})
                record = result.single()
                count = record["created"] if record else 0
                logger.info("[ResultProcessor] Created %d TestResult nodes in %s", count, db)
        except Exception as e:
            logger.warning("[ResultProcessor] Graph update failed: %s", e)

        return count
