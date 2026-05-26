/**
 * AI Core Engine — Website JavaScript
 */

// ── Section Navigation ───────────────────────────────────────────────────
function showSection(sectionId) {
    const homeScroll = document.getElementById('home-scroll');
    const docs = document.getElementById('docs');
    const about = document.getElementById('about');
    const getting = document.getElementById('getting-started');

    // Hide all non-home sections
    [docs, about, getting].forEach(el => { if (el) el.style.display = 'none'; });

    if (sectionId === 'home') {
        homeScroll.style.display = 'block';
        // Reset scroll to top of snap container
        homeScroll.scrollTo({ top: 0, behavior: 'smooth' });
    } else {
        homeScroll.style.display = 'none';
        const target = document.getElementById(sectionId);
        if (target) {
            target.style.display = 'block';
        }
    }

    // Update active nav link
    document.querySelectorAll('.nav-link').forEach(link => link.classList.remove('active'));
    const activeLink = document.querySelector(`.nav-link[href="#${sectionId}"]`);
    if (activeLink) activeLink.classList.add('active');

    // Close mobile menu
    document.getElementById('nav-menu').classList.remove('active');

    // Scroll page to top
    window.scrollTo({ top: 0, behavior: 'smooth' });

    // Load endpoints on first docs visit
    if (sectionId === 'docs' && !window._endpointsLoaded) {
        loadEndpoints();
    }
}

// ── Mobile Hamburger ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    const hamburger = document.querySelector('.hamburger');
    const navMenu = document.getElementById('nav-menu');

    if (hamburger) {
        hamburger.addEventListener('click', () => {
            navMenu.classList.toggle('active');
            hamburger.setAttribute('aria-expanded', navMenu.classList.contains('active'));
        });
    }

    // Handle hash navigation on load
    const hash = window.location.hash.replace('#', '');
    if (hash && ['home', 'docs', 'about', 'getting-started'].includes(hash)) {
        showSection(hash);
    }

    // ── Reveal-on-scroll (IntersectionObserver) ──────────────────────────
    const revealElements = document.querySelectorAll('.reveal');
    const revealObserver = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('revealed');
            }
        });
    }, { threshold: 0.15 });

    revealElements.forEach(el => revealObserver.observe(el));

    // Also observe inside the scroll-snap container
    const homeScroll = document.getElementById('home-scroll');
    if (homeScroll) {
        homeScroll.addEventListener('scroll', () => {
            revealElements.forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.top < window.innerHeight * 0.85) {
                    el.classList.add('revealed');
                }
            });
        });
    }
});

// ── Load Endpoints from OpenAPI ──────────────────────────────────────────
let _openApiSpec = null;
window._endpointsLoaded = false;

async function loadEndpoints() {
    const grid = document.getElementById('endpoints-grid');
    try {
        const resp = await fetch('/openapi.json');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        _openApiSpec = await resp.json();
        window._endpointsLoaded = true;
        renderEndpoints(_openApiSpec);
    } catch (err) {
        grid.innerHTML = `
            <div style="text-align:center;padding:3rem;grid-column:1/-1;">
                <i class="fas fa-exclamation-triangle" style="font-size:2rem;color:#e74c3c;"></i>
                <p style="margin-top:1rem;opacity:.7;">Could not load API spec. Make sure the server is running.</p>
                <p style="font-size:0.85rem;opacity:.5;">${err.message}</p>
            </div>`;
    }
}

function renderEndpoints(spec) {
    const grid = document.getElementById('endpoints-grid');
    if (!spec || !spec.paths) {
        grid.innerHTML = '<p>No endpoints found.</p>';
        return;
    }

    const cards = [];
    for (const [path, methods] of Object.entries(spec.paths)) {
        for (const [method, details] of Object.entries(methods)) {
            const tag = (details.tags && details.tags[0]) || 'Other';
            const tier = tag.toLowerCase();
            cards.push({
                path,
                method: method.toUpperCase(),
                summary: details.summary || path,
                description: details.description || '',
                tier,
                operationId: details.operationId || '',
                details,
            });
        }
    }

    window._allEndpoints = cards;
    renderEndpointCards(cards);
}

function renderEndpointCards(cards) {
    const grid = document.getElementById('endpoints-grid');
    if (cards.length === 0) {
        grid.innerHTML = '<p style="text-align:center;grid-column:1/-1;opacity:.6;">No matching endpoints.</p>';
        return;
    }

    grid.innerHTML = cards.map((card, idx) => `
        <div class="endpoint-card" onclick="openEndpointDetail(${idx})" data-tier="${card.tier}" data-name="${card.operationId}">
            <div class="endpoint-name">${card.operationId || card.path}</div>
            <div class="endpoint-summary">${truncate(card.summary, 100)}</div>
            <span class="endpoint-tier tier-${card.tier}">${card.tier}</span>
        </div>
    `).join('');
}

function truncate(str, max) {
    if (!str) return '';
    return str.length > max ? str.slice(0, max) + '…' : str;
}

// ── Endpoint Filtering ───────────────────────────────────────────────────
let _currentTier = 'all';

function setTierFilter(tier) {
    _currentTier = tier;
    document.querySelectorAll('.tier-filters .filter-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tier === tier);
    });
    filterEndpoints();
}

function filterEndpoints() {
    if (!window._allEndpoints) return;
    const query = (document.getElementById('endpoint-search').value || '').toLowerCase();
    const filtered = window._allEndpoints.filter(card => {
        const matchesTier = _currentTier === 'all' || card.tier === _currentTier;
        const matchesQuery = !query ||
            card.operationId.toLowerCase().includes(query) ||
            card.summary.toLowerCase().includes(query) ||
            card.path.toLowerCase().includes(query);
        return matchesTier && matchesQuery;
    });
    renderEndpointCards(filtered);
}

// ── Endpoint Detail Modal ────────────────────────────────────────────────

// Example prompts for each tool (operationId → sample usage)
const TOOL_EXAMPLES = {
    health_check: 'Use health_check to verify the AICE server is running.',
    search_nodes: 'Use search_nodes with query "IfxCan" and workspace_id "illd" to find CAN-related nodes.',
    search_database: 'Use search_database with query "SPI initialization sequence" and workspace_id "mcal".',
    get_node_by_id: 'Use get_node_by_id with node_id "Function:IfxCan_Can_initModule" and workspace_id "illd".',
    get_neighbors: 'Use get_neighbors with node_id "Function:IfxCan_Can_initModule", direction "outgoing", relationship_type "CALLS_INTERNALLY".',
    execute_cypher: 'Use execute_cypher with query "MATCH (f:Function)-[:ACCESSES_REGISTER]->(r:Register) WHERE f.name CONTAINS \'Adc\' RETURN f.name, r.name LIMIT 10".',
    query_api_function: 'Use query_api_function with function_name "IfxCan_Can_initModule" and module_name "CAN".',
    query_dependencies: 'Use query_dependencies with module_name "CAN" and workspace_id "illd".',
    find_requirement_traces: 'Use find_requirement_traces with requirement_id "REQ-CAN-001" and workspace_id "illd".',
    find_coverage_gaps: 'Use find_coverage_gaps with module_name "ADC" and workspace_id "illd".',
    get_coverage_report: 'Use get_coverage_report with module_name "SPI" and workspace_id "mcal".',
    build_traceability_matrix: 'Use build_traceability_matrix with module_name "CAN" and workspace_id "mcal".',
    get_ontology_schema: 'Use get_ontology_schema with workspace_id "illd" and include_live_stats true.',
    get_ontology_compliance: 'Use get_ontology_compliance with module_name "ADC" and ontology_profile "illd".',
    list_ontology_profiles: 'Use list_ontology_profiles to see available workspace profiles (illd, mcal).',
    list_available_modules: 'Use list_available_modules to get all supported modules.',
    get_graph_statistics: 'Use get_graph_statistics to see total node/relationship counts.',
    shortest_path: 'Use shortest_path with source_id "Function:IfxCan_Can_initModule" and target_id "Register:CAN_CLC" and workspace_id "illd".',
    detect_communities: 'Use detect_communities with module_name "CAN" to find clusters of related nodes.',
    visualize_subgraph: 'Use visualize_subgraph with center_node_id "Function:IfxSpi_SpiMaster_init" and depth 2.',
    get_distribution: 'Use get_distribution with property "node_type" and workspace_id "illd".',
    sandbox_upload: 'Use sandbox_upload to ingest a local file for graph analysis.',
    sandbox_diff: 'Use sandbox_diff to compare sandbox content against the main graph.',
    sandbox_status: 'Use sandbox_status to check the current sandbox session state.',
    sandbox_clear: 'Use sandbox_clear to remove all uploaded sandbox data.',
    session_start: 'Use session_start with description "Analyzing CAN module requirements".',
    session_end: 'Use session_end with session_id from the active session.',
    session_store: 'Use session_store with key "findings" and value "CAN driver has 3 uncovered requirements".',
    session_retrieve: 'Use session_retrieve with key "findings" to get stored session data.',
    submit_human_feedback: 'Use submit_human_feedback with result_id "res-123", rating "positive", comment "Correct traceability link".',
    get_learning_metrics: 'Use get_learning_metrics to see how the system has improved from feedback.',
    get_failure_patterns: 'Use get_failure_patterns to identify common query failure modes.',
    build_context: 'Use build_context with query "How does the ADC module handle interrupts?" and workspace_id "illd".',
    query_enhance: 'Use query_enhance with query "find SPI functions" to get an improved, expanded query.',
    evaluate_confidence: 'Use evaluate_confidence with result_id "res-456" to get a confidence score.',
    process_results: 'Use process_results with raw_results from a previous search to post-process and rank them.',
    complete_review: 'Use complete_review with review_id "rev-001" and decision "approved".',
    get_review_analytics: 'Use get_review_analytics to see review throughput and approval rates.',
    override_review_routing: 'Use override_review_routing with module_name "CAN" and reviewer "john.doe@infineon.com".',
    validate_entity: 'Use validate_entity with node_id "Function:IfxAdc_Adc_init" and workspace_id "illd".',
    validate_api_usage: 'Use validate_api_usage with function_name "IfxCan_Can_initModule" and code_snippet "IfxCan_Can_initModule(&config);".',
    get_function_hsi: 'Use get_function_hsi with function_name "IfxGtm_Tom_Timer_init" and module_name "Timer".',
    analyze_hw_sw_links: 'Use analyze_hw_sw_links with module_name "ADC" to find hardware-software interface connections.',
    detect_polling_requirements: 'Use detect_polling_requirements with module_name "CAN" to find register polling patterns.',
    generate_initialization_code: 'Use generate_initialization_code with module_name "SPI" and peripheral "QSPI0".',
    get_type_definition: 'Use get_type_definition with type_name "IfxCan_Can_Config" and workspace_id "illd".',
    rlm_orchestrate: 'Use rlm_orchestrate with goal "Find all untested requirements in the CAN module".',
    rlm_plan_preview: 'Use rlm_plan_preview with goal "Trace REQ-001 to source code" to see the execution plan.',
    cache_stats: 'Use cache_stats to check cache hit/miss ratios.',
    cache_get: 'Use cache_get with key "module:CAN:functions" to retrieve cached data.',
    cache_clear: 'Use cache_clear to flush the entire cache.',
    cache_invalidate_module: 'Use cache_invalidate_module with module_name "ADC" after data updates.',
    cache_refresh_config: 'Use cache_refresh_config to reload cache configuration.',
    get_token_info: 'Use get_token_info to check your current authentication token details.',
    ensure_valid_token: 'Use ensure_valid_token to refresh or validate your session token.',
};

function getToolExample(operationId) {
    if (!operationId) return null;
    // Try exact match, then try with common prefixes stripped
    return TOOL_EXAMPLES[operationId] ||
           TOOL_EXAMPLES[operationId.replace(/^(get_|list_|find_|build_|detect_|validate_|search_)/, '')] ||
           null;
}

function buildExampleHtml(operationId) {
    const example = getToolExample(operationId);
    if (!example) return '';
    return `
        <details class="example-dropdown">
            <summary><i class="fas fa-lightbulb"></i> Example Usage</summary>
            <div class="example-content">
                <code>${example}</code>
            </div>
        </details>`;
}

function openEndpointDetail(idx) {
    const card = window._allEndpoints[idx];
    if (!card) return;

    const modal = document.getElementById('endpoint-modal');
    const title = document.getElementById('endpoint-modal-title');
    const body = document.getElementById('endpoint-modal-body');

    title.textContent = card.operationId || card.path;

    // Build parameter table
    let paramsHtml = '';
    const schema = card.details.requestBody?.content?.['application/json']?.schema;
    if (schema && schema.$ref && _openApiSpec) {
        const refName = schema.$ref.replace('#/components/schemas/', '');
        const schemaDef = _openApiSpec.components?.schemas?.[refName];
        if (schemaDef && schemaDef.properties) {
            const required = schemaDef.required || [];
            paramsHtml = `
                <div class="detail-section">
                    <label>Parameters</label>
                    <table class="param-table">
                        <thead><tr><th>Name</th><th>Type</th><th>Required</th><th>Default</th></tr></thead>
                        <tbody>
                            ${Object.entries(schemaDef.properties).map(([name, prop]) => `
                                <tr>
                                    <td><code>${name}</code></td>
                                    <td>${prop.type || 'string'}</td>
                                    <td>${required.includes(name) ? '✓' : ''}</td>
                                    <td>${prop.default !== undefined ? prop.default : '—'}</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>`;
        }
    }

    body.innerHTML = `
        <div class="detail-section">
            <label>Endpoint</label>
            <p><code>${card.method} ${card.path}</code></p>
        </div>
        <div class="detail-section">
            <label>Access Tier</label>
            <span class="endpoint-tier tier-${card.tier}">${card.tier}</span>
        </div>
        <div class="detail-section">
            <label>Summary</label>
            <p>${card.summary}</p>
        </div>
        ${card.description ? `<div class="detail-section"><label>Description</label><p>${card.description}</p></div>` : ''}
        ${paramsHtml}
        ${buildExampleHtml(card.operationId)}
        <div class="detail-section">
            <label>Responses</label>
            <p><code>200</code> — Successful tool invocation</p>
            <p><code>403</code> — Permission denied (RBAC)</p>
        </div>
    `;

    modal.classList.add('active');
}

function closeEndpointModal() {
    document.getElementById('endpoint-modal').classList.remove('active');
}

// ── Image Modal ──────────────────────────────────────────────────────────
function openImageModal(src) {
    const modal = document.getElementById('image-modal');
    document.getElementById('image-modal-img').src = src;
    modal.classList.add('active');
}

function closeImageModal() {
    document.getElementById('image-modal').classList.remove('active');
}

// Close modals on backdrop click or Escape
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('active');
    }
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.modal.active').forEach(m => m.classList.remove('active'));
    }
});
