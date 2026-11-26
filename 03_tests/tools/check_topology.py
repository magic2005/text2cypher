import json
from agent.core import Graph,configuration,topology
graph=Graph(configuration())
try:
    rows=graph.run("MATCH p=(a:微服务)-[:消息依赖]->(b:微服务) RETURN p LIMIT 3")
finally:graph.close()
topo=topology(rows)
assert topo["nodes"] and topo["edges"],topo
print(json.dumps({"rows":len(rows),"nodes":len(topo["nodes"]),"edges":len(topo["edges"])}))
