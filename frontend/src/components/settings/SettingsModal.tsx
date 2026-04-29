// SettingsModal.tsx — Settings panel with Security tab for credential management
import { useState, useEffect, useCallback, FormEvent } from "react";
import {
  X,
  Shield,
  User,
  Lock,
  Save,
  AlertTriangle,
  CheckCircle,
  ChevronDown,
  LogOut,
  Wrench,
  Brain,
  Server,
  Settings2,
  Layers,
  Sparkles,
  Plug,
} from "lucide-react";
import { useAuthStore } from "../../stores/authStore";
import * as api from "../../lib/api";
import { ToolEditor } from "./ToolEditor";
import { SkillEditor } from "./SkillEditor";
import { McpTab } from "./McpTab";
import { InfrastructureTab } from "./InfrastructureTab";
import { RetrievalSettingsTab } from "./RetrievalSettingsTab";
import { IngestionSettingsTab } from "./IngestionSettingsTab";
import { ModelsTab } from "./ModelsTab";

interface SettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export function SettingsModal({ isOpen, onClose }: SettingsModalProps) {
  const [activeTab, setActiveTab] = useState("Security");

  // Security tab state
  const [currentPassword, setCurrentPassword] = useState("");
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  const { user, setAuth, logout } = useAuthStore();

  // Reset security form state when modal opens
  useEffect(() => {
    if (isOpen) {
      setCurrentPassword("");
      setNewUsername("");
      setNewPassword("");
      setConfirmPassword("");
      setSaveError(null);
      setSaveSuccess(false);
    }
  }, [isOpen]);

  // Handle ESC key to close
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    if (isOpen) {
      window.addEventListener("keydown", handleKeyDown);
    }
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  const handleSecuritySubmit = useCallback(
    async (e: FormEvent) => {
      e.preventDefault();
      setSaveError(null);
      setSaveSuccess(false);

      // Validation
      if (!currentPassword.trim()) {
        setSaveError("Current password is required");
        return;
      }

      if (newPassword && newPassword !== confirmPassword) {
        setSaveError("New password and confirmation do not match");
        return;
      }

      if (!newUsername.trim() && !newPassword) {
        setSaveError("Provide a new username or new password to update");
        return;
      }

      if (newPassword && newPassword.length < 6) {
        setSaveError("New password must be at least 6 characters");
        return;
      }

      setIsSaving(true);

      try {
        const response = await api.updateCredentials({
          current_password: currentPassword,
          new_username: newUsername.trim() || undefined,
          new_password: newPassword || undefined,
        });

        // Update auth store with fresh token and user data
        setAuth(response.access_token, response.user);
        setSaveSuccess(true);

        // Clear form
        setCurrentPassword("");
        setNewUsername("");
        setNewPassword("");
        setConfirmPassword("");

        // Auto-hide success message after 3 seconds
        setTimeout(() => setSaveSuccess(false), 3000);
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Failed to update credentials";
        if (message.includes("401")) {
          setSaveError("Current password is incorrect");
        } else {
          setSaveError(message);
        }
      } finally {
        setIsSaving(false);
      }
    },
    [currentPassword, newUsername, newPassword, confirmPassword, setAuth],
  );

  const tabs = [
    { id: "Security", icon: Shield, label: "Security" },
    { id: "General", icon: User, label: "General" },
    { id: "Models", icon: Brain, label: "Models" },
    { id: "Privacy", icon: Lock, label: "Privacy" },
    { id: "Tools", icon: Wrench, label: "Agent Tools" },
    { id: "Skills", icon: Sparkles, label: "Skills" },
    { id: "MCP", icon: Plug, label: "MCP Server" },
    { id: "Infrastructure", icon: Server, label: "Infrastructure" },
    { id: "Retrieval", icon: Settings2, label: "Retrieval Config" },
    { id: "Ingestion", icon: Layers, label: "Ingestion Config" },
  ];

  // ── Tab Content Renderers ──

  const renderSecurityTab = () => (
    <div className="space-y-8">
      {/* Header */}
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">Security</h2>
        <p className="text-[13px] text-gray-500">
          Manage your authentication credentials.
        </p>
      </div>

      {/* Current User Info */}
      <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 bg-[#444] rounded-lg flex items-center justify-center text-sm font-bold text-white">
            {user?.username?.charAt(0).toUpperCase() || "?"}
          </div>
          <div>
            <p className="text-[14px] text-white font-medium">
              {user?.username || "Unknown"}
            </p>
            <p className="text-[11px] text-gray-500 font-mono">
              ID: {user?.id || "—"} · Created:{" "}
              {user?.created_at
                ? new Date(user.created_at).toLocaleDateString()
                : "—"}
            </p>
          </div>
        </div>
      </div>

      {/* Success Message */}
      {saveSuccess && (
        <div className="flex items-center gap-3 border border-emerald-500/30 bg-emerald-500/5 px-4 py-3 rounded-lg animate-fade-in">
          <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
          <p className="text-[13px] text-emerald-300">
            Credentials updated successfully. New token issued.
          </p>
        </div>
      )}

      {/* Error Message */}
      {saveError && (
        <div className="flex items-start gap-3 border border-red-500/30 bg-red-500/5 px-4 py-3 rounded-lg animate-fade-in">
          <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 shrink-0" />
          <div>
            <p className="text-[11px] font-bold uppercase tracking-widest text-red-400">
              Update Failed
            </p>
            <p className="text-[13px] text-red-300/80 mt-1">{saveError}</p>
          </div>
        </div>
      )}

      {/* Credential Update Form */}
      <form onSubmit={handleSecuritySubmit} className="space-y-5">
        {/* Current Password */}
        <div className="space-y-2">
          <label className="block text-[13px] font-medium text-gray-300">
            <Lock className="w-3.5 h-3.5 inline mr-1.5 text-red-400" />
            Current Password <span className="text-red-400">*</span>
          </label>
          <input
            type="password"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            autoComplete="current-password"
            className="w-full bg-[#2f2f2f] border border-white/10 rounded-lg py-2.5 px-3 text-[14px] text-white focus:outline-none focus:border-white/30 focus:ring-1 focus:ring-white/30 transition-all"
            placeholder="Enter current password"
          />
        </div>

        {/* Divider */}
        <div className="border-t border-white/5" />

        {/* New Username */}
        <div className="space-y-2">
          <label className="block text-[13px] font-medium text-gray-300">
            <User className="w-3.5 h-3.5 inline mr-1.5 text-gray-400" />
            New Username{" "}
            <span className="text-[11px] text-gray-500">(optional)</span>
          </label>
          <input
            type="text"
            value={newUsername}
            onChange={(e) => setNewUsername(e.target.value)}
            autoComplete="off"
            spellCheck={false}
            className="w-full bg-[#2f2f2f] border border-white/10 rounded-lg py-2.5 px-3 text-[14px] text-white focus:outline-none focus:border-white/30 focus:ring-1 focus:ring-white/30 transition-all"
            placeholder={user?.username || "Enter new username"}
          />
        </div>

        {/* New Password */}
        <div className="space-y-2">
          <label className="block text-[13px] font-medium text-gray-300">
            <Lock className="w-3.5 h-3.5 inline mr-1.5 text-gray-400" />
            New Password{" "}
            <span className="text-[11px] text-gray-500">
              (min 6 chars, optional)
            </span>
          </label>
          <input
            type="password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            autoComplete="new-password"
            className="w-full bg-[#2f2f2f] border border-white/10 rounded-lg py-2.5 px-3 text-[14px] text-white focus:outline-none focus:border-white/30 focus:ring-1 focus:ring-white/30 transition-all"
            placeholder="Enter new password"
          />
        </div>

        {/* Confirm New Password */}
        {newPassword && (
          <div className="space-y-2 animate-fade-in">
            <label className="block text-[13px] font-medium text-gray-300">
              <Lock className="w-3.5 h-3.5 inline mr-1.5 text-gray-400" />
              Confirm New Password
            </label>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              autoComplete="new-password"
              className={`w-full bg-[#2f2f2f] border rounded-lg py-2.5 px-3 text-[14px] text-white focus:outline-none focus:ring-1 transition-all ${confirmPassword && confirmPassword !== newPassword
                  ? "border-red-500/50 focus:border-red-500/50 focus:ring-red-500/30"
                  : "border-white/10 focus:border-white/30 focus:ring-white/30"
                }`}
              placeholder="Confirm new password"
            />
            {confirmPassword && confirmPassword !== newPassword && (
              <p className="text-[11px] text-red-400 mt-1">
                Passwords do not match
              </p>
            )}
          </div>
        )}

        {/* Save Button */}
        <div className="pt-2">
          <button
            type="submit"
            disabled={isSaving || (!newUsername.trim() && !newPassword)}
            className={`w-full flex items-center justify-center gap-2 py-2.5 px-4 text-[13px] font-medium rounded-lg transition-all duration-200 ${isSaving
                ? "bg-white/5 text-gray-500 cursor-wait"
                : !newUsername.trim() && !newPassword
                  ? "bg-white/5 text-gray-600 cursor-not-allowed"
                  : "bg-white text-[#242424] hover:bg-gray-200 cursor-pointer"
              }`}
          >
            {isSaving ? (
              <>
                <span className="animate-pulse">Updating...</span>
              </>
            ) : (
              <>
                <Save className="w-4 h-4" />
                <span>Save Changes</span>
              </>
            )}
          </button>
        </div>
      </form>
    </div>
  );

  const renderGeneralTab = () => (
    <div className="space-y-8">
      {/* Header */}
      <h2 className="text-xl font-semibold text-white mb-8">Profile</h2>

      {/* Profile Fields */}
      <div className="space-y-8">
        {/* Name Row */}
        <div className="flex gap-6">
          <div className="flex-1 space-y-2">
            <label className="block text-[13px] font-medium text-gray-300">
              Full name
            </label>
            <div className="relative flex items-center">
              <div className="absolute left-3 w-6 h-6 bg-[#444] rounded-full flex items-center justify-center text-xs font-medium text-white">
                {user?.username?.charAt(0).toUpperCase() || "K"}
              </div>
              <input
                type="text"
                defaultValue={user?.username || "Kingsley"}
                disabled
                className="w-full bg-[#2f2f2f] border border-white/10 rounded-lg py-2.5 pl-12 pr-3 text-[14px] text-white/50 cursor-not-allowed"
              />
            </div>
            <p className="text-[11px] text-gray-600">
              Change username in the Security tab
            </p>
          </div>

          <div className="flex-1 space-y-2">
            <label className="block text-[13px] font-medium text-gray-300">
              Display name
            </label>
            <input
              type="text"
              defaultValue={user?.username || "Kingsley"}
              disabled
              className="w-full bg-[#2f2f2f] border border-white/10 rounded-lg py-2.5 px-3 text-[14px] text-white/50 cursor-not-allowed"
            />
          </div>
        </div>

        {/* Work Description Row */}
        <div className="space-y-2">
          <label className="block text-[13px] font-medium text-gray-300">
            What best describes your work?
          </label>
          <div className="relative">
            <select
              disabled
              className="w-full appearance-none bg-[#2f2f2f] border border-white/10 rounded-lg py-2.5 pl-3 pr-10 text-[14px] text-white/50 cursor-not-allowed"
            >
              <option>Engineering</option>
              <option>Design</option>
              <option>Product Management</option>
              <option>Data Science</option>
              <option>Other</option>
            </select>
            <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-600 pointer-events-none" />
          </div>
        </div>

        {/* Personal Preferences */}
        <div className="space-y-2">
          <label className="block text-[13px] font-medium text-gray-300">
            Personal preferences for responses
          </label>
          <textarea
            disabled
            className="w-full h-[200px] bg-[#2f2f2f] border border-white/10 rounded-xl p-4 text-[13px] leading-relaxed text-white/30 resize-none cursor-not-allowed"
            defaultValue="DETERMINISTIC TECHNICAL REASONING PROTOCOL v2 — Configure via .env and system prompts"
          />
        </div>
      </div>
    </div>
  );

  const renderPrivacyTab = () => (
    <div className="space-y-8">
      <h2 className="text-xl font-semibold text-white mb-2">Privacy</h2>
      <p className="text-[13px] text-gray-500 mb-8">
        Data retention and privacy settings.
      </p>

      <div className="space-y-6">
        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-4">
          <h3 className="text-[14px] font-medium text-white mb-1">
            Conversation Data
          </h3>
          <p className="text-[12px] text-gray-500">
            All conversations are stored locally in MongoDB. No data is sent to
            third parties beyond your configured LLM provider.
          </p>
        </div>

        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-4">
          <h3 className="text-[14px] font-medium text-white mb-1">
            Authentication Tokens
          </h3>
          <p className="text-[12px] text-gray-500">
            JWT tokens are stored in browser localStorage. Tokens expire after 7
            days. Passwords are hashed with bcrypt and never stored in plain
            text.
          </p>
        </div>

        <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-4">
          <h3 className="text-[14px] font-medium text-white mb-1">
            Vector Embeddings
          </h3>
          <p className="text-[12px] text-gray-500">
            Document embeddings are stored in Qdrant. Graph relationships are in
            Neo4j. All services run within your Docker network.
          </p>
        </div>
      </div>
    </div>
  );

  const renderToolTab = () => <ToolEditor />;

  const renderSkillsTab = () => <SkillEditor />;

  const renderMcpTab = () => <McpTab />;

  const renderInfrastructureTab = () => <InfrastructureTab />;

  const renderRetrievalTab = () => <RetrievalSettingsTab />;

  const renderIngestionTab = () => <IngestionSettingsTab />;

  const renderModelsTab = () => <ModelsTab />;

  const renderTabContent = () => {
    switch (activeTab) {
      case "Security":
        return renderSecurityTab();
      case "Tools":
        return renderToolTab();
      case "Skills":
        return renderSkillsTab();
      case "MCP":
        return renderMcpTab();
      case "General":
        return renderGeneralTab();
      case "Privacy":
        return renderPrivacyTab();
      case "Models":
        return renderModelsTab();
      case "Infrastructure":
        return renderInfrastructureTab();
      case "Retrieval":
        return renderRetrievalTab();
      case "Ingestion":
        return renderIngestionTab();
      default:
        return renderGeneralTab();
    }
  };

  // Closed → render nothing. Previous version kept the full-viewport wrapper
  // mounted with opacity-0; the backdrop's onClick was eating clicks meant for
  // the sidebar's Settings button (which dispatches `open-settings` to set
  // isOpen=true) and instantly closing the modal again.
  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-bg-base animate-overlay-in opacity-100"
        onClick={onClose}
      />

      {/* Modal Container */}
      <div
        className={`relative w-full max-w-[1200px] h-[85vh] max-h-[800px] min-h-[500px] bg-[#242424] rounded-2xl shadow-2xl flex overflow-hidden border border-white/5 ${isOpen ? "animate-modal-in" : "opacity-0 scale-95"
          }`}
        style={{ fontFamily: "Inter, -apple-system, sans-serif" }}
      >
        {/* Sidebar Navigation */}
        <div className="w-[260px] bg-[#242424] border-r border-white/5 flex flex-col py-6 overflow-y-auto custom-scrollbar shrink-0">
          {/* Section: Account */}
          <div className="mb-2 px-6 text-[12px] font-semibold text-gray-500 uppercase tracking-wider">
            Account
          </div>
          <div className="flex flex-col space-y-0.5 px-3 mb-6">
            {tabs.slice(0, 2).map((tab) => (
              <button
                key={tab.id}
                onClick={() => {
                  setActiveTab(tab.id);
                  setSaveError(null);
                  setSaveSuccess(false);
                }}
                className={`text-left px-3 py-2.5 rounded-lg text-[13px] transition-colors flex items-center gap-2 ${activeTab === tab.id
                    ? "bg-[#333333] text-white font-medium"
                    : "text-gray-400 hover:bg-[#2f2f2f] hover:text-gray-200"
                  }`}
              >
                <tab.icon className="w-4.5 h-4.5" />
                {tab.label}
                {tab.id === "Security" && (
                  <span className="ml-auto text-[11px] px-2 py-0.5 bg-accent-main/20 text-accent-main rounded uppercase tracking-wider font-bold">
                    Auth
                  </span>
                )}
              </button>
            ))}
          </div>

          {/* Section: System */}
          <div className="mb-2 px-6 text-[11px] font-semibold text-gray-500 uppercase tracking-wider">
            System
          </div>
          <div className="flex flex-col space-y-0.5 px-3">
            {tabs.slice(2, 7).map((tab) => (
              <button
                key={tab.id}
                onClick={() => {
                  setActiveTab(tab.id);
                  setSaveError(null);
                  setSaveSuccess(false);
                }}
                className={`text-left px-3 py-2.5 rounded-lg text-[13px] transition-colors flex items-center gap-2 ${activeTab === tab.id
                    ? "bg-[#333333] text-white font-medium"
                    : "text-gray-400 hover:bg-[#2f2f2f] hover:text-gray-200"
                  }`}
              >
                <tab.icon className="w-4.5 h-4.5" />
                {tab.label}
              </button>
            ))}
          </div>

          {/* Section: Config */}
          <div className="mb-2 px-6 text-[12px] font-semibold text-gray-500 uppercase tracking-wider mt-4">
            Config
          </div>
          <div className="flex flex-col space-y-0.5 px-3">
            {tabs.slice(7).map((tab) => (
              <button
                key={tab.id}
                onClick={() => {
                  setActiveTab(tab.id);
                  setSaveError(null);
                  setSaveSuccess(false);
                }}
                className={`text-left px-3 py-2.5 rounded-lg text-[13px] transition-colors flex items-center gap-2 ${activeTab === tab.id
                    ? "bg-[#333333] text-white font-medium"
                    : "text-gray-400 hover:bg-[#2f2f2f] hover:text-gray-200"
                  }`}
              >
                <tab.icon className="w-4.5 h-4.5" />
                {tab.label}
              </button>
            ))}
          </div>

          {/* User info + logout at bottom */}
          <div className="mt-auto px-3 pt-6 border-t border-white/5 mx-3">
            <div className="flex items-center gap-2 px-3 py-2">
              <div className="w-7 h-7 bg-[#444] rounded-lg flex items-center justify-center text-[11px] font-bold text-white">
                {user?.username?.charAt(0).toUpperCase() || "?"}
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-[12px] text-white truncate">
                  {user?.username || "Unknown"}
                </p>
                <p className="text-[10px] text-gray-500 font-mono">
                  Authenticated
                </p>
              </div>
              <button
                onClick={logout}
                className="p-1.5 text-gray-500 hover:text-red-400 hover:bg-red-500/10 rounded-lg transition-colors"
                title="Log out"
              >
                <LogOut className="w-4 h-4" />
              </button>
            </div>
          </div>
        </div>

        {/* Main Content Area */}
        <div className="flex-1 bg-[#242424] flex flex-col overflow-hidden relative">
          {/* Close Button */}
          <button
            onClick={onClose}
            className="absolute top-6 right-6 p-2 text-gray-400 hover:text-white hover:bg-white/10 rounded-lg transition-colors z-10"
          >
            <X className="w-5 h-5" />
          </button>

          {activeTab === "Tools" ? (
            <div className="flex-1 flex flex-col min-h-0 bg-bg-base">
              {renderTabContent()}
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto custom-scrollbar p-10">
              <div className="max-w-[680px]">{renderTabContent()}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
