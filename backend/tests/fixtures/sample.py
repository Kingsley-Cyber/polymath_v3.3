"""Code lane test fixture — small Python module with 1 class + 4 functions."""
import numpy as np
from heapq import heappush, heappop


def normalize(vector):
    """Normalize a vector to unit length."""
    norm = np.linalg.norm(vector)
    if norm == 0:
        return vector
    return vector / norm


def cosine_similarity(a, b):
    """Compute cosine similarity between two vectors."""
    return float(np.dot(normalize(a), normalize(b)))


class VectorStore:
    """In-memory vector store with HNSW-style insert."""

    def __init__(self, dim, ef_construction=200):
        self.dim = dim
        self.ef_construction = ef_construction
        self.vectors = []
        self.queue = []

    def insert(self, vector):
        if vector.shape[0] != self.dim:
            raise ValueError("dim mismatch")
        heappush(self.queue, (id(vector), vector))
        self.vectors.append(vector)

    def search(self, query, k=5):
        scored = [(cosine_similarity(query, v), v) for v in self.vectors]
        scored.sort(key=lambda x: -x[0])
        return scored[:k]


def main():
    store = VectorStore(dim=4)
    store.insert(np.array([1.0, 0.0, 0.0, 0.0]))
    store.insert(np.array([0.0, 1.0, 0.0, 0.0]))
    return store.search(np.array([1.0, 0.0, 0.0, 0.0]))


if __name__ == "__main__":
    main()
