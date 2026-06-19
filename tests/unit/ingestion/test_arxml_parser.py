"""Tests for the ARXML Parser (arxml_parser).

Covers:
- Pure ARXML (ECUC module configuration values)
- Template ARXML (EB tresos macros stripped)
- Chunk generation
- Cross-reference extraction
- Error handling
"""

from __future__ import annotations

import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

import xml.etree.ElementTree as ET

from IngestionPipeline.parsers import arxml_parser

# ---------------------------------------------------------------------------
# Sample pure ARXML – mirrors EcuC_001.arxml structure (simplified)
# ---------------------------------------------------------------------------

SAMPLE_PURE_ARXML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <AUTOSAR xmlns="http://autosar.org/schema/r4.0"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
             xsi:schemaLocation="http://autosar.org/schema/r4.0 AUTOSAR_00049.xsd">
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>EcuC</SHORT-NAME>
          <ELEMENTS>
            <ECUC-MODULE-CONFIGURATION-VALUES>
              <SHORT-NAME>EcuC</SHORT-NAME>
              <DEFINITION-REF DEST="ECUC-MODULE-DEF">/AURIX3G/EcucDefs/EcuC</DEFINITION-REF>
              <IMPLEMENTATION-CONFIG-VARIANT>VARIANT-POST-BUILD</IMPLEMENTATION-CONFIG-VARIANT>
              <CONTAINERS>
                <ECUC-CONTAINER-VALUE>
                  <SHORT-NAME>EcucHardware</SHORT-NAME>
                  <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/AURIX3G/EcucDefs/EcuC/EcucHardware</DEFINITION-REF>
                  <SUB-CONTAINERS>
                    <ECUC-CONTAINER-VALUE>
                      <SHORT-NAME>CoreDef_0</SHORT-NAME>
                      <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/AURIX3G/EcucDefs/EcuC/EcucHardware/EcucCoreDefinition</DEFINITION-REF>
                      <PARAMETER-VALUES>
                        <ECUC-NUMERICAL-PARAM-VALUE>
                          <DEFINITION-REF DEST="ECUC-INTEGER-PARAM-DEF">/AURIX3G/EcucDefs/EcuC/EcucHardware/EcucCoreDefinition/EcucCoreId</DEFINITION-REF>
                          <VALUE>1</VALUE>
                        </ECUC-NUMERICAL-PARAM-VALUE>
                      </PARAMETER-VALUES>
                    </ECUC-CONTAINER-VALUE>
                    <ECUC-CONTAINER-VALUE>
                      <SHORT-NAME>CoreDef_1</SHORT-NAME>
                      <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/AURIX3G/EcucDefs/EcuC/EcucHardware/EcucCoreDefinition</DEFINITION-REF>
                      <PARAMETER-VALUES>
                        <ECUC-NUMERICAL-PARAM-VALUE>
                          <DEFINITION-REF DEST="ECUC-INTEGER-PARAM-DEF">/AURIX3G/EcucDefs/EcuC/EcucHardware/EcucCoreDefinition/EcucCoreId</DEFINITION-REF>
                          <VALUE>9</VALUE>
                        </ECUC-NUMERICAL-PARAM-VALUE>
                      </PARAMETER-VALUES>
                    </ECUC-CONTAINER-VALUE>
                  </SUB-CONTAINERS>
                </ECUC-CONTAINER-VALUE>
                <ECUC-CONTAINER-VALUE>
                  <SHORT-NAME>EcucPartitionCollection</SHORT-NAME>
                  <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/AURIX3G/EcucDefs/EcuC/EcucPartitionCollection</DEFINITION-REF>
                  <SUB-CONTAINERS>
                    <ECUC-CONTAINER-VALUE>
                      <SHORT-NAME>EcucPartition</SHORT-NAME>
                      <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/AURIX3G/EcucDefs/EcuC/EcucPartitionCollection/EcucPartition</DEFINITION-REF>
                      <PARAMETER-VALUES>
                        <ECUC-NUMERICAL-PARAM-VALUE>
                          <DEFINITION-REF DEST="ECUC-BOOLEAN-PARAM-DEF">/AURIX3G/EcucDefs/EcuC/EcucPartitionCollection/EcucPartition/PartitionCanBeRestarted</DEFINITION-REF>
                          <VALUE>0</VALUE>
                        </ECUC-NUMERICAL-PARAM-VALUE>
                      </PARAMETER-VALUES>
                    </ECUC-CONTAINER-VALUE>
                  </SUB-CONTAINERS>
                </ECUC-CONTAINER-VALUE>
              </CONTAINERS>
            </ECUC-MODULE-CONFIGURATION-VALUES>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AUTOSAR>
""")

# ---------------------------------------------------------------------------
# Sample template ARXML – contains EB tresos macros
# ---------------------------------------------------------------------------

SAMPLE_TEMPLATE_ARXML = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    [!NOCODE!]
      [!INCLUDE "Adc.m"!]
    [!ENDNOCODE!]
    <AUTOSAR xmlns="http://autosar.org/schema/r4.0"
             xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>AUTOSAR_Adc</SHORT-NAME>
          <AR-PACKAGES>
            <AR-PACKAGE>
              <SHORT-NAME>BswModuleDescriptions</SHORT-NAME>
              <ELEMENTS>
                <BSW-MODULE-DESCRIPTION>
                  <SHORT-NAME>Adc</SHORT-NAME>
                  <INTERNAL-BEHAVIORS>
                    <BSW-INTERNAL-BEHAVIOR>
                      <SHORT-NAME>AdcBehavior</SHORT-NAME>
                      <EXCLUSIVE-AREAS>
                        <EXCLUSIVE-AREA>
                          <SHORT-NAME>MutualDataQM0</SHORT-NAME>
                        </EXCLUSIVE-AREA>
    [!IF "node:exists(AdcConfigSet/AdcHwUnit/*/AdcGroup/*/AdcEruTriggerConfig/*)"!]
                        <EXCLUSIVE-AREA>
                          <SHORT-NAME>RuntimeProtWriteSeq</SHORT-NAME>
                        </EXCLUSIVE-AREA>
    [!ENDIF!]
                      </EXCLUSIVE-AREAS>
                    </BSW-INTERNAL-BEHAVIOR>
                  </INTERNAL-BEHAVIORS>
                </BSW-MODULE-DESCRIPTION>
              </ELEMENTS>
            </AR-PACKAGE>
            <AR-PACKAGE>
              <SHORT-NAME>Implementations</SHORT-NAME>
              <ELEMENTS>
                <BSW-IMPLEMENTATION>
                  <SHORT-NAME>Adc</SHORT-NAME>
                  <PROGRAMMING-LANGUAGE>C</PROGRAMMING-LANGUAGE>
                  <RESOURCE-CONSUMPTION>
                    <SHORT-NAME>ResourceConsumption</SHORT-NAME>
                    <MEMORY-SECTIONS>
                      <MEMORY-SECTION>
                        <SHORT-NAME>ADC_CONST_ASIL_D_8</SHORT-NAME>
                        <ALIGNMENT>8</ALIGNMENT>
                        <PREFIX-REF DEST="SECTION-NAME-PREFIX">/AUTOSAR_Adc/Implementations/Adc/ResourceConsumption/ADC</PREFIX-REF>
                        <SW-ADDRMETHOD-REF DEST="SW-ADDR-METHOD">/AUTOSAR_MemMap/SwAddrMethods/CONST</SW-ADDRMETHOD-REF>
                        <SYMBOL>CONST_ASIL_D_8</SYMBOL>
                      </MEMORY-SECTION>
                      <MEMORY-SECTION>
                        <SHORT-NAME>ADC_CONST_ASIL_D_16</SHORT-NAME>
                        <ALIGNMENT>16</ALIGNMENT>
                        <PREFIX-REF DEST="SECTION-NAME-PREFIX">/AUTOSAR_Adc/Implementations/Adc/ResourceConsumption/ADC</PREFIX-REF>
                        <SW-ADDRMETHOD-REF DEST="SW-ADDR-METHOD">/AUTOSAR_MemMap/SwAddrMethods/CONST</SW-ADDRMETHOD-REF>
                        <SYMBOL>CONST_ASIL_D_16</SYMBOL>
                      </MEMORY-SECTION>
                    </MEMORY-SECTIONS>
                    <SECTION-NAME-PREFIXS>
                      <SECTION-NAME-PREFIX>
                        <SHORT-NAME>ADC</SHORT-NAME>
                        <SYMBOL>ADC</SYMBOL>
                      </SECTION-NAME-PREFIX>
                    </SECTION-NAME-PREFIXS>
                  </RESOURCE-CONSUMPTION>
                  <SW-VERSION>[!"$moduleSoftwareVer"!]</SW-VERSION>
                  <VENDOR-ID>17</VENDOR-ID>
                  <AR-RELEASE-VERSION>[!"$moduleReleaseVer"!]</AR-RELEASE-VERSION>
                  <BEHAVIOR-REF DEST="BSW-INTERNAL-BEHAVIOR">/AUTOSAR_Adc/BswModuleDescriptions/Adc/AdcBehavior</BEHAVIOR-REF>
                  <VENDOR-SPECIFIC-MODULE-DEF-REFS>
                    <VENDOR-SPECIFIC-MODULE-DEF-REF DEST="ECUC-MODULE-DEF">/AURIX3G/EcucDefs/Adc</VENDOR-SPECIFIC-MODULE-DEF-REF>
                  </VENDOR-SPECIFIC-MODULE-DEF-REFS>
                </BSW-IMPLEMENTATION>
              </ELEMENTS>
            </AR-PACKAGE>
          </AR-PACKAGES>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AUTOSAR>
""")

# ---------------------------------------------------------------------------
# Sample ARXML with REFERENCE-VALUES
# ---------------------------------------------------------------------------

SAMPLE_ARXML_WITH_REFS = textwrap.dedent("""\
    <?xml version="1.0" encoding="UTF-8"?>
    <AUTOSAR xmlns="http://autosar.org/schema/r4.0">
      <AR-PACKAGES>
        <AR-PACKAGE>
          <SHORT-NAME>TestPkg</SHORT-NAME>
          <ELEMENTS>
            <ECUC-MODULE-CONFIGURATION-VALUES>
              <SHORT-NAME>TestModule</SHORT-NAME>
              <DEFINITION-REF DEST="ECUC-MODULE-DEF">/Vendor/EcucDefs/TestModule</DEFINITION-REF>
              <CONTAINERS>
                <ECUC-CONTAINER-VALUE>
                  <SHORT-NAME>Channel_0</SHORT-NAME>
                  <DEFINITION-REF DEST="ECUC-PARAM-CONF-CONTAINER-DEF">/Vendor/EcucDefs/TestModule/Channel</DEFINITION-REF>
                  <PARAMETER-VALUES>
                    <ECUC-TEXTUAL-PARAM-VALUE>
                      <DEFINITION-REF DEST="ECUC-STRING-PARAM-DEF">/Vendor/EcucDefs/TestModule/Channel/ChannelName</DEFINITION-REF>
                      <VALUE>ADC_CH0</VALUE>
                    </ECUC-TEXTUAL-PARAM-VALUE>
                  </PARAMETER-VALUES>
                  <REFERENCE-VALUES>
                    <ECUC-REFERENCE-VALUE>
                      <DEFINITION-REF DEST="ECUC-REFERENCE-DEF">/Vendor/EcucDefs/TestModule/Channel/HwUnitRef</DEFINITION-REF>
                      <VALUE-REF DEST="ECUC-CONTAINER-VALUE">/TestPkg/TestModule/HwUnit_0</VALUE-REF>
                    </ECUC-REFERENCE-VALUE>
                  </REFERENCE-VALUES>
                </ECUC-CONTAINER-VALUE>
              </CONTAINERS>
            </ECUC-MODULE-CONFIGURATION-VALUES>
          </ELEMENTS>
        </AR-PACKAGE>
      </AR-PACKAGES>
    </AUTOSAR>
""")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def pure_arxml_file(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("arxml_tests")
    f = tmp / "EcuC.arxml"
    f.write_text(SAMPLE_PURE_ARXML, encoding="utf-8")
    return str(f)


@pytest.fixture(scope="session")
def template_arxml_file(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("arxml_tests_tpl")
    f = tmp / "Adc_Bswmd.arxml"
    f.write_text(SAMPLE_TEMPLATE_ARXML, encoding="utf-8")
    return str(f)


@pytest.fixture(scope="session")
def ref_arxml_file(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("arxml_tests_ref")
    f = tmp / "Refs.arxml"
    f.write_text(SAMPLE_ARXML_WITH_REFS, encoding="utf-8")
    return str(f)


# ===================================================================
# Top-level structure tests
# ===================================================================

class TestPureArxml:
    """Tests for pure AUTOSAR XML (no template macros)."""

    def test_returns_expected_top_level_keys(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        expected = {
            "file_path", "file_type", "is_template", "autosar_schema",
            "modules", "chunks", "cross_references", "statistics",
        }
        assert expected == set(result.keys())

    def test_file_type_is_arxml(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert result["file_type"] == "arxml"

    def test_not_detected_as_template(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert result["is_template"] is False
        assert result["statistics"]["template_macros_stripped"] == 0

    def test_schema_detected(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert result["autosar_schema"] == "r4.0"

    def test_ecuc_module_extracted(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert "EcuC" in result["modules"]
        mod = result["modules"]["EcuC"]
        assert mod["type"] == "ECUC-MODULE-CONFIGURATION-VALUES"
        assert mod["definition_ref"] == "/AURIX3G/EcucDefs/EcuC"

    def test_implementation_variant(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        assert mod["metadata"]["implementation_variant"] == "VARIANT-POST-BUILD"

    def test_containers_extracted(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        names = [c["short_name"] for c in mod["containers"]]
        assert "EcucHardware" in names
        assert "EcucPartitionCollection" in names

    def test_sub_containers_extracted(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        hw = next(c for c in mod["containers"] if c["short_name"] == "EcucHardware")
        assert len(hw["sub_containers"]) == 2
        names = [s["short_name"] for s in hw["sub_containers"]]
        assert "CoreDef_0" in names
        assert "CoreDef_1" in names

    def test_parameter_values_extracted(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        hw = next(c for c in mod["containers"] if c["short_name"] == "EcucHardware")
        core0 = next(s for s in hw["sub_containers"] if s["short_name"] == "CoreDef_0")
        assert len(core0["parameters"]) == 1
        p = core0["parameters"][0]
        assert p["name"] == "EcucCoreId"
        assert p["param_type"] == "INTEGER"
        assert p["value"] == "1"

    def test_second_core_value(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        hw = next(c for c in mod["containers"] if c["short_name"] == "EcucHardware")
        core1 = next(s for s in hw["sub_containers"] if s["short_name"] == "CoreDef_1")
        assert core1["parameters"][0]["value"] == "9"

    def test_partition_boolean_param(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        pc = next(
            c for c in mod["containers"]
            if c["short_name"] == "EcucPartitionCollection"
        )
        part = pc["sub_containers"][0]
        assert part["parameters"][0]["param_type"] == "BOOLEAN"
        assert part["parameters"][0]["value"] == "0"

    def test_autosar_paths_built_correctly(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod = result["modules"]["EcuC"]
        assert mod["path"] == "/EcuC/EcuC"
        hw = next(c for c in mod["containers"] if c["short_name"] == "EcucHardware")
        assert hw["path"] == "/EcuC/EcuC/EcucHardware"
        core0 = hw["sub_containers"][0]
        assert core0["path"] == "/EcuC/EcuC/EcucHardware/CoreDef_0"


# ===================================================================
# Template ARXML tests
# ===================================================================

class TestTemplateArxml:
    """Tests for ARXML files with EB tresos template macros."""

    def test_detected_as_template(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        assert result["is_template"] is True
        assert result["statistics"]["template_macros_stripped"] > 0

    def test_xml_parses_after_macro_stripping(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        assert result["modules"]  # should have at least something

    def test_bsw_module_description_extracted(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        bsw_key = "Adc__bsw_desc"
        assert bsw_key in result["modules"]
        mod = result["modules"][bsw_key]
        assert mod["type"] == "BSW-MODULE-DESCRIPTION"

    def test_exclusive_areas_extracted(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        bsw = result["modules"]["Adc__bsw_desc"]
        ea_names = [ea["short_name"] for ea in bsw["exclusive_areas"]]
        assert "MutualDataQM0" in ea_names
        # RuntimeProtWriteSeq should also be present (macro line stripped,
        # but the XML element itself remains)
        assert "RuntimeProtWriteSeq" in ea_names

    def test_bsw_implementation_extracted(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl_key = "Adc__bsw_impl"
        assert impl_key in result["modules"]
        mod = result["modules"][impl_key]
        assert mod["type"] == "BSW-IMPLEMENTATION"
        assert mod["programming_language"] == "C"
        assert mod["vendor_id"] == "17"

    def test_memory_sections_extracted(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl = result["modules"]["Adc__bsw_impl"]
        ms_names = [ms["short_name"] for ms in impl["memory_sections"]]
        assert "ADC_CONST_ASIL_D_8" in ms_names
        assert "ADC_CONST_ASIL_D_16" in ms_names

    def test_memory_section_details(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl = result["modules"]["Adc__bsw_impl"]
        ms8 = next(
            ms for ms in impl["memory_sections"]
            if ms["short_name"] == "ADC_CONST_ASIL_D_8"
        )
        assert ms8["alignment"] == "8"
        assert ms8["symbol"] == "CONST_ASIL_D_8"
        assert "CONST" in ms8["sw_addr_method_ref"]

    def test_section_name_prefixes_extracted(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl = result["modules"]["Adc__bsw_impl"]
        assert len(impl["section_name_prefixes"]) >= 1
        assert impl["section_name_prefixes"][0]["symbol"] == "ADC"

    def test_behavior_ref_captured(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl = result["modules"]["Adc__bsw_impl"]
        assert impl["behavior_ref"] == "/AUTOSAR_Adc/BswModuleDescriptions/Adc/AdcBehavior"

    def test_vendor_module_def_ref(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl = result["modules"]["Adc__bsw_impl"]
        assert impl["vendor_module_def_ref"] == "/AURIX3G/EcucDefs/Adc"

    def test_inline_macro_stripped_from_sw_version(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        impl = result["modules"]["Adc__bsw_impl"]
        # The inline [!"$moduleSoftwareVer"!] was stripped, so sw_version
        # should be None or empty
        assert impl["sw_version"] is None or impl["sw_version"] == ""


# ===================================================================
# Chunk generation tests
# ===================================================================

class TestChunks:
    """Tests for the flat chunk list."""

    def test_chunks_non_empty(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert len(result["chunks"]) > 0

    def test_chunk_has_required_keys(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        for chunk in result["chunks"]:
            assert "path" in chunk
            assert "type" in chunk
            assert "content" in chunk

    def test_container_chunks_have_path(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        container_chunks = [
            c for c in result["chunks"]
            if c["type"] == "ECUC-CONTAINER-VALUE"
        ]
        assert len(container_chunks) > 0
        for c in container_chunks:
            assert c["path"].startswith("/")

    def test_parameter_appears_in_chunk_content(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        core_chunks = [
            c for c in result["chunks"]
            if "CoreDef_0" in c["path"]
        ]
        assert len(core_chunks) == 1
        assert "EcucCoreId" in core_chunks[0]["content"]
        assert "1" in core_chunks[0]["content"]

    def test_memory_section_chunks(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        ms_chunks = [c for c in result["chunks"] if c["type"] == "MEMORY-SECTION"]
        assert len(ms_chunks) >= 2
        names = [c["content"] for c in ms_chunks]
        assert any("ADC_CONST_ASIL_D_8" in n for n in names)

    def test_exclusive_area_chunks(self, template_arxml_file):
        result = arxml_parser.parse(template_arxml_file)
        ea_chunks = [c for c in result["chunks"] if c["type"] == "EXCLUSIVE-AREA"]
        assert len(ea_chunks) >= 1

    def test_statistics_total_chunks(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert result["statistics"]["total_chunks"] == len(result["chunks"])


# ===================================================================
# Cross-reference tests
# ===================================================================

class TestCrossReferences:
    """Tests for DEFINITION-REF / VALUE-REF collection."""

    def test_definition_refs_collected(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert len(result["cross_references"]) > 0

    def test_cross_ref_has_required_keys(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        for cr in result["cross_references"]:
            assert "source_path" in cr
            assert "target_ref" in cr
            assert "ref_type" in cr

    def test_module_definition_ref_captured(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        mod_refs = [
            cr for cr in result["cross_references"]
            if cr["ref_type"] == "definition"
            and cr["target_ref"] == "/AURIX3G/EcucDefs/EcuC"
        ]
        assert len(mod_refs) >= 1

    def test_param_definition_refs_captured(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        param_refs = [
            cr for cr in result["cross_references"]
            if cr["ref_type"] == "param_definition"
        ]
        assert len(param_refs) > 0
        core_id_refs = [
            r for r in param_refs
            if "EcucCoreId" in r["target_ref"]
        ]
        assert len(core_id_refs) >= 1

    def test_value_refs_captured(self, ref_arxml_file):
        result = arxml_parser.parse(ref_arxml_file)
        val_refs = [
            cr for cr in result["cross_references"]
            if cr["ref_type"] == "reference_value"
        ]
        assert len(val_refs) >= 1
        assert any(
            "/TestPkg/TestModule/HwUnit_0" in r["target_ref"]
            for r in val_refs
        )


# ===================================================================
# Reference-values in containers
# ===================================================================

class TestReferenceValues:
    """Tests for REFERENCE-VALUES extraction."""

    def test_reference_values_extracted(self, ref_arxml_file):
        result = arxml_parser.parse(ref_arxml_file)
        mod = result["modules"]["TestModule"]
        ch = mod["containers"][0]
        assert len(ch["references"]) == 1
        ref = ch["references"][0]
        assert ref["value_ref"] == "/TestPkg/TestModule/HwUnit_0"

    def test_textual_param_extracted(self, ref_arxml_file):
        result = arxml_parser.parse(ref_arxml_file)
        mod = result["modules"]["TestModule"]
        ch = mod["containers"][0]
        assert ch["parameters"][0]["name"] == "ChannelName"
        assert ch["parameters"][0]["param_type"] == "STRING"
        assert ch["parameters"][0]["value"] == "ADC_CH0"


# ===================================================================
# Statistics tests
# ===================================================================

class TestStatistics:
    """Tests for the statistics dict."""

    def test_statistics_keys(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        stats = result["statistics"]
        expected = {
            "total_modules", "total_containers", "total_parameters",
            "total_references", "total_chunks", "template_macros_stripped",
        }
        assert expected == set(stats.keys())

    def test_module_count(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        assert result["statistics"]["total_modules"] == 1

    def test_container_count(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        # 2 top-level + 2 core defs + 1 partition = 5
        assert result["statistics"]["total_containers"] == 5

    def test_parameter_count(self, pure_arxml_file):
        result = arxml_parser.parse(pure_arxml_file)
        # 2 EcucCoreId + 1 PartitionCanBeRestarted = 3
        assert result["statistics"]["total_parameters"] == 3


# ===================================================================
# Error handling
# ===================================================================

class TestErrorHandling:
    """Tests for graceful error handling."""

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            arxml_parser.parse("/nonexistent/path.arxml")

    def test_malformed_xml(self, tmp_path):
        bad = tmp_path / "bad.arxml"
        bad.write_text("<AUTOSAR><broken", encoding="utf-8")
        with pytest.raises(ET.ParseError):
            arxml_parser.parse(str(bad))

    def test_empty_autosar(self, tmp_path):
        empty = tmp_path / "empty.arxml"
        empty.write_text(
            '<?xml version="1.0"?>'
            '<AUTOSAR xmlns="http://autosar.org/schema/r4.0"></AUTOSAR>',
            encoding="utf-8",
        )
        result = arxml_parser.parse(str(empty))
        assert result["modules"] == {}
        assert result["chunks"] == []
        assert result["statistics"]["total_modules"] == 0


# ===================================================================
# Template macro stripping unit tests
# ===================================================================

class TestMacroStripping:
    """Direct tests for _strip_template_macros."""

    def test_block_macros_stripped(self):
        text = '[!IF "cond"!]\n<elem/>\n[!ENDIF!]'
        cleaned, is_tpl, count = arxml_parser._strip_template_macros(text)
        assert is_tpl is True
        assert count == 2  # IF + ENDIF
        assert "[!IF" not in cleaned
        assert "[!ENDIF" not in cleaned
        assert "<elem/>" in cleaned

    def test_inline_macros_stripped(self):
        text = '<VALUE>[!"$myVar"!]</VALUE>'
        cleaned, is_tpl, count = arxml_parser._strip_template_macros(text)
        assert is_tpl is True
        assert '[!"$myVar"!]' not in cleaned
        assert "<VALUE>" in cleaned

    def test_pure_xml_unchanged(self):
        text = "<AUTOSAR><AR-PACKAGES/></AUTOSAR>"
        cleaned, is_tpl, count = arxml_parser._strip_template_macros(text)
        assert is_tpl is False
        assert count == 0
        assert cleaned == text

    def test_nocode_blocks_stripped(self):
        text = "[!NOCODE!]\n  [!VAR \"x\" = \"1\"!]\n[!ENDNOCODE!]\n<AUTOSAR/>"
        cleaned, is_tpl, count = arxml_parser._strip_template_macros(text)
        assert is_tpl is True
        assert "<AUTOSAR/>" in cleaned
