"""Interactive Neo4j Cloud explorer — list nodes and relationships."""
from neo4j import GraphDatabase

URI = "bolt+ssc://bolt-passthrough-neo4j-mcswai-legato.icp.infineon.com:443"
USER = "neo4j"
PASSWORD = "legato"
DATABASE = "neo4j"  # Community edition — single database


def get_node_labels(driver):
    with driver.session(database=DATABASE) as s:
        result = s.run("CALL db.labels()")
        return sorted([r["label"] for r in result])


def get_relationship_types(driver):
    with driver.session(database=DATABASE) as s:
        result = s.run("CALL db.relationshipTypes()")
        return sorted([r["relationshipType"] for r in result])


def get_node_counts(driver, labels):
    counts = {}
    with driver.session(database=DATABASE) as s:
        for label in labels:
            result = s.run(f"MATCH (n:`{label}`) RETURN count(n) AS c")
            counts[label] = result.single()["c"]
    return counts


def get_rel_counts(driver, types):
    counts = {}
    with driver.session(database=DATABASE) as s:
        for t in types:
            result = s.run(f"MATCH ()-[r:`{t}`]->() RETURN count(r) AS c")
            counts[t] = result.single()["c"]
    return counts


def main():
    print(f"\nConnecting to cloud Neo4j at {URI} ...")
    driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))
    driver.verify_connectivity()
    print("Connected!\n")

    print(f"{'='*60}")
    print(f"  NEO4J CLOUD — DATABASE: {DATABASE}")
    print(f"  {URI}")
    print(f"{'='*60}")

    # Node labels + counts
    labels = get_node_labels(driver)
    label_counts = get_node_counts(driver, labels)
    total_nodes = sum(label_counts.values())

    print(f"\n  NODE LABELS ({len(labels)}) — {total_nodes} total nodes")
    print(f"  {'-'*56}")
    for label in labels:
        print(f"    {label:45s} {label_counts[label]:>6}")

    # Relationship types + counts
    types = get_relationship_types(driver)
    rel_counts = get_rel_counts(driver, types)
    total_rels = sum(rel_counts.values())

    print(f"\n  RELATIONSHIP TYPES ({len(types)}) — {total_rels} total relationships")
    print(f"  {'-'*56}")
    for t in types:
        print(f"    {t:45s} {rel_counts[t]:>6}")

    # Sample data
    print(f"\n  SAMPLE FUNCTIONS (first 10)")
    print(f"  {'-'*56}")
    with driver.session(database=DATABASE) as s:
        funcs = s.run("MATCH (f:Function) RETURN f.name AS name ORDER BY f.name LIMIT 10").data()
        for f in funcs:
            print(f"    {f['name']}")

    print(f"\n  SAMPLE STRUCTS (first 10)")
    print(f"  {'-'*56}")
    with driver.session(database=DATABASE) as s:
        structs = s.run("MATCH (s:Struct) RETURN s.name AS name ORDER BY s.name LIMIT 10").data()
        for st in structs:
            print(f"    {st['name']}")

    print(f"\n  SAMPLE DEPENDS_ON (first 10)")
    print(f"  {'-'*56}")
    with driver.session(database=DATABASE) as s:
        deps = s.run(
            "MATCH (a:Function)-[:DEPENDS_ON]->(b:Function) "
            "RETURN a.name AS from_fn, b.name AS to_fn LIMIT 10"
        ).data()
        for d in deps:
            print(f"    {d['from_fn']:40s} -> {d['to_fn']}")

    print(f"\n{'='*60}\n")
    driver.close()


if __name__ == "__main__":
    main()
