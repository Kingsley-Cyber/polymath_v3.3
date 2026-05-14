# Vector Search Basics

Retrieval-augmented generation systems rely on vector similarity search at
their core. The standard primitive is the inner-product or cosine score
between a query embedding and a corpus of pre-computed document embeddings.

## A naive Python implementation

The simplest baseline iterates over every vector and returns the top-K.

```python
import numpy as np

def cosine_search(query, vectors, k=5):
    scores = [float(np.dot(query, v) / (np.linalg.norm(query) * np.linalg.norm(v))) for v in vectors]
    ranked = sorted(enumerate(scores), key=lambda x: -x[1])
    return [(i, s) for i, s in ranked[:k]]
```

This is O(N * D) per query and scales poorly past a few hundred thousand
vectors. Production systems reach for approximate indexes instead.

## HNSW indexes

HNSW (Hierarchical Navigable Small World) builds a multi-layer graph where
the upper layers act as long-range navigation aids and the lower layers
refine local neighborhoods.

```python
import numpy as np
from heapq import heappush, heappop

class HNSWIndex:
    """Toy HNSW with a single layer. Production: use hnswlib."""

    def __init__(self, dim, ef_construction=200, M=16):
        self.dim = dim
        self.ef_construction = ef_construction
        self.M = M
        self.nodes = []
        self.graph = {}

    def insert(self, vector):
        if vector.shape[0] != self.dim:
            raise ValueError("dim mismatch")
        idx = len(self.nodes)
        self.nodes.append(vector)
        self.graph[idx] = []
        for j in range(max(0, idx - self.M), idx):
            self.graph[idx].append(j)
            self.graph[j].append(idx)
        return idx

    def search(self, query, k=5, ef_search=50):
        if not self.nodes:
            return []
        visited = set()
        candidates = []
        for start in [0]:
            heappush(candidates, (-_score(query, self.nodes[start]), start))
        results = []
        while candidates and len(results) < ef_search:
            neg_score, node = heappop(candidates)
            if node in visited:
                continue
            visited.add(node)
            results.append((node, -neg_score))
            for neighbor in self.graph.get(node, []):
                if neighbor not in visited:
                    heappush(candidates, (-_score(query, self.nodes[neighbor]), neighbor))
        results.sort(key=lambda x: -x[1])
        return results[:k]


def _score(a, b):
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
```

This implementation is intentionally simple — real systems handle
multi-layer graphs, dynamic insertion at runtime, and disk-backed indexes.

## Trade-offs

HNSW shines on recall-vs-latency curves but uses more memory than flat
indexes. IVF (Inverted File) trades some recall for tight memory use by
quantizing residual vectors against a coarse codebook.

```typescript
// TypeScript reference for what an IVF query interface might look like
interface IVFQuery {
    centroids: number[][];
    probes: number;
    topK: number;
}

class IVFIndex {
    constructor(public readonly dim: number, public readonly nlist: number) {}

    train(vectors: number[][]): void {
        // kmeans over `vectors` to build `nlist` centroids
    }

    add(vectors: number[][]): void {
        // assign each vector to its nearest centroid; store residual
    }

    search(query: number[], opts: IVFQuery): Array<[number, number]> {
        // probe the top `nprobe` centroids, score within each cell
        return [];
    }
}
```

The choice between HNSW and IVF (or hybrids like HNSW-PQ) comes down to
the corpus size, query latency budget, and memory ceiling.
