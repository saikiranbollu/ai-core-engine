"""
Test Script Parser
==================

Parses C test script files (.c/.h) from MCAL validation repositories
to extract test case functions and helper functions.

Extracts:
    - Function name, category, description
    - Full function body (for Qdrant semantic search)
    - APIs called (Test_* wrappers → actual driver APIs)
    - Configuration guards (#if BASE_TEST_MOD_CFG_INDEX == ...)
    - Input parameters (getParamU8/U16 calls)
    - Result assertions (SendU8/U16 between StartResult/EndResult)

Usage::

    from testscript_parser import parse_test_script

    result = parse_test_script("path/to/Test_Eth_17_Leth.c", module="ETH_17_LETH")
    # result = {
    #     "file_metadata": {...},
    #     "test_cases": [...],
    #     "helpers": [...],
    # }
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("testscript_parser")

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Test case function: void Leth_Tc_Fn_001(void)
# Captures: module prefix, category, number
_TC_FUNC_RE = re.compile(
    r"^void\s+([A-Za-z]+)_Tc_(Fn|CFL|FltInj|FltInj_Em|RU|Stress)_(\d+)\s*\(void\)",
    re.MULTILINE,
)

# Any non-test function definition (helpers, wrappers)
# Matches: void funcname(...) or Std_ReturnType funcname(...) at line start
_HELPER_FUNC_RE = re.compile(
    r"^(static\s+)?(void|Std_ReturnType|uint\d+|uint8|uint16|uint32|sint\d+|boolean)\s+"
    r"(l?[A-Z][A-Za-z0-9_]+)\s*\(",
    re.MULTILINE,
)

# Description in comment block before function
_DESC_RE = re.compile(r"\*\*\s*Description\s*:\s*(.+?)(?:\s*\*\*\s*$)", re.MULTILINE)

# Syntax/Name in comment block
_SYNTAX_RE = re.compile(r"\*\*\s*Syntax/Name\s*:\s*(\w+)", re.MULTILINE)

# API calls: Test_Eth_17_Leth_* or Eth_17_Leth_* direct calls
_API_CALL_RE = re.compile(r"\b((?:Test_)?Eth_17_[A-Za-z]+_[A-Za-z]+)\s*\(")

# Generic module API call pattern (works for any module)
_GENERIC_API_RE = re.compile(r"\b((?:Test_)?[A-Z][a-z]+(?:_\d+)?_[A-Z][a-z]+_[A-Za-z]+)\s*\(")

# Config guard: #if (BASE_TEST_MOD_CFG_INDEX == 21)
_CFG_GUARD_RE = re.compile(r"BASE_TEST_MOD_CFG_INDEX\s*==\s*(\d+)")

# Input parameters: getParamU8(N), getParamU16(N), getParamU8Array(N, ...)
_INPUT_PARAM_RE = re.compile(r"getParam(U8|U16|U32|U8Array)\s*\(")

# Result reporting: SendU8, SendU16, SendU8X, etc.
_RESULT_SEND_RE = re.compile(r"\b(Send(?:U8|U16|U32|U8X|U8HexArray|ReportedDetsCount|ReportedSECount|ReportedRTECount|ReportedDemsList))\s*\(")

# File header metadata
_FILE_VERSION_RE = re.compile(r"\*\*\s*File VERSION\s*:\s*(.+)")
_FILE_DATE_RE = re.compile(r"\*\*\s*DATE\s*:\s*(.+)")
_FILE_AUTHOR_RE = re.compile(r"\*\*\s*AUTHOR\s*:\s*(.+)")
_FILE_DESC_RE = re.compile(r"\*\*\s*DESCRIPTION\s*:\s*(.+)")

# Category extraction from function name
_CATEGORY_MAP = {
    "Fn": "Functional",
    "CFL": "ControlFlow",
    "FltInj": "FaultInjection",
    "FltInj_Em": "FaultInjection_ErrorMonitor",
    "RU": "Robustness",
    "Stress": "Stress",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FileMetadata:
    filename: str
    filepath: str
    version: str = ""
    date: str = ""
    author: str = ""
    description: str = ""
    module: str = ""


@dataclass
class ParsedFunction:
    """Represents a parsed function from the test script."""
    function_name: str
    category: str  # "Functional", "ControlFlow", etc. or "Helper", "Wrapper"
    description: str
    body: str  # Full function body including signature
    line_start: int
    line_end: int
    test_case_id: str = ""  # e.g., "Leth_Tc_Fn_001" (empty for helpers)
    apis_called: List[str] = field(default_factory=list)
    cfg_guards: List[str] = field(default_factory=list)
    input_param_count: int = 0
    result_sends: List[str] = field(default_factory=list)
    is_test_case: bool = True


@dataclass
class ParseResult:
    file_metadata: FileMetadata
    test_cases: List[ParsedFunction]
    helpers: List[ParsedFunction]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _find_function_end(lines: List[str], start_idx: int) -> int:
    """
    Find the closing brace of a function body starting from the opening brace.
    Handles nested braces correctly.
    """
    brace_count = 0
    found_open = False

    for i in range(start_idx, len(lines)):
        for ch in lines[i]:
            if ch == '{':
                brace_count += 1
                found_open = True
            elif ch == '}':
                brace_count -= 1
                if found_open and brace_count == 0:
                    return i
    return len(lines) - 1


def _extract_comment_block(lines: List[str], func_line_idx: int) -> str:
    """
    Look backwards from a function definition to find the preceding comment block.
    Returns the description text found within the comment.
    """
    desc_parts = []
    # Look back up to 15 lines for a comment block
    start = max(0, func_line_idx - 15)
    block = "\n".join(lines[start:func_line_idx])

    m = _DESC_RE.search(block)
    if m:
        desc = m.group(1).strip().rstrip("*").strip()
        # Also look for continuation lines
        desc_line_idx = block[:m.start()].count('\n') + start
        for j in range(desc_line_idx + 1, func_line_idx):
            line = lines[j].strip()
            if line.startswith("**") and not line.startswith("***"):
                continuation = line.lstrip("*").strip().rstrip("*").strip()
                if continuation and not continuation.startswith("MAY BE") and continuation != "/":
                    desc_parts.append(continuation)
                else:
                    break
            else:
                break
        return (desc + " " + " ".join(desc_parts)).strip()
    return ""


def _extract_cfg_guards(lines: List[str], func_line_idx: int) -> List[str]:
    """
    Look backwards from a function to find #if cfg guards that enclose it.
    """
    guards = []
    # Check lines before the function (within 5 lines)
    for i in range(max(0, func_line_idx - 5), func_line_idx):
        for m in _CFG_GUARD_RE.finditer(lines[i]):
            guards.append(m.group(1))
    return guards


def _extract_apis_from_body(body: str, module_prefix: str = "") -> List[str]:
    """
    Extract unique API names called in a function body.
    Maps Test_* wrappers to actual APIs.
    """
    apis = set()
    # Find all API-like calls
    for m in _API_CALL_RE.finditer(body):
        api = m.group(1)
        # Map Test_ wrapper to actual API
        if api.startswith("Test_"):
            actual_api = api[5:]  # Remove "Test_" prefix
            apis.add(actual_api)
        else:
            apis.add(api)

    # Also try generic pattern if module_prefix provided
    if module_prefix:
        prefix_pattern = re.compile(rf"\b((?:Test_)?{re.escape(module_prefix)}_[A-Za-z]+)\s*\(")
        for m in prefix_pattern.finditer(body):
            api = m.group(1)
            if api.startswith("Test_"):
                apis.add(api[5:])
            else:
                apis.add(api)

    return sorted(apis)


def _count_input_params(body: str) -> int:
    """Count the number of getParam* calls in a function body."""
    return len(_INPUT_PARAM_RE.findall(body))


def _extract_result_sends(body: str) -> List[str]:
    """Extract the sequence of Send* calls between StartResult/EndResult."""
    # Find the section between StartResult() and EndResult()
    start_match = re.search(r"StartResult\s*\(\s*\)", body)
    end_match = re.search(r"EndResult\s*\(\s*\)", body)
    if not start_match or not end_match:
        return []

    result_section = body[start_match.end():end_match.start()]
    return [m.group(1) for m in _RESULT_SEND_RE.finditer(result_section)]


def _parse_file_metadata(content: str, filepath: str, module: str) -> FileMetadata:
    """Extract file-level metadata from the header comment block."""
    meta = FileMetadata(
        filename=Path(filepath).name,
        filepath=filepath,
        module=module,
    )

    # Only look at first 50 lines for header metadata
    header = "\n".join(content.split("\n")[:50])

    m = _FILE_VERSION_RE.search(header)
    if m:
        meta.version = m.group(1).strip().rstrip("*").strip()

    m = _FILE_DATE_RE.search(header)
    if m:
        meta.date = m.group(1).strip().rstrip("*").strip()

    m = _FILE_AUTHOR_RE.search(header)
    if m:
        meta.author = m.group(1).strip().rstrip("*").strip()

    m = _FILE_DESC_RE.search(header)
    if m:
        meta.description = m.group(1).strip().rstrip("*").strip()

    return meta


def parse_test_script(
    filepath: str | Path,
    module: str = "",
    *,
    include_helpers: bool = True,
) -> ParseResult:
    """
    Parse a C test script file and extract all test case functions and helpers.

    Args:
        filepath: Path to the .c file.
        module: MCAL module name (e.g., "ETH_17_LETH"). Auto-detected if empty.
        include_helpers: Whether to parse helper/utility functions too.

    Returns:
        ParseResult with file_metadata, test_cases, and helpers.
    """
    filepath = Path(filepath)
    content = filepath.read_text(encoding="utf-8", errors="replace")
    lines = content.split("\n")

    # Auto-detect module from filename if not provided
    if not module:
        # Test_Eth_17_Leth.c → ETH_17_LETH
        name = filepath.stem
        if name.startswith("Test_"):
            module = name[5:].upper()
        else:
            module = name.split("_")[0].upper()

    # Determine the module API prefix for API detection
    # ETH_17_LETH → Eth_17_Leth
    parts = module.split("_")
    if len(parts) >= 3:
        module_prefix = f"{parts[0].capitalize()}_{parts[1]}_{parts[2].capitalize()}"
    else:
        module_prefix = module.capitalize()

    logger.info("Parsing %s (module=%s, prefix=%s)", filepath.name, module, module_prefix)

    # Parse file metadata
    file_meta = _parse_file_metadata(content, str(filepath), module)

    # --- Find all test case functions ---
    test_cases: List[ParsedFunction] = []
    helpers: List[ParsedFunction] = []

    # Track which line ranges belong to test functions so we can identify helpers
    tc_line_ranges = set()

    for m in _TC_FUNC_RE.finditer(content):
        prefix = m.group(1)     # e.g., "Leth"
        category = m.group(2)   # e.g., "Fn", "CFL", "FltInj_Em"
        number = m.group(3)     # e.g., "001"

        func_name = f"{prefix}_Tc_{category}_{number}"
        test_case_id = func_name

        # Find line number (0-indexed)
        line_idx = content[:m.start()].count("\n")

        # Find function body end
        end_idx = _find_function_end(lines, line_idx)
        body = "\n".join(lines[line_idx:end_idx + 1])

        # Mark this range as belonging to a test function
        for li in range(line_idx, end_idx + 1):
            tc_line_ranges.add(li)

        # Extract metadata
        description = _extract_comment_block(lines, line_idx)
        cfg_guards = _extract_cfg_guards(lines, line_idx)
        apis_called = _extract_apis_from_body(body, module_prefix)
        input_count = _count_input_params(body)
        result_sends = _extract_result_sends(body)

        tc = ParsedFunction(
            function_name=func_name,
            category=_CATEGORY_MAP.get(category, category),
            description=description,
            body=body,
            line_start=line_idx + 1,  # 1-indexed
            line_end=end_idx + 1,
            test_case_id=test_case_id,
            apis_called=apis_called,
            cfg_guards=cfg_guards,
            input_param_count=input_count,
            result_sends=result_sends,
            is_test_case=True,
        )
        test_cases.append(tc)

    logger.info("Found %d test case functions", len(test_cases))

    # --- Find helper functions (if requested) ---
    if include_helpers:
        for m in _HELPER_FUNC_RE.finditer(content):
            func_name = m.group(3)
            line_idx = content[:m.start()].count("\n")

            # Skip if this line belongs to a test case function
            if line_idx in tc_line_ranges:
                continue

            # Skip the test case functions themselves (they match _HELPER too)
            if re.match(r"[A-Za-z]+_Tc_(Fn|CFL|FltInj|FltInj_Em|RU|Stress)_\d+", func_name):
                continue

            # Check this is a function DEFINITION (has opening brace), not a declaration
            # Look at the match line and following few lines for '{'
            # A declaration ends with ';' before any '{'
            rest_of_match = content[m.start():]
            paren_depth = 0
            found_open_paren = False
            is_definition = False
            for ch in rest_of_match:
                if ch == '(':
                    paren_depth += 1
                    found_open_paren = True
                elif ch == ')':
                    paren_depth -= 1
                elif found_open_paren and paren_depth == 0:
                    if ch == '{':
                        is_definition = True
                        break
                    elif ch == ';':
                        break  # It's a declaration
                    elif ch in ('\n', '\r', ' ', '\t', '#'):
                        continue
                    # Skip preprocessor lines after signature
                    elif ch == '/':
                        continue

            if not is_definition:
                continue

            # Find function body end
            end_idx = _find_function_end(lines, line_idx)
            body = "\n".join(lines[line_idx:end_idx + 1])

            # Determine if it's a wrapper (Test_Module_ApiName pattern)
            is_wrapper = func_name.startswith("Test_")

            description = _extract_comment_block(lines, line_idx)
            apis_called = _extract_apis_from_body(body, module_prefix)

            category = "Wrapper" if is_wrapper else "Helper"

            helper = ParsedFunction(
                function_name=func_name,
                category=category,
                description=description,
                body=body,
                line_start=line_idx + 1,
                line_end=end_idx + 1,
                test_case_id="",
                apis_called=apis_called,
                cfg_guards=[],
                input_param_count=0,
                result_sends=[],
                is_test_case=False,
            )
            helpers.append(helper)

        logger.info("Found %d helper/wrapper functions", len(helpers))

    return ParseResult(
        file_metadata=file_meta,
        test_cases=test_cases,
        helpers=helpers,
    )


# ---------------------------------------------------------------------------
# CLI for testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python testscript_parser.py <path_to_test.c> [module]")
        sys.exit(1)

    fpath = sys.argv[1]
    mod = sys.argv[2] if len(sys.argv) > 2 else ""

    result = parse_test_script(fpath, module=mod)

    print(f"\n{'='*60}")
    print(f"  File: {result.file_metadata.filename}")
    print(f"  Module: {result.file_metadata.module}")
    print(f"  Version: {result.file_metadata.version}")
    print(f"  Author: {result.file_metadata.author}")
    print(f"  Date: {result.file_metadata.date}")
    print(f"{'='*60}")
    print(f"  Test Cases: {len(result.test_cases)}")
    print(f"  Helpers: {len(result.helpers)}")
    print(f"{'='*60}")

    # Print category distribution
    from collections import Counter
    cats = Counter(tc.category for tc in result.test_cases)
    print("\n  Categories:")
    for cat, cnt in cats.most_common():
        print(f"    {cat}: {cnt}")

    # Print first 5 test cases
    print(f"\n  First 5 test cases:")
    for tc in result.test_cases[:5]:
        apis_str = ", ".join(tc.apis_called[:3])
        if len(tc.apis_called) > 3:
            apis_str += f" (+{len(tc.apis_called)-3} more)"
        print(f"    {tc.test_case_id} [{tc.category}] L{tc.line_start}-{tc.line_end}")
        print(f"      Desc: {tc.description[:80]}...")
        print(f"      APIs: {apis_str}")
        print(f"      Inputs: {tc.input_param_count}, Results: {len(tc.result_sends)}")
        if tc.cfg_guards:
            print(f"      CfgGuards: {tc.cfg_guards}")

    # Print first 5 helpers
    if result.helpers:
        print(f"\n  First 5 helpers:")
        for h in result.helpers[:5]:
            print(f"    {h.function_name} [{h.category}] L{h.line_start}-{h.line_end}")
            print(f"      Desc: {h.description[:80]}")
