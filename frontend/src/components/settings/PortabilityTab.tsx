import { useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  AlertTriangle,
  Archive,
  CheckCircle,
  Download,
  HardDrive,
  Loader2,
  Upload,
} from "lucide-react";
import * as api from "../../lib/api";

export function PortabilityTab() {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [isExporting, setIsExporting] = useState(false);
  const [isImporting, setIsImporting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const handleDownload = async () => {
    setError(null);
    setMessage(null);
    setIsExporting(true);
    try {
      const blob = await api.downloadPortabilityArchive();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      const stamp = new Date().toISOString().replace(/[:.]/g, "-");
      a.href = url;
      a.download = `polymath-runtime-export-${stamp}.zip`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setMessage("Export archive downloaded.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setIsExporting(false);
    }
  };

  const handleUploadClick = () => {
    fileInputRef.current?.click();
  };

  const handleFileSelected = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    setMessage(null);
    setIsImporting(true);
    try {
      const result = await api.uploadPortabilityArchive(file);
      const mongoCount = Object.values(result.stats.mongo_documents || {}).reduce(
        (sum, n) => sum + n,
        0,
      );
      const qdrantCount = Object.values(result.stats.qdrant_points || {}).reduce(
        (sum, n) => sum + n,
        0,
      );
      setMessage(
        `Import complete: ${mongoCount} Mongo records, ${qdrantCount} vector points, ${result.stats.neo4j_nodes || 0} graph nodes.`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Import failed");
    } finally {
      setIsImporting(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-xl font-semibold text-white mb-2">
          Runtime Portability
        </h2>
        <p className="text-[13px] text-gray-500 leading-relaxed">
          Download or restore a Polymath archive from this browser.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <StatusTile
          icon={<HardDrive className="w-4 h-4 text-cyan-300" />}
          label="Stores"
          value="Mongo · Qdrant · Neo4j"
        />
        <StatusTile
          icon={<Archive className="w-4 h-4 text-emerald-300" />}
          label="Archive"
          value="ZIP"
        />
        <StatusTile
          icon={<CheckCircle className="w-4 h-4 text-purple-300" />}
          label="Requirement"
          value="Same embedding dim"
        />
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <button
          type="button"
          onClick={handleDownload}
          disabled={isExporting || isImporting}
          className="min-h-[132px] bg-[#2a2a2a] border border-white/5 rounded-lg p-5 text-left hover:border-emerald-400/40 hover:bg-emerald-400/5 transition-colors disabled:opacity-60 disabled:cursor-wait"
        >
          <div className="flex items-center gap-3 mb-4">
            {isExporting ? (
              <Loader2 className="w-5 h-5 text-emerald-300 animate-spin" />
            ) : (
              <Download className="w-5 h-5 text-emerald-300" />
            )}
            <span className="text-[15px] font-semibold text-white">
              Download Export
            </span>
          </div>
          <p className="text-[12px] text-gray-500 leading-relaxed">
            Saves a portable ZIP containing your corpora, chunks, vectors, graph,
            settings, tools, and skills.
          </p>
        </button>

        <button
          type="button"
          onClick={handleUploadClick}
          disabled={isExporting || isImporting}
          className="min-h-[132px] bg-[#2a2a2a] border border-white/5 rounded-lg p-5 text-left hover:border-cyan-400/40 hover:bg-cyan-400/5 transition-colors disabled:opacity-60 disabled:cursor-wait"
        >
          <div className="flex items-center gap-3 mb-4">
            {isImporting ? (
              <Loader2 className="w-5 h-5 text-cyan-300 animate-spin" />
            ) : (
              <Upload className="w-5 h-5 text-cyan-300" />
            )}
            <span className="text-[15px] font-semibold text-white">
              Upload Import
            </span>
          </div>
          <p className="text-[12px] text-gray-500 leading-relaxed">
            Opens your system file picker and restores a Polymath ZIP into this
            account.
          </p>
        </button>
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept=".zip,application/zip"
        className="hidden"
        onChange={(event) => handleFileSelected(event.target.files?.[0])}
      />

      {message && (
        <div className="flex items-start gap-3 border border-emerald-400/30 bg-emerald-400/5 px-4 py-3 rounded-lg">
          <CheckCircle className="w-4 h-4 text-emerald-300 mt-0.5 shrink-0" />
          <div className="text-[12px] text-emerald-100/90 leading-relaxed">
            {message}
          </div>
        </div>
      )}

      {error && (
        <div className="flex items-start gap-3 border border-red-400/30 bg-red-400/5 px-4 py-3 rounded-lg">
          <AlertTriangle className="w-4 h-4 text-red-300 mt-0.5 shrink-0" />
          <div className="text-[12px] text-red-100/90 leading-relaxed">
            {error}
          </div>
        </div>
      )}

      <div className="flex items-start gap-3 border border-amber-400/30 bg-amber-400/5 px-4 py-3 rounded-lg">
        <AlertTriangle className="w-4 h-4 text-amber-300 mt-0.5 shrink-0" />
        <div className="text-[12px] text-amber-100/90 leading-relaxed">
          Imports merge records into the current account. Keep the same
          embedding model and dimension on the destination device.
        </div>
      </div>
    </div>
  );
}

function StatusTile({
  icon,
  label,
  value,
}: {
  icon: ReactNode;
  label: string;
  value: string;
}) {
  return (
    <div className="bg-[#2a2a2a] border border-white/5 rounded-lg p-3 min-w-0">
      <div className="flex items-center gap-2 text-[10px] text-gray-500 uppercase tracking-widest">
        {icon}
        <span className="truncate">{label}</span>
      </div>
      <div className="text-[12px] text-white font-mono mt-2 truncate">
        {value}
      </div>
    </div>
  );
}
