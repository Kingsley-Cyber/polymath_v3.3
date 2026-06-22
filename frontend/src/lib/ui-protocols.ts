import type { Theme } from "../types";

export const UI_PROTOCOLS: {
  id: Theme;
  label: string;
  accent: "main" | "secondary";
}[] = [
  { id: "ayu-mirage", label: "Obsidian Graph", accent: "main" },
  { id: "gruvbox", label: "Polymath Onto.", accent: "secondary" },
  { id: "serendipity", label: "Deterministic", accent: "main" },
  { id: "nord", label: "Arctic Ice", accent: "main" },
  { id: "dracula", label: "Neon Spectral", accent: "main" },
  { id: "solar", label: "Solar Focus", accent: "secondary" },
  { id: "claude", label: "???", accent: "main" },
];
