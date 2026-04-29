// useCollections.ts - Hook for fetching and managing collections
import { useState, useEffect, useCallback } from "react";
import { getCollections } from "../lib/api";
import type { Collection } from "../types";

export function useCollections() {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchCollections = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await getCollections();
      setCollections(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load collections");
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchCollections();
  }, [fetchCollections]);

  return {
    collections,
    isLoading,
    error,
    refetch: fetchCollections,
  };
}
