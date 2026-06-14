import { useState, useEffect, useCallback } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { Plus, Trash2, Save, X, Loader2 } from "lucide-react";
import * as api from "../../lib/api";
import { useSettingsStore } from "../../stores/settingsStore";
import type { Tool, ToolCreate, ToolFormData } from "../../types";

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
      { message: "Parameters must be a valid JSON object" },
    )
    .transform((str) => JSON.parse(str)),
  code: z.string().min(1, "Code cannot be empty"),
  enabled: z.boolean(),
  // Phase 24 — slash command (optional, "" means none). Validated server-side too.
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

export function ToolEditor() {
  const [editingTool, setEditingTool] = useState<Tool | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const { availableTools, loadTools } = useSettingsStore();

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<ToolFormData>({
    resolver: zodResolver(toolSchema),
    defaultValues: {
      name: "",
      description: "",
      parameters: JSON.stringify({ type: "object", properties: {} }, null, 2),
      code: "def execute(**kwargs):\n    # Your code here\n    return",
      enabled: true,
      slash_command: "",
    },
  });

  const fetchTools = useCallback(async () => {
    const tools = await api.listTools();
    loadTools(tools);
  }, [loadTools]);

  useEffect(() => {
    fetchTools();
  }, [fetchTools]);

  const selectTool = useCallback(
    (tool: Tool | null) => {
      setEditingTool(tool);
      if (tool) {
        reset({
          name: tool.name,
          description: tool.description,
          parameters: JSON.stringify(tool.parameters, null, 2),
          code: tool.code,
          enabled: tool.enabled,
          slash_command: tool.slash_command || "",
        });
      } else {
        reset({
          name: "",
          description: "",
          parameters: JSON.stringify(
            { type: "object", properties: {} },
            null,
            2,
          ),
          code: "def execute(**kwargs):\n    # Your code here\n    return",
          enabled: true,
          slash_command: "",
        });
      }
    },
    [reset],
  );

  const handleSave = async (data: ToolFormData) => {
    setIsSaving(true);
    try {
      const slashNorm = (data.slash_command || "").trim().toLowerCase();
      const payload: ToolCreate = {
        ...data,
        slash_command: slashNorm || null,
      };
      if (editingTool) {
        await api.updateTool(editingTool.id, payload);
      } else {
        await api.createTool(payload);
      }
      await fetchTools();
      selectTool(null);
    } catch (error) {
      console.error("Failed to save tool:", error);
    } finally {
      setIsSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    if (
      confirm("Permanently delete this tool? This action cannot be undone.")
    ) {
      await api.deleteTool(id);
      await fetchTools();
      if (editingTool?.id === id) {
        selectTool(null);
      }
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row bg-bg-base text-content-primary font-mono">
      {/* Sidebar List */}
      <div className="w-full md:w-[320px] max-h-48 md:max-h-none border-b md:border-b-0 md:border-r border-border-minimal flex flex-col shrink-0">
        <div className="p-3 border-b border-border-minimal flex-shrink-0">
          <button
            onClick={() => selectTool(null)}
            className="w-full btn-secondary"
          >
            <Plus className="w-3.5 h-3.5 mr-2" />
            New Agent Tool
          </button>
        </div>
        <div className="flex-1 overflow-y-auto custom-scrollbar">
          {availableTools.length > 0 ? (
            availableTools.map((tool) => (
              <div
                key={tool.id}
                onClick={() => selectTool(tool)}
                className={`group flex justify-between items-start p-3 border-b border-border-minimal cursor-pointer hover:bg-bg-surface ${
                  editingTool?.id === tool.id ? "bg-bg-surface" : ""
                }`}
              >
                <div className="flex-1 overflow-hidden">
                  <h3 className="font-bold text-sm truncate text-content-primary">
                    {tool.name}
                  </h3>
                  <p className="text-xs text-content-secondary truncate">
                    {tool.description}
                  </p>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    handleDelete(tool.id);
                  }}
                  className="p-1 text-content-tertiary hover:text-error opacity-0 group-hover:opacity-100"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
            ))
          ) : (
            <div className="p-4 text-center text-xs text-content-tertiary">
              No tools created.
            </div>
          )}
        </div>
      </div>

      {/* Main Editor Form */}
      <div className="flex-1 min-h-0 overflow-y-auto custom-scrollbar p-4 md:p-6 flex flex-col">
        <h2 className="text-lg font-bold uppercase tracking-widest mb-4">
          {editingTool ? "Edit Tool" : "Create New Tool"}
        </h2>
        <form
          onSubmit={handleSubmit(handleSave)}
          className="flex flex-col h-full gap-4"
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="input-label">Tool Name</label>
              <input
                {...register("name")}
                placeholder="get_stock_price"
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
                placeholder="For the LLM to understand what this tool does"
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
                (optional, e.g. /calc — unique across tools + skills)
              </span>
            </label>
            <input
              {...register("slash_command")}
              placeholder="/calc"
              className="input-brutalist"
            />
            {errors.slash_command && (
              <p className="text-xs text-error mt-1">
                {String(errors.slash_command.message)}
              </p>
            )}
          </div>

          <div>
            <label className="input-label">Python Code</label>
            <textarea
              {...register("code")}
              placeholder="def execute(symbol: str): ..."
              className="input-brutalist font-mono flex-1 min-h-[200px]"
              rows={8}
            />
            {errors.code && (
              <p className="text-xs text-error mt-1">{errors.code.message}</p>
            )}
          </div>

          <div>
            <label className="input-label">Parameters (JSON Schema)</label>
            <textarea
              {...register("parameters")}
              className="input-brutalist font-mono"
              rows={6}
            />
            {errors.parameters && (
              <p className="text-xs text-error mt-1">
                {typeof errors.parameters.message === "string"
                  ? errors.parameters.message
                  : "Invalid JSON"}
              </p>
            )}
          </div>

          <div className="flex justify-between items-center mt-auto pt-4 border-t border-border-minimal">
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
                onClick={() => selectTool(null)}
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
                {editingTool ? "Save Changes" : "Create Tool"}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

// Add some brutalist input styles to index.css if they don't exist
// @layer components {
//   .input-label { @apply block text-xs font-bold uppercase tracking-widest text-content-secondary mb-1; }
//   .input-brutalist { @apply w-full bg-bg-surface border border-border-minimal p-2 text-sm text-content-primary placeholder:text-content-tertiary focus:outline-none focus:border-accent-main transition-none rounded-none; }
// }
