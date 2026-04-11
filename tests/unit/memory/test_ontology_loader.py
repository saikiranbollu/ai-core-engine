"""
Unit tests — OntologyLoader
============================
Verifies that OntologyLoader correctly reads ontology.yaml and
exposes typed accessors for profiles, node types, relationships, etc.
"""

import pytest
from pathlib import Path

from src.MemoryLayer.memory.ontology_loader import OntologyLoader

# ontology.yaml sits at src/HybridRAG/config/ontology.yaml
_ONTOLOGY_PATH = Path(__file__).resolve().parents[3] / "src" / "HybridRAG" / "config" / "ontology.yaml"


@pytest.fixture(scope="module")
def ontology() -> OntologyLoader:
    assert _ONTOLOGY_PATH.exists(), f"ontology.yaml not found at {_ONTOLOGY_PATH}"
    return OntologyLoader(str(_ONTOLOGY_PATH))


class TestOntologyMetadata:
    def test_version_is_string(self, ontology: OntologyLoader):
        assert isinstance(ontology.version, str)
        assert len(ontology.version) > 0

    def test_domain_is_string(self, ontology: OntologyLoader):
        assert isinstance(ontology.domain, str)

    def test_has_illd_profile(self, ontology: OntologyLoader):
        assert "illd" in ontology.available_profiles


class TestNodeTypes:
    def test_illd_has_node_types(self, ontology: OntologyLoader):
        names = ontology.get_node_type_names("illd")
        assert isinstance(names, list)
        assert len(names) > 0

    def test_expected_node_types_present(self, ontology: OntologyLoader):
        names = set(ontology.get_node_type_names("illd"))
        for expected in ["Function", "Struct", "Enum", "Typedef", "EnumValue", "Requirement", "Register", "BitField", "HardwareRegister", "RegisterField"]:
            assert expected in names, f"Missing expected node type: {expected}"

    def test_node_type_has_properties(self, ontology: OntologyLoader):
        node_types = ontology.get_node_types("illd")
        for nt in node_types:
            assert "name" in nt or "label" in nt, f"Node type missing name/label key: {nt}"


class TestRelationships:
    def test_illd_has_relationships(self, ontology: OntologyLoader):
        rels = ontology.get_relationships("illd")
        assert isinstance(rels, list)
        assert len(rels) > 0


class TestSupportedModules:
    def test_supported_modules_exist(self, ontology: OntologyLoader):
        modules = ontology.get_supported_modules("illd")
        assert isinstance(modules, list)
        assert len(modules) > 0

    def test_cxpi_is_supported(self, ontology: OntologyLoader):
        modules = [m.lower() for m in ontology.get_supported_modules("illd")]
        assert "cxpi" in modules


class TestInvalidProfile:
    def test_unknown_profile_raises(self, ontology: OntologyLoader):
        with pytest.raises(Exception):
            ontology.get_node_type_names("nonexistent_profile_xyz")
