"""Exercise real RPC + MQ subqueries, merge, dedup and topology with a test model."""
import json
from agent.core import Graph, ask, configuration

rpc = "MATCH (m:方法)-[:归属于]->(:类)-[:归属于]->(s:微服务) MATCH p=(m)-[:下游调用]->(:下游API)-[:目标服务]->(t:微服务) WHERE s.服务名称='ts-food-service' RETURN DISTINCT s AS source,t AS target,p AS evidence LIMIT 10"
mq = "MATCH p=(s:微服务)-[:消息依赖]->(t:微服务) WHERE s.服务名称='ts-food-service' RETURN s AS source,t AS target,p AS evidence LIMIT 10"
cfg=configuration()
graph=Graph(cfg)
try:
    record=ask("ts-food-service 的 REST 和 MQ 下游服务分别有哪些？",graph,cfg,
               model=lambda messages,cfg:json.dumps({"queries":[rpc,mq,mq]},ensure_ascii=False),level="复杂")
finally:graph.close()
assert record["error"] is None,record["error"]
counts=[s["count"] for s in record["stages"] if s["stage"]=="执行" and s["passed"]]
assert len(counts)==3 and min(counts)>0,counts
assert len(record["results"])<sum(counts),"没有对重复子查询结果去重"
assert record["topology"]["nodes"] and record["topology"]["edges"]
assert any(s["stage"]=="问题拆分" and s["subquery_count"]==3 for s in record["stages"])
print(json.dumps({"subqueries":len(counts),"raw_rows":sum(counts),"merged_rows":len(record["results"]),
                  "topology_nodes":len(record["topology"]["nodes"]),"topology_edges":len(record["topology"]["edges"])}))
