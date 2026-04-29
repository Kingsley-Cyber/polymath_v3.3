// Phase 24 — types for the Skills feature. Mirrors types/tools.ts shape.

import { z } from "zod";

export const skillSchema = z.object({
  name: z.string().min(2, "Name must be at least 2 characters"),
  description: z.string().min(10, "Description is too short"),
  instructions: z.string().min(1, "Instructions cannot be empty"),
  enabled: z.boolean(),
  slash_command: z
    .string()
    .refine(
      (v) => {
        const norm = (v || "").trim().toLowerCase();
        return norm === "" || /^\/[a-z0-9_-]{1,31}$/.test(norm);
      },
      { message: "Slash command must look like /lowercase-letters-digits-only (2-32 chars total)" },
    ),
});

export type SkillFormData = z.infer<typeof skillSchema>;

export interface SkillBase {
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
  slash_command?: string | null;
}

export interface Skill extends SkillBase {
  id: string;
}

export type SkillCreate = SkillBase;

export type SkillUpdate = Partial<SkillBase>;
