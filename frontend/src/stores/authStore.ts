// Auth Store - Zustand state management for authentication
import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { UserPublic } from "../types";

interface AuthState {
  // State
  token: string | null;
  user: UserPublic | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;

  // Actions
  setAuth: (token: string, user: UserPublic) => void;
  clearAuth: () => void;
  setLoading: (loading: boolean) => void;
  setError: (error: string | null) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set) => ({
      // Initial state
      token: null,
      user: null,
      isAuthenticated: false,
      isLoading: false,
      error: null,

      // Set authentication data after successful login or token refresh
      setAuth: (token, user) =>
        set({
          token,
          user,
          isAuthenticated: true,
          isLoading: false,
          error: null,
        }),

      // Clear all auth state
      clearAuth: () =>
        set({
          token: null,
          user: null,
          isAuthenticated: false,
          isLoading: false,
          error: null,
        }),

      // Loading state for async operations
      setLoading: (isLoading) => set({ isLoading }),

      // Error state
      setError: (error) => set({ error, isLoading: false }),

      // Full logout — clears persisted state
      logout: () =>
        set({
          token: null,
          user: null,
          isAuthenticated: false,
          isLoading: false,
          error: null,
        }),
    }),
    {
      name: "polymath-auth",
      // Only persist token and user — derived state recomputes on hydration
      partialize: (state) => ({
        token: state.token,
        user: state.user,
      }),
      // Re-derive isAuthenticated after rehydration
      merge: (persistedState, currentState) => {
        const ps = persistedState as Partial<AuthState>;
        const hasToken = ps?.token != null && ps.token !== "";
        const hasUser = ps?.user != null;
        return {
          ...currentState,
          ...ps,
          // Re-derive: only authenticated if we have BOTH token and user
          isAuthenticated: hasToken && hasUser,
          isLoading: false,
          error: null,
        };
      },
    },
  ),
);
