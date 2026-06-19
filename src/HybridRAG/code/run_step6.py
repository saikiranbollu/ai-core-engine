"""Minimal runner for ADC source code KG ingestion (step 6)."""
import sys, os, importlib.util
from dotenv import load_dotenv

# Load .env
load_dotenv(r'c:\git-repos\ai-core-engine\env\.env')

# Add the KG directory to path for imports
kg_dir = r'c:\git-repos\ai-core-engine\src\HybridRAG\code\KG'
sys.path.insert(0, kg_dir)

# Load modules from .pyc that have no .py source (deleted)
pyc_dir = os.path.join(kg_dir, '__pycache__')
# Load in dependency order: _kg_safety first, then others
load_order = ['_kg_safety', 'incremental_tracker', 'config_struct_resolver',
              'dependency_fetcher', 'sfr_parsers']
for mod_name in load_order:
    pyc_path = os.path.join(pyc_dir, f'{mod_name}.cpython-312.pyc')
    if os.path.exists(pyc_path) and mod_name not in sys.modules:
        py_path = os.path.join(kg_dir, mod_name + '.py')
        if not os.path.exists(py_path):
            spec = importlib.util.spec_from_file_location(mod_name, pyc_path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception as e:
                print(f"Warning: {mod_name} load failed: {e}")

# Now import and run
from build_knowledge_graph import SourceCodeKnowledgeGraphBuilder
from pathlib import Path

src_dir = Path(r'C:\git-repos\ai-core-engine\src\HybridRAG\temp\temporary_data\aurix3g_sw_mcal_tc4xx_adc_src')
temp_dir = Path(r'c:\git-repos\ai-core-engine\src\HybridRAG\temp\src_adc')

neo4j_cfg = {
    'uri': 'bolt+ssc://bolt-passthrough-neo4j-ai-core-engine-mcal.icp.infineon.com:443',
    'username': 'neo4j',
    'password': os.environ.get('NEO4J_PASSWORD', 'legato'),
    'database': 'neo4j',
}

builder = SourceCodeKnowledgeGraphBuilder(
    neo4j_cfg=neo4j_cfg,
    module='ADC',
    source_dir=src_dir,
    temp_dir=temp_dir,
    sum_mode=True,
    sum_configs=['AS460_TC499N_STD_Host_Config1', 'AS460_TC499N_STD_Host_Config2',
                 'AS460_TC499N_STD_Host_Config3', 'AS460_TC499N_STD_Host_Config4'],
    force_incremental=True,
    project='A3G',
    dry_run=False,  # Write to Neo4j for real
)
builder.build()
