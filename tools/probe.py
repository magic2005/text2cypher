"""One-off read-only discovery of the teacher graph. Never stores credentials."""
import json
import os
import re
from pathlib import Path

import pymupdf
from neo4j import GraphDatabase, READ_ACCESS


def main():
    pdf = pymupdf.open(os.environ["ASSIGNMENT_PDF"])
    page = pdf[13].get_text()
    match = re.search(r"密码[:：]\s*([^\s]+)", page)
    if not match:
        raise RuntimeError("PDF 中找不到数据库密码")
    uri = "bolt://62.234.93.223:30005"
    driver = GraphDatabase.driver(uri, auth=("neo4j", match.group(1)), connection_timeout=8)
    output = {"source": "live Neo4j graph", "uri": uri, "labels": [], "relationships": [], "properties": {}, "samples": {}}
    with driver.session(default_access_mode=READ_ACCESS) as session:
        session.run("RETURN 1 AS ok").single()
        output["labels"] = [r["label"] for r in session.run("CALL db.labels() YIELD label RETURN label ORDER BY label")]
        output["relationships"] = [r["relationshipType"] for r in session.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType ORDER BY relationshipType")]
        for label in output["labels"]:
            query = "MATCH (n:`" + label.replace("`", "``") + "`) RETURN properties(n) AS p LIMIT 3"
            rows = [dict(r["p"]) for r in session.run(query)]
            output["samples"][label] = rows
            output["properties"][label] = [r["key"] for r in session.run(
                "MATCH (n:`" + label.replace("`", "``") + "`) UNWIND keys(n) AS key RETURN DISTINCT key ORDER BY key")]
        output["patterns"] = [dict(r) for r in session.run("MATCH (a)-[r]->(b) RETURN labels(a) AS source, type(r) AS type, labels(b) AS target, count(*) AS count ORDER BY count DESC LIMIT 100")]
        output["relationship_properties"] = [dict(r) for r in session.run("MATCH ()-[r]->() UNWIND keys(r) AS key RETURN type(r) AS type, collect(DISTINCT key) AS properties")]
    driver.close()
    dest = Path(__file__).resolve().parents[1] / "data" / "schema.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"labels": output["labels"], "relationships": output["relationships"], "patterns": output["patterns"][:30], "properties":output["properties"]}, ensure_ascii=True))


if __name__ == "__main__":
    main()
