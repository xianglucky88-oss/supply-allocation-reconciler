"""Min-cost max-flow used by the hierarchical solver.

Each resolution layer is solved as a transportation network:

    source -> demand nodes -> eligible token nodes -> sink

Arc costs encode (priority, plant preference, availability time, substitute
rank, token id) so the minimum-cost flow fills the most important orders from
the best sources first, while optimising globally instead of greedily.

Reverse arcs carry negative costs, so shortest paths use SPFA (Bellman-Ford
with a queue); graphs here are tiny (orders x batches) and deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple


@dataclass
class _Edge:
    to: int
    cap: int
    cost: int
    rev: int                 # index of reverse edge in adjacency[to]
    flow: int = 0


@dataclass
class MinCostFlow:
    n: int
    adj: List[List[_Edge]] = field(default_factory=list)
    relaxations: int = 0
    augmentations: int = 0

    def __post_init__(self) -> None:
        if not self.adj:
            self.adj = [[] for _ in range(self.n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int) -> None:
        forward = _Edge(to=v, cap=cap, cost=cost, rev=len(self.adj[v]))
        backward = _Edge(to=u, cap=0, cost=-cost, rev=len(self.adj[u]))
        self.adj[u].append(forward)
        self.adj[v].append(backward)

    def flow(self, source: int, sink: int, limit: int) -> Tuple[int, int]:
        """Push up to ``limit`` units of min-cost flow.

        Returns (units_pushed, total_cost).  SPFA ties are broken by node
        id so repeated runs select identical augmenting paths.
        """
        inf = 10 ** 30
        pushed = 0
        total_cost = 0
        while pushed < limit:
            dist = [inf] * self.n
            in_queue = [False] * self.n
            prev_node = [-1] * self.n
            prev_edge = [-1] * self.n
            dist[source] = 0
            queue = [source]
            in_queue[source] = True
            head = 0
            while head < len(queue):
                u = queue[head]
                head += 1
                in_queue[u] = False
                for ei, edge in enumerate(self.adj[u]):
                    if edge.cap <= 0:
                        continue
                    nd = dist[u] + edge.cost
                    self.relaxations += 1
                    if nd < dist[edge.to] or (
                        nd == dist[edge.to]
                        and prev_node[edge.to] == -1
                        and u < edge.to  # deterministic tie break
                    ):
                        dist[edge.to] = nd
                        prev_node[edge.to] = u
                        prev_edge[edge.to] = ei
                        if not in_queue[edge.to]:
                            queue.append(edge.to)
                            in_queue[edge.to] = True
            if dist[sink] == inf:
                break
            add = limit - pushed
            node = sink
            while node != source:
                edge = self.adj[prev_node[node]][prev_edge[node]]
                add = min(add, edge.cap)
                node = prev_node[node]
            node = sink
            while node != source:
                pu = prev_node[node]
                edge = self.adj[pu][prev_edge[node]]
                edge.cap -= add
                edge.flow += add
                self.adj[node][edge.rev].cap += add
                self.adj[node][edge.rev].flow -= add
                node = pu
            pushed += add
            total_cost += add * dist[sink]
            self.augmentations += 1
        return pushed, total_cost

    def edge_flows(self, u: int) -> List[Tuple[int, int]]:
        """Return (target node, net flow) for every saturated arc out of u."""
        return [
            (edge.to, edge.flow)
            for edge in self.adj[u]
            if edge.flow > 0
        ]
