// useSettings.ts - Hook wrapper around settings store
import { useSettingsStore } from "../stores/settingsStore";

export function useSettings() {
  const store = useSettingsStore();

  return {
    // Model settings
    selectedModel: store.selectedModel,
    temperature: store.temperature,
    topP: store.topP,
    maxTokens: store.maxTokens,

    // RAG settings
    retrievalK: store.retrievalK,
    hydeEnabled: store.hydeEnabled,
    rerankingEnabled: store.rerankingEnabled,
    reasoningMode: store.reasoningMode,
    selectedCollectionIds: store.selectedCollectionIds,

    // UI settings
    theme: store.theme,
    fontSize: store.fontSize,
    reducedMotion: store.reducedMotion,
    sidebarOpen: store.sidebarOpen,

    // Actions
    updateSettings: store.updateSettings,
    setSelectedModel: store.setSelectedModel,
    toggleHyDE: store.toggleHyDE,
    toggleReranking: store.toggleReranking,
    toggleCollection: store.toggleCollection,
    selectAllCollections: store.selectAllCollections,
    clearCollections: store.clearCollections,
    setTheme: store.setTheme,
    toggleSidebar: store.toggleSidebar,
    resetToDefaults: store.resetToDefaults,
  };
}
