"""Unit tests for AutoStubGenerator."""

import textwrap
from pathlib import Path

import pytest

# Adjust sys.path so the parser package is importable
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "IngestionPipeline", "parsers"))

from auto_stub_generator import AutoStubGenerator, COMMON_HEADERS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def adc_source(tmp_path):
    """Create a mock ADC module source tree."""
    ssc_inc = tmp_path / "ssc" / "inc"
    ssc_src = tmp_path / "ssc" / "src"
    ssc_inc.mkdir(parents=True)
    ssc_src.mkdir(parents=True)

    # Minimal Adc.h
    (ssc_inc / "Adc.h").write_text(textwrap.dedent("""\
        #ifndef ADC_H
        #define ADC_H
        #include "Adc_Cfg.h"
        #include "Std_Types.h"
        void Adc_Init(void);
        #endif
    """))

    # Minimal Adc.c
    (ssc_src / "Adc.c").write_text(textwrap.dedent("""\
        #include "Adc.h"
        #include "Adc_Data.h"
        #include "Det.h"
        #include "Dem.h"
        #include "Mcal_SafetyError.h"
        #include "Dma.h"
        #include "Gtm.h"
        #include "Adc_MemMap.h"

        /* Version checks */
        #if (ADC_AR_RELEASE_MAJOR_VERSION != 4u)
          #error "Wrong AUTOSAR version!"
        #endif
        #if (ADC_AR_RELEASE_MINOR_VERSION != 6u)
          #error "Wrong AUTOSAR version!"
        #endif
        #if (ADC_SW_MAJOR_VERSION != 2u)
          #error "Wrong SW version!"
        #endif

        /* Feature checks */
        #if (ADC_DMA_RESULT_HANDLING == STD_ON)
        void Adc_DmaHandler(void) {}
        #endif

        #if (ADC_ENABLE_DIAGNOSTICS != STD_OFF)
        void Adc_Diag(void) {}
        #endif

        #if (ADC_DEV_ERR_REPORTING == STD_ON)
        void Adc_DetReport(void) {}
        #endif

        #if (ADC_SAFETY_ERR_REPORTING == STD_ON)
        void Adc_SafetyCheck(void) {}
        #endif

        /* DEM */
        #if (ADC_E_DMA_TRANSFER_FAILURE_DEM_REPORTING == STD_ON)
        void Adc_DemReport(void) {}
        #endif

        /* SchM calls */
        void Adc_Init(void) {
            SchM_Enter_Adc_AdcInternal();
            SchM_Exit_Adc_AdcInternal();
            SchM_Enter_Adc_AdcKernel();
            SchM_Exit_Adc_AdcKernel();
        }

        /* Constants */
        uint32 buffers[ADC_MAX_HW_UNITS];
        uint32 groups[ADC_MAX_GROUPS];

        /* Module ID check */
        #if (ADC_MODULE_ID != 123u)
          #error "Wrong module ID"
        #endif
        #if (ADC_VENDOR_ID != 17u)
          #error "Wrong vendor ID"
        #endif
    """))

    return tmp_path


@pytest.fixture
def gen(adc_source, tmp_path):
    """Return an AutoStubGenerator for the mock ADC module."""
    output = tmp_path / "generated_stubs"
    return AutoStubGenerator("ADC", adc_source, output)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMixedCaseDetection:
    def test_detects_from_header(self, gen):
        """Should detect 'Adc' from Adc.h in ssc/inc/."""
        gen.generate()
        assert gen.module_mixed == "Adc"

    def test_fallback_capitalise(self, tmp_path):
        """With no header files, falls back to capitalize()."""
        empty = tmp_path / "empty_src"
        empty.mkdir()
        out = tmp_path / "stubs_out"
        g = AutoStubGenerator("SPI", empty, out)
        g.generate()
        assert g.module_mixed == "Spi"


class TestVersionScanning:
    def test_extracts_version_defines(self, gen, adc_source):
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        versions = gen._scan_version_checks(content)
        assert "ADC_AR_RELEASE_MAJOR_VERSION" in versions
        assert versions["ADC_AR_RELEASE_MAJOR_VERSION"] == "4u"
        assert "ADC_AR_RELEASE_MINOR_VERSION" in versions
        assert versions["ADC_AR_RELEASE_MINOR_VERSION"] == "6u"
        assert "ADC_SW_MAJOR_VERSION" in versions
        assert versions["ADC_SW_MAJOR_VERSION"] == "2u"


class TestFeatureScanning:
    def test_collects_features(self, gen, adc_source):
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        features = gen._scan_feature_switches(content)
        assert "ADC_DMA_RESULT_HANDLING" in features
        assert "ADC_ENABLE_DIAGNOSTICS" in features

    def test_reporting_features_detected(self, gen, adc_source):
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        features = gen._scan_feature_switches(content)
        assert "ADC_DEV_ERR_REPORTING" in features
        assert "ADC_SAFETY_ERR_REPORTING" in features


class TestDemScanning:
    def test_dem_switches(self, gen, adc_source):
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        dem = gen._scan_dem_reporting(content)
        assert "ADC_E_DMA_TRANSFER_FAILURE_DEM_REPORTING" in dem


class TestSchMScanning:
    def test_exclusive_areas(self, gen, adc_source):
        gen.module_mixed = "Adc"
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        areas = gen._scan_schm_exclusive_areas(content)
        assert "AdcInternal" in areas
        assert "AdcKernel" in areas


class TestConstantScanning:
    def test_constants(self, gen, adc_source):
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        constants = gen._scan_constants(content)
        assert "ADC_MAX_HW_UNITS" in constants
        assert "ADC_MAX_GROUPS" in constants


class TestModuleIdScanning:
    def test_module_vendor_ids(self, gen, adc_source):
        content = (adc_source / "ssc" / "src" / "Adc.c").read_text()
        ids = gen._scan_module_ids(content)
        assert ids.get("ADC_MODULE_ID") == "123u"
        assert ids.get("ADC_VENDOR_ID") == "17u"


class TestFeatureClassification:
    def test_reporting_disabled(self):
        assert AutoStubGenerator._should_disable("ADC_DEV_ERR_REPORTING") is True
        assert AutoStubGenerator._should_disable("ADC_SAFETY_ERR_REPORTING") is True
        assert AutoStubGenerator._should_disable("ADC_E_DMA_TRANSFER_FAILURE_DEM_REPORTING") is True

    def test_partition_disabled(self):
        assert AutoStubGenerator._should_disable("ADC_PARTITION_ERR_CHECK") is True

    def test_functional_features_enabled(self):
        assert AutoStubGenerator._should_disable("ADC_DMA_RESULT_HANDLING") is False
        assert AutoStubGenerator._should_disable("ADC_ENABLE_DIAGNOSTICS") is False
        assert AutoStubGenerator._should_disable("ADC_HW_TRIGGER_API") is False


class TestFullGeneration:
    def test_generates_all_expected_files(self, gen):
        gen.generate()
        out = gen.output_dir
        expected = {
            "Adc_Cfg.h", "Adc_Data.h", "Adc_PBcfg.h",
            "Adc_CpuPrivMode.h", "Adc_MemMap.h", "Adc_Cbk.h",
            "SchM_Adc.h",
        }
        generated = {f.name for f in out.glob("*.h")}
        assert expected.issubset(generated), f"Missing: {expected - generated}"

    def test_cross_module_stubs_generated(self, gen):
        gen.generate()
        out = gen.output_dir
        generated = {f.name for f in out.glob("*.h")}
        assert "Dma.h" in generated
        assert "Gtm.h" in generated

    def test_common_headers_not_generated(self, gen):
        gen.generate()
        out = gen.output_dir
        generated = {f.name for f in out.glob("*.h")}
        for common in COMMON_HEADERS:
            assert common not in generated, f"Should not generate common header: {common}"

    def test_cfg_contains_versions(self, gen):
        gen.generate()
        cfg = (gen.output_dir / "Adc_Cfg.h").read_text()
        assert "ADC_AR_RELEASE_MAJOR_VERSION" in cfg
        assert "4u" in cfg

    def test_cfg_features_on_and_off(self, gen):
        gen.generate()
        cfg = (gen.output_dir / "Adc_Cfg.h").read_text()
        # Functional feature should be ON
        assert "#define ADC_DMA_RESULT_HANDLING   STD_ON" in cfg
        # Reporting features should be OFF
        assert "#define ADC_DEV_ERR_REPORTING   STD_OFF" in cfg

    def test_cfg_dem_reporting_off(self, gen):
        gen.generate()
        cfg = (gen.output_dir / "Adc_Cfg.h").read_text()
        assert "#define ADC_E_DMA_TRANSFER_FAILURE_DEM_REPORTING   STD_OFF" in cfg

    def test_schm_has_exclusive_areas(self, gen):
        gen.generate()
        schm = (gen.output_dir / "SchM_Adc.h").read_text()
        assert "SchM_Enter_Adc_AdcInternal" in schm
        assert "SchM_Exit_Adc_AdcInternal" in schm
        assert "SchM_Enter_Adc_AdcKernel" in schm

    def test_data_h_includes_module_header(self, gen):
        gen.generate()
        data = (gen.output_dir / "Adc_Data.h").read_text()
        assert '#include "Adc.h"' in data
        assert '#include "SchM_Adc.h"' in data

    def test_cross_module_stub_has_guard(self, gen):
        gen.generate()
        dma = (gen.output_dir / "Dma.h").read_text()
        assert "#ifndef DMA_H" in dma
        assert '#include "Std_Types.h"' in dma

    def test_own_header_not_shadowed(self, gen):
        """Headers already in ssc/inc should not be generated."""
        gen.generate()
        out = gen.output_dir
        generated = {f.name for f in out.glob("*.h")}
        # Adc.h exists in ssc/inc — should NOT be generated as a stub
        assert "Adc.h" not in generated


class TestModuleAgnostic:
    """Generate stubs for a different module to verify module-agnosticism."""

    def test_spi_module(self, tmp_path):
        ssc_inc = tmp_path / "spi_src" / "ssc" / "inc"
        ssc_src = tmp_path / "spi_src" / "ssc" / "src"
        ssc_inc.mkdir(parents=True)
        ssc_src.mkdir(parents=True)

        (ssc_inc / "Spi.h").write_text("#ifndef SPI_H\n#define SPI_H\n#endif\n")
        (ssc_src / "Spi.c").write_text(textwrap.dedent("""\
            #include "Spi.h"
            #include "Spi_Cfg.h"
            #include "Spi_Data.h"
            #if (SPI_AR_RELEASE_MAJOR_VERSION != 4u)
              #error "Bad version"
            #endif
            #if (SPI_CHANNEL_BUFFERS_ALLOWED == STD_ON)
            void Spi_Buf(void) {}
            #endif
            void Spi_Init(void) {
                SchM_Enter_Spi_SpiInternal();
                SchM_Exit_Spi_SpiInternal();
            }
        """))

        out = tmp_path / "spi_stubs"
        gen = AutoStubGenerator("SPI", tmp_path / "spi_src", out)
        gen.generate()

        assert gen.module_mixed == "Spi"
        generated = {f.name for f in out.glob("*.h")}
        assert "Spi_Cfg.h" in generated
        assert "Spi_Data.h" in generated
        assert "SchM_Spi.h" in generated

        cfg = (out / "Spi_Cfg.h").read_text()
        assert "SPI_AR_RELEASE_MAJOR_VERSION" in cfg
        assert "SPI_CHANNEL_BUFFERS_ALLOWED" in cfg

        schm = (out / "SchM_Spi.h").read_text()
        assert "SchM_Enter_Spi_SpiInternal" in schm


class TestTresosTemplateSkipping:
    """Tresos EB template files ([! syntax) should be skipped."""

    def test_skips_template_files(self, tmp_path):
        src = tmp_path / "mod_src"
        ssc = src / "ssc" / "src"
        ssc.mkdir(parents=True)
        (src / "ssc" / "inc").mkdir(parents=True)
        (src / "ssc" / "inc" / "Mod.h").write_text("#ifndef MOD_H\n#define MOD_H\n#endif\n")

        # Real C file
        (ssc / "Mod.c").write_text(textwrap.dedent("""\
            #include "Mod.h"
            #if (MOD_FEAT_A == STD_ON)
            void Mod_A(void) {}
            #endif
        """))

        # Tresos template (should be skipped)
        tmpl = src / "Plugins" / "generate" / "template"
        tmpl.mkdir(parents=True)
        (tmpl / "Mod_Cfg.h").write_text("[!IF \"ModGeneral/Feat\" = 'true'!]\n#define MOD_FAKE STD_ON\n[!ENDIF!]\n")

        out = tmp_path / "mod_stubs"
        gen = AutoStubGenerator("MOD", src, out)
        gen.generate()

        cfg = (out / "Mod_Cfg.h").read_text()
        assert "MOD_FEAT_A" in cfg
        # The Tresos template define should NOT appear
        assert "MOD_FAKE" not in cfg
