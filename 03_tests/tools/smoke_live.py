"""Checkpoint B: graph execution with an injected deterministic test model."""
import json
from agent.core import Graph, ask, configuration

cfg=configuration()
graph=Graph(cfg)
try:
    response=ask("列出十个微服务",graph,cfg,
                 model=lambda messages,cfg: json.dumps({"queries":["MATCH (s:微服务) RETURN s.服务名称 AS 服务 LIMIT 10"]},ensure_ascii=False))
finally:graph.close()
assert response["error"] is None,response["error"]
assert len(response["results"])==10
print(json.dumps({"pipeline":"passed","result_count":len(response["results"]),
                  "model":"injected test stub; no LLM credential"},ensure_ascii=True))
