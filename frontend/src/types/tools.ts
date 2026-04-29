// types/tools.ts - All type definitions for the Custom Tools feature

import { z } from "zod";

// Zod schema for form validation, exported for use in other files.
export const toolSchema = z.object({
  name: z.string().min(2, "Name must be at least 2 characters"),
  description: z.string().min(10, "Description is too short"),
  parameters: z
    .string()
    .refine(
      (val) => {
        try {
          JSON.parse(val);
          return true;
        } catch {
          return false;
        }
      },
      { message: "Parameters must be a valid JSON object" }
    )
    .transform((str) => JSON.parse(str)),
  code: z.string().min(1, "Code cannot be empty"),
  enabled: z.boolean(),
  // Phase 24 — slash command (optional, "" means none).
  slash_command: z
    .string()
    .refine(
      (v) => {
        const norm = (v || "").trim().toLowerCase();
        return norm === "" || /^\/[a-z0-9_-]{1,31}$/.test(norm);
      },
      { message: "Slash must be /lowercase-letters-digits-only (2-32 chars)" },
    ),
});

// TypeScript type inferred from the Zod schema for use in components.
export type ToolFormData = z.infer<typeof toolSchema>;

// Base interface for tool data structure, used for creation.
export interface ToolBase {
  name: string;
  description: string;
  parameters: Record<string, any>; // JSON Schema
  code: string;
  enabled: boolean;
  // Phase 24 — slash command for in-chat invocation. Optional. Unique across
  // tools + skills (validated server-side).
  slash_command?: string | null;
}

// Interface for a tool object as returned from the API, including the ID.
export interface Tool extends ToolBase {
  id: string;
}

// Type for creating a new tool.
export type ToolCreate = ToolBase;

// Type for partially updating an existing tool.
export type ToolUpdate = Partial<ToolBase>;
