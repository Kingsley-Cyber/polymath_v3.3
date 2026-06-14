// Phase 24 — SkillEditor. Mirrors ToolEditor shape; swaps Python code for
// markdown instructions. Skills inject as <skills_active> context, not as
// callable functions.
import { useState, useEffect, useCallback } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { Plus, Trash2, Save, X, Loader2 } from "lucide-react";
import * as api from "../../lib/api";
import { useSettingsStore } from "../../stores/settingsStore";
import { skillSchema } from "../../types/skills";
import type { Skill, SkillCreate, SkillFormData } from "../../types";

export function SkillEditor() {
  const [editingSkill, setEditingSkill] = useState<Skill | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const { availableSkills, loadSkills } = useSettingsStore();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<SkillFormData>({
    resolver: zodResolver(skillSchema),
    defaultValues: {
      name: "",
      description: "",
      instructions: "",
      enabled: true,
      slash_command: "",
    },
  });

  const fetchSkills = useCallback(async () => {
    const skills = await api.listSkills();
    loadSkills(skills);
  }, [loadSkills]);

  useEffect(() => {
    fetchSkills();
  }, [fetchSkills]);

  const selectSkill = useCallback(
    (skill: Skill | null) => {
      setSaveError(null);
      setEditingSkill(skill);
      if (skill) {
        reset({
          name: skill.name,
          description: skill.description,
          instructions: skill.instructions,
          enabled: skill.enabled,
          slash_command: skill.slash_command || "",
        });
      } else {
        reset({
          name: "",
          description: "",
          instructions: "",
          enabled: true,
          slash_command: "",
        });
      }
    },
    [reset],
  );

  const handleSave = async (data: SkillFormData) => {
    setIsSaving(true);
    setSaveError(null);
    try {
      const slashNorm = (data.slash_command || "").trim().toLowerCase();
      const payload: SkillCreate = {
        ...data,
        slash_command: slashNorm || null,
      };
      if (editingSkill) {
        await api.updateSkill(editingSkill.id, payload);
      } else {
        await api.createSkill(payload);
      }
      await fetchSkills();
      selectSkill(null);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Failed to save skill");
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (
      confirm("Permanently delete this skill? This action cannot be undone.")
    ) {
      await api.deleteSkill(id);
      await fetchSkills();
      if (editingSkill?.id === id) {
        selectSkill(null);
      }
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row bg-bg-base text-content-primary font-mono">
      {/* Sidebar List */}
      <div className="w-full md:w-[320px] max-h-48 md:max-h-none border-b md:border-b-0 md:border-r border-border-minimal flex flex-col shrink-0">
        <div className="p-3 border-b border-border-minimal flex-shrink-0">
          <button
            onClick={() => selectSkill(null)}
            className="w-full btn-secondary"
          >
            <Plus className="w-3.5 h-3.5 mr-2" />
            New Skill
          </button>
        </div>
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {availableSkills.length > 0 ? (
            availableSkills.map((skill) => (
              <div
                key={skill.id}
                onClick={() => selectSkill(skill)}
                className={`group flex justify-between items-start p-3 border-b border-border-minimal cursor-pointer hover:bg-bg-surface ${
                  editingSkill?.id === skill.id ? "bg-bg-surface" : ""
                }`}
              >
                <div className="flex-1 overflow-hidden">
                  <h3 className="font-bold text-sm truncate text-content-primary">
                    {skill.name}
                  </h3>
                  {skill.slash_command && (
                    <p className="text-[10px] text-accent-main font-mono">
                      {skill.slash_command}
                    </p>
                  )}
                  <p className="text-xs text-content-secondary truncate">
                    {skill.description}
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(skill.id);
                  }}
                  className="p-1 text-content-tertiary hover:text-error opacity-0 group-hover:opacity-100"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            ))
          ) : (
            <div className="p-4 text-center text-xs text-content-tertiary">
              No skills created.
            </div>
          )}
        </div>
      </div>

      {/* Main Editor Form */}
      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-4 md:p-6 flex flex-col">
        <h2 className="text-lg font-bold uppercase tracking-widest mb-4">
          {editingSkill ? "Edit Skill" : "Create New Skill"}
        </h2>
        <form
          onSubmit={handleSubmit(handleSave)}
          className="flex flex-col h-full gap-4"
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="input-label">Skill Name</label>
              <input
                {...register("name")}
                placeholder="App Design"
                className="input-brutalist"
              />
              {errors.name && (
                <p className="text-xs text-error mt-1">{errors.name.message}</p>
              )}
            </div>
            <div>
              <label className="input-label">Description</label>
              <input
                {...register("description")}
                placeholder="UI/UX design — generates HTML/CSS from a PRD"
                className="input-brutalist"
              />
              {errors.description && (
                <p className="text-xs text-error mt-1">
                  {errors.description.message}
                </p>
              )}
            </div>
          </div>

          <div>
            <label className="input-label">
              Slash Command{" "}
              <span className="text-content-tertiary normal-case font-normal">
                (optional, e.g. /design — unique across tools + skills)
              </span>
            </label>
            <input
              {...register("slash_command")}
              placeholder="/design"
              className="input-brutalist"
            />
            {errors.slash_command && (
              <p className="text-xs text-error mt-1">
                {String(errors.slash_command.message)}
              </p>
            )}
          </div>

          <div className="flex-1 flex flex-col">
            <label className="input-label">
              Instructions (markdown){" "}
              <span className="text-content-tertiary normal-case font-normal">
                — appended to the user-turn context as a &lt;skill&gt; block
              </span>
            </label>
            <textarea
              {...register("instructions")}
              placeholder={`You are a senior UI/UX designer. When the user asks about interfaces:
- Start with the user's goal, not the visual.
- Propose 2-3 distinct directions, not one "best" answer.
- Always mention spacing, hierarchy, and contrast.
- Output HTML + inline CSS when generating designs.`}
              className="input-brutalist font-mono flex-1 min-h-[260px]"
              rows={14}
            />
            {errors.instructions && (
              <p className="text-xs text-error mt-1">
                {errors.instructions.message}
              </p>
            )}
          </div>

          {saveError && (
            <p className="text-xs text-error">{saveError}</p>
          )}

          <div className="flex flex-col sm:flex-row sm:justify-between sm:items-center gap-3 mt-auto pt-4 border-t border-border-minimal">
            <label className="flex items-center gap-2 text-sm cursor-pointer">
              <input
                type="checkbox"
                {...register("enabled")}
                className="h-4 w-4 bg-bg-surface border-border-minimal"
              />
              Enabled
            </label>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => selectSkill(null)}
                className="btn-secondary"
              >
                <X className="w-3.5 h-3.5 mr-2" /> Cancel
              </button>
              <button
                type="submit"
                disabled={isSaving || !isDirty}
                className="btn-primary"
              >
                {isSaving ? (
                  <Loader2 className="w-3.5 h-3.5 mr-2 animate-spin" />
                ) : (
                  <Save className="w-3.5 h-3.5 mr-2" />
                )}
                {editingSkill ? "Save Changes" : "Create Skill"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}
