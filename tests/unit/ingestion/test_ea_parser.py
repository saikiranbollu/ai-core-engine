"""Tests for the EA Model Parser (ea_parser).

All ``object_model`` interactions are mocked — no EA COM or real pyDump
directory is needed.
"""

from __future__ import annotations

import os
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Allow running from the project root
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from IngestionPipeline.parsers import ea_parser
from IngestionPipeline.parsers.ea_parser import _EAModelFlattener, _safe_str, _safe_attr


# ---------------------------------------------------------------------------
# Helpers — lightweight stubs that mimic object_model objects
# ---------------------------------------------------------------------------

class _Stub:
    """
    Generic attribute-bag that behaves like an object_model data class.
    Any kwarg passed to the constructor becomes an attribute.
    """

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _make_param(**overrides):
    defaults = dict(
        Name="ConfigPtr", Type="const Adc_ConfigType*", Direction="in",
        Description="Pointer to config", GUID="{P-GUID}", IsConstPtr=True,
        Const=True, Transient=False, ReferenceType="pointer",
        Containment="By Reference", Range=None,
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_function(**overrides):
    defaults = dict(
        Name="Adc_Init", GUID="{F-GUID}", FQN="Model/Adc/Init",
        Description="Init the ADC driver", Source="IFX",
        File=["Adc.h"], ASIL="ASIL-B", ServiceID="0x00",
        Synchronous="Synchronous", Reentrant="Non Reentrant",
        ReentrantDesc="", FunctionType="Standard",
        CallType="Regular", Body="", ReturnType="void",
        ReturnType_Reference="", ReturnType_Description="None",
        Algorithm="", Comments="", Events="", UserHint="",
        Range=None,
        MemorySection=_Stub(Name="ADC_CODE"),
        PreprocessorFragment=None,
        Parameters=[_make_param()],
        ErrorHandling=[],
        ConfigDependencies=[],
        DesignDecisions=[],
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_interface(**overrides):
    func = overrides.pop("Function", _make_function())
    return _Stub(Function=func, **overrides)


def _make_structure(**overrides):
    member = _Stub(
        Name="pSfrBaseAddr", Type="volatile void*",
        IsConst=False, IsVolatile=True, Containment="By Value",
        Multiplicity="1", Range=None, PreprocessorFragment=None,
        P1Const=None, P1Transient=None, P2Const=None, P2Transient=None,
    )
    defaults = dict(
        Name="Adc_ConfigType", GUID="{S-GUID}", FQN="Model/Adc/Config",
        File=["Adc.h"], Source="IFX", Description="Config structure",
        StructureMembers=[member], DesignDecisions=[], PreprocessorFragment=None,
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_enum(**overrides):
    literal = _Stub(Name="ADC_IDLE", Value="0", PreprocessorFragment=None)
    defaults = dict(
        Name="Adc_StatusType", GUID="{E-GUID}", FQN="Model/Adc/Status",
        File=["Adc.h"], Source="IFX", Type="uint8",
        EnumRange=[literal], DesignDecisions=[], PreprocessorFragment=None,
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_datatypes(**overrides):
    defaults = dict(
        Structures=[_make_structure()],
        Typedefs=[],
        Enumerations=[_make_enum()],
        FunctionPointers=[],
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_macro(**overrides):
    defaults = dict(
        Name="ADC_MODULE_ID", GUID="{M-GUID}", FQN="Model/Adc/Macros",
        File=["Adc.h"], Source="IFX", Type="Functional Macro",
        Value="123", Algorithm="", Asserts="", Multiplicity="1",
        DesignDecisions=[], PreprocessorFragment=None,
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_global(**overrides):
    defaults = dict(
        Name="Adc_DriverState", GUID="{G-GUID}", FQN="Model/Adc/Globals",
        File=["Adc.c"], Source="IFX", Type="Adc_StatusType",
        Scope="Static", IsConst=False, IsVolatile=True,
        Containment="By Value", Multiplicity="1",
        MemorySection=_Stub(Name="ADC_VAR"), Range=None,
        DesignDecisions=[], PreprocessorFragment=None,
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_error_code(**overrides):
    defaults = dict(
        Name="ADC_E_UNINIT", GUID="{EC-GUID}", FQN="Model/Adc/Errors",
        File=["Adc.h"], Source="IFX", ErrorValue="0x01",
        ErrorType="Development",
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_memory_section(**overrides):
    defaults = dict(
        Name="ADC_CODE", GUID="{MS-GUID}", FQN="Model/Adc/MemSec",
        Source="IFX", Description="Code section",
    )
    defaults.update(overrides)
    return _Stub(**defaults)


def _make_component(**overrides):
    defaults = dict(
        Name="Adc", GUID="{C-GUID}", FQN="Model/Components/Product/Adc",
        Description="ADC Driver", Source="IFX", Variant="Variant PB",
        ModuleID=123, ASIL="ASIL-B",
        Interfaces=_Stub(ProvidedInterfaces=[_make_interface()]),
        LocalInterfaces=[_make_interface(Function=_make_function(Name="Adc_lBusyWait"))],
        Datatypes=_make_datatypes(),
        Macros=[_make_macro()],
        Globals=[_make_global()],
        ErrorCodes=[_make_error_code()],
        MemorySections=[_make_memory_section()],
        FileStructure=None,
        ConfigInterface=None,
        ConfigStructs=[],
        SafetyAous=None,
        SecurityAous=None,
        DesignDecisions=None,
        DesignInformation=None,
        ArchitecturalDecisions=None,
        ArchitecturalInformation=None,
        HwSwInterface=None,
        SwSwInterface=None,
        DynamicViews=None,
        UserManual=None,
        ExternalDependencies=None,
        StaticData=None,
    )
    defaults.update(overrides)
    return _Stub(**defaults)


# ---------------------------------------------------------------------------
# Tests — _safe_str / _safe_attr helpers
# ---------------------------------------------------------------------------

class TestSafeHelpers:

    def test_safe_str_returns_value(self):
        s = _Stub(Name="Hello")
        assert _safe_str(s, "Name") == "Hello"

    def test_safe_str_returns_default_on_missing(self):
        s = _Stub()
        assert _safe_str(s, "Missing", "fallback") == "fallback"

    def test_safe_str_returns_default_on_none(self):
        s = _Stub(Name=None)
        assert _safe_str(s, "Name") == ""

    def test_safe_attr_returns_value(self):
        s = _Stub(Count=42)
        assert _safe_attr(s, "Count") == 42

    def test_safe_attr_returns_default_on_missing(self):
        s = _Stub()
        assert _safe_attr(s, "Count", -1) == -1


# ---------------------------------------------------------------------------
# Tests — _EAModelFlattener
# ---------------------------------------------------------------------------

class TestFlattener:

    def test_flatten_param(self):
        p = _make_param()
        result = _EAModelFlattener._flatten_param(p)
        assert result["name"] == "ConfigPtr"
        assert result["type"] == "const Adc_ConfigType*"
        assert result["direction"] == "in"
        assert result["is_const_ptr"] is True

    def test_flatten_function(self):
        f = _make_function()
        result = _EAModelFlattener._flatten_function(f)
        assert result["name"] == "Adc_Init"
        assert result["return_type"] == "void"
        assert result["memory_section"] == "ADC_CODE"
        assert len(result["parameters"]) == 1
        assert result["parameters"][0]["name"] == "ConfigPtr"

    def test_flatten_interface(self):
        iface = _make_interface()
        result = _EAModelFlattener._flatten_interface(iface)
        assert result["name"] == "Adc_Init"

    def test_flatten_structure(self):
        s = _make_structure()
        result = _EAModelFlattener._flatten_structure(s)
        assert result["name"] == "Adc_ConfigType"
        assert len(result["members"]) == 1
        assert result["members"][0]["name"] == "pSfrBaseAddr"

    def test_flatten_enumeration(self):
        e = _make_enum()
        result = _EAModelFlattener._flatten_enumeration(e)
        assert result["name"] == "Adc_StatusType"
        assert len(result["literals"]) == 1
        assert result["literals"][0]["name"] == "ADC_IDLE"

    def test_flatten_macro(self):
        m = _make_macro()
        result = _EAModelFlattener._flatten_macro(m)
        assert result["name"] == "ADC_MODULE_ID"
        assert result["value"] == "123"

    def test_flatten_global(self):
        g = _make_global()
        result = _EAModelFlattener._flatten_global(g)
        assert result["name"] == "Adc_DriverState"
        assert result["memory_section"] == "ADC_VAR"
        assert result["is_volatile"] is True

    def test_flatten_error_code(self):
        ec = _make_error_code()
        result = _EAModelFlattener._flatten_error_code(ec)
        assert result["name"] == "ADC_E_UNINIT"
        assert result["error_value"] == "0x01"

    def test_flatten_memory_section(self):
        ms = _make_memory_section()
        result = _EAModelFlattener._flatten_memory_section(ms)
        assert result["name"] == "ADC_CODE"

    def test_flatten_datatypes(self):
        dt = _make_datatypes()
        result = _EAModelFlattener._flatten_datatypes(dt)
        assert len(result["structures"]) == 1
        assert len(result["enumerations"]) == 1
        assert result["typedefs"] == []
        assert result["function_pointers"] == []

    def test_flatten_datatypes_none(self):
        result = _EAModelFlattener._flatten_datatypes(None)
        assert result["structures"] == []

    def test_flatten_component_full(self):
        comp = _make_component()
        result = _EAModelFlattener.flatten_component(comp)

        assert result["component"] == "Adc"
        assert result["guid"] == "{C-GUID}"
        assert result["variant"] == "Variant PB"
        assert result["module_id"] == 123
        assert result["asil"] == "ASIL-B"

        assert len(result["interfaces"]) == 1
        assert result["interfaces"][0]["name"] == "Adc_Init"

        assert len(result["local_interfaces"]) == 1
        assert result["local_interfaces"][0]["name"] == "Adc_lBusyWait"

        assert len(result["datatypes"]["structures"]) == 1
        assert len(result["macros"]) == 1
        assert len(result["globals"]) == 1
        assert len(result["error_codes"]) == 1
        assert len(result["memory_sections"]) == 1

    def test_flatten_component_empty_sections(self):
        """Component with all optional sections set to None."""
        comp = _make_component(
            Interfaces=None, LocalInterfaces=None, Datatypes=None,
            Macros=None, Globals=None, ErrorCodes=None,
            MemorySections=None, ConfigStructs=None,
        )
        result = _EAModelFlattener.flatten_component(comp)
        assert result["component"] == "Adc"
        assert result["interfaces"] == []
        assert result["local_interfaces"] == []
        assert result["datatypes"]["structures"] == []
        assert result["macros"] == []
        assert result["globals"] == []

    def test_flatten_common(self):
        sub_comp = _make_component(Name="SubComp")
        common = _Stub(
            Name="Common",
            Components=[sub_comp],
            McalUsage=[
                _Stub(
                    ModuleName="Adc", Asil="ASIL-B", Variant="PB",
                    PartitionGranularity="Module",
                    HwDependencies=[_Stub(Name="ADC_HW", Access="RW")],
                    SwDependencies=[_Stub(Name="Mcu", Description="Clock dependency")],
                ),
            ],
            UserManual=None,
            StaticData=None,
            SafetyAous=None,
            SecurityAous=None,
        )
        result = _EAModelFlattener.flatten_common(common)
        assert result["component"] == "Common"
        assert len(result["sub_components"]) == 1
        assert result["sub_components"][0]["component"] == "SubComp"
        assert len(result["mcal_usage"]) == 1
        assert result["mcal_usage"][0]["module_name"] == "Adc"

    def test_compute_statistics(self):
        comp = _EAModelFlattener.flatten_component(_make_component())
        stats = _EAModelFlattener.compute_statistics([comp])
        assert stats["total_components"] == 1
        assert stats["total_functions"] == 1
        assert stats["total_local_functions"] == 1
        assert stats["total_structures"] == 1
        assert stats["total_enumerations"] == 1
        assert stats["total_macros"] == 1
        assert stats["total_globals"] == 1
        assert stats["total_error_codes"] == 1
        assert stats["total_memory_sections"] == 1


# ---------------------------------------------------------------------------
# Tests — preprocessor flattening
# ---------------------------------------------------------------------------

class TestPreprocessor:

    def test_flatten_none(self):
        assert _EAModelFlattener._flatten_preprocessor(None) is None

    def test_flatten_expression(self):
        expr = _Stub(LOperand="ADC_FEATURE", ROperand="STD_ON", Operator="==")
        ppf = _Stub(
            Modeled=_Stub(
                IsExpression=True,
                IsCompositeExpr=False,
                Expressions=_Stub(Expression=[expr]),
            ),
            FreeText=_Stub(IsAvailable=False),
        )
        result = _EAModelFlattener._flatten_preprocessor(ppf)
        assert result is not None
        assert len(result["expressions"]) == 1
        assert result["expressions"][0]["l_operand"] == "ADC_FEATURE"

    def test_flatten_freetext(self):
        ppf = _Stub(
            Modeled=_Stub(IsExpression=False, IsCompositeExpr=False),
            FreeText=_Stub(
                IsAvailable=True,
                PreCodeFragment="#if defined(ADC_FEATURE)",
                PostCodeFragment="#endif",
            ),
        )
        result = _EAModelFlattener._flatten_preprocessor(ppf)
        assert result is not None
        assert result["free_text"]["pre_code_fragment"] == "#if defined(ADC_FEATURE)"


# ---------------------------------------------------------------------------
# Tests — decisions flattening
# ---------------------------------------------------------------------------

class TestDecisions:

    def test_flatten_decision_with_children(self):
        child = _Stub(
            Name="SubDecision", GUID="{SD-GUID}", FQN="path",
            Type="Design", Description="child desc", Decisions=[],
        )
        parent = _Stub(
            Name="ParentDecision", GUID="{PD-GUID}", FQN="path",
            Type="Design", Description="parent desc", Decisions=[child],
        )
        result = _EAModelFlattener._flatten_decision(parent)
        assert result["name"] == "ParentDecision"
        assert len(result["children"]) == 1
        assert result["children"][0]["name"] == "SubDecision"


# ---------------------------------------------------------------------------
# Tests — file structure flattening
# ---------------------------------------------------------------------------

class TestFileStructure:

    def test_flatten_file_structure(self):
        header = _Stub(
            Name="Adc.h", GUID="{H-GUID}", FQN="path",
            Source="IFX", Description="Header", Version="1.0",
            Generate=True,
            Dependencies=[_Stub(Name="Std_Types.h", IncludePreference="Standard")],
        )
        fs = _Stub(
            CFileStructure=_Stub(
                Headerfiles=[header], Sourcefiles=[], ExtHeaderfiles=[],
                Images=[], GUID="{FS-GUID}",
            ),
            PluginFileStructure=None,
        )
        result = _EAModelFlattener._flatten_file_structure(fs)
        assert len(result["header_files"]) == 1
        assert result["header_files"][0]["name"] == "Adc.h"
        assert len(result["header_files"][0]["dependencies"]) == 1

    def test_flatten_none_file_structure(self):
        result = _EAModelFlattener._flatten_file_structure(None)
        assert result["header_files"] == []
        assert result["source_files"] == []


# ---------------------------------------------------------------------------
# Tests — public parse() API
# ---------------------------------------------------------------------------

class TestParseAPI:

    def test_parse_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="not found"):
            ea_parser.parse(
                "C:/nonexistent/path.eap",
                project="mcal",
                components=["Adc"],
            )

    def test_parse_pydump_mode(self, tmp_path):
        """Validate that parse() invokes object_model in Read mode for a directory."""
        comp = _make_component()

        # Create a fake pyDump directory
        pydump_dir = tmp_path / "pyDump"
        pydump_dir.mkdir()

        mock_parse_obj = MagicMock()
        mock_parse_obj.Status = True
        mock_parse_obj.ParsedModelList = [comp]

        with patch(
            "IngestionPipeline.parsers.ea_parser._EAModelExtractor.extract",
            return_value=[comp],
        ):
            result = ea_parser.parse(
                str(pydump_dir),
                project="mcal",
                components=["Adc"],
            )

        assert result["project"] == "mcal"
        assert len(result["components"]) == 1
        assert result["components"][0]["component"] == "Adc"
        assert result["common"] is None
        assert result["statistics"]["total_components"] == 1

    def test_parse_with_common(self, tmp_path):
        """Validate that Common component is separated from regular components."""
        comp = _make_component()
        common = _Stub(
            Name="Common",
            Components=[_make_component(Name="SubComp")],
            McalUsage=[],
            UserManual=None,
            StaticData=None,
            SafetyAous=None,
            SecurityAous=None,
        )

        pydump_dir = tmp_path / "pyDump"
        pydump_dir.mkdir()

        with patch(
            "IngestionPipeline.parsers.ea_parser._EAModelExtractor.extract",
            return_value=[comp, common],
        ):
            result = ea_parser.parse(
                str(pydump_dir),
                project="mcal",
                components=["Adc", "Common"],
            )

        assert len(result["components"]) == 1
        assert result["common"] is not None
        assert result["common"]["component"] == "Common"

    def test_parse_returns_correct_top_level_keys(self, tmp_path):
        pydump_dir = tmp_path / "pyDump"
        pydump_dir.mkdir()

        with patch(
            "IngestionPipeline.parsers.ea_parser._EAModelExtractor.extract",
            return_value=[_make_component()],
        ):
            result = ea_parser.parse(
                str(pydump_dir),
                project="mcal",
                components=["Adc"],
            )

        assert set(result.keys()) == {"project", "components", "common", "statistics"}


# ---------------------------------------------------------------------------
# Tests — _EAModelExtractor
# ---------------------------------------------------------------------------

class TestExtractor:

    def test_is_pydump_dir_detection(self, tmp_path):
        pydump = tmp_path / "pydump"
        pydump.mkdir()
        from IngestionPipeline.parsers.ea_parser import _EAModelExtractor
        ext = _EAModelExtractor(str(pydump), "mcal", ["Adc"])
        assert ext._is_pydump_dir is True
        assert ext._is_ea_file is False

    def test_is_ea_file_detection(self, tmp_path):
        eap_file = tmp_path / "model.eap"
        eap_file.touch()
        from IngestionPipeline.parsers.ea_parser import _EAModelExtractor
        ext = _EAModelExtractor(str(eap_file), "mcal", ["Adc"])
        assert ext._is_ea_file is True
        assert ext._is_pydump_dir is False

    def test_extract_raises_file_not_found(self):
        from IngestionPipeline.parsers.ea_parser import _EAModelExtractor
        ext = _EAModelExtractor("C:/nonexistent", "mcal", ["Adc"])
        with pytest.raises(FileNotFoundError):
            ext.extract()


# ---------------------------------------------------------------------------
# Tests — external dependencies flattening
# ---------------------------------------------------------------------------

class TestExternalDependencies:

    def test_flatten_none(self):
        result = _EAModelFlattener._flatten_external_dependencies(None)
        assert result["external_interfaces"] == {}

    def test_flatten_with_data(self):
        ext = _Stub(
            ExternalInterfaces={"Spi": [_make_interface()]},
            ExternalLocalInterface={},
            ExternalDatatypes={},
        )
        result = _EAModelFlattener._flatten_external_dependencies(ext)
        assert "Spi" in result["external_interfaces"]
        assert result["external_interfaces"]["Spi"][0]["name"] == "Adc_Init"


# ---------------------------------------------------------------------------
# Tests — config interface flattening
# ---------------------------------------------------------------------------

class TestConfigInterface:

    def test_flatten_config_container(self):
        param = _Stub(
            Name="AdcPrescale", GUID="{CP-GUID}", Description="Prescaler",
            Source="AUTOSAR", Scope="Module", Type="uint32", Default="0",
            Range=None, LowerMultiplicity="1", UpperMultiplicity="1",
            PBVariantMultiplicity="", PBVariantValue="",
            isPublished=True, MultiplicityConfigClass="", ValueConfigClass="",
            ConfigDependencies=[], CodegenDependencies={}, DesignDecisions=[],
        )
        container = _Stub(
            Name="AdcConfigSet", GUID="{CC-GUID}", Description="Config set",
            Source="AUTOSAR", Scope="Module", Type="Container",
            LowerMultiplicity="1", UpperMultiplicity="1",
            PBVariantMultiplicity="", isPublished=True,
            ConfigSubContainers=["AdcHwUnit"],
            ConfigDependencies=[], CodegenDependencies={},
            ConfigParameters=[param], DesignDecisions=[],
        )
        cfg = _Stub(ConfigContainers=[container], Images=[])
        result = _EAModelFlattener._flatten_config_interface(cfg)
        assert len(result) == 1
        assert result[0]["name"] == "AdcConfigSet"
        assert len(result[0]["config_parameters"]) == 1
        assert result[0]["config_parameters"][0]["name"] == "AdcPrescale"
