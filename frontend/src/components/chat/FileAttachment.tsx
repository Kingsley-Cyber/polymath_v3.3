// FileAttachment.tsx - File attachment display and upload component
import { useEffect, useState } from "react";
import { ImageOff, X, Upload } from "lucide-react";

interface FileAttachmentProps {
  file: File;
  onRemove?: () => void;
  uploadProgress?: number;
  isUploading?: boolean;
}

export function FileAttachment({
  file,
  onRemove,
  uploadProgress,
  isUploading = false,
}: FileAttachmentProps) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewFailed, setPreviewFailed] = useState(false);

  useEffect(() => {
    setPreviewFailed(false);
    if (!(file.type || "").toLowerCase().startsWith("image/")) {
      setPreviewUrl(null);
      return;
    }
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  const getFileTag = () => {
    const type = file.type;
    if (type.startsWith("image/")) return "IMG";
    if (type.includes("pdf")) return "PDF";
    if (type.includes("text")) return "TXT";
    if (type.includes("json")) return "JSN";
    return "FIL";
  };

  return (
    <div
      className="relative flex min-w-0 items-center gap-2 border border-border-minimal bg-bg-surface px-2 py-1.5 group"
      data-testid="attachment-preview"
    >
      {previewUrl && !previewFailed ? (
        <img
          src={previewUrl}
          alt={`Preview of ${file.name}`}
          className="h-10 w-10 shrink-0 object-cover"
          width={40}
          height={40}
          onError={() => setPreviewFailed(true)}
          data-testid="attachment-preview-image"
        />
      ) : previewFailed ? (
        <span
          className="flex h-10 w-10 shrink-0 items-center justify-center border border-error/30 text-error"
          title="Image preview failed"
        >
          <ImageOff className="h-4 w-4" />
        </span>
      ) : null}
      {/* File type tag */}
      <span className="status-badge status-badge-inf">{`<${getFileTag()}>`}</span>

      {/* File Info */}
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-text-primary truncate">
          {file.name}
        </div>
        <div className="flex items-center gap-2 text-xs text-text-tertiary">
          <span>{formatFileSize(file.size)}</span>
          {isUploading && uploadProgress !== undefined && (
            <span className="text-primary">{uploadProgress}%</span>
          )}
        </div>
      </div>

      {/* Upload Progress or Remove Button */}
      {isUploading ? (
        <div className="flex items-center gap-2">
          <span className="status-badge status-badge-gen">{"<GEN>"}</span>
          {uploadProgress !== undefined && (
            <div className="w-16 h-1 bg-border rounded-full overflow-hidden">
              <div
                className="h-full bg-primary transition-all duration-300"
                style={{ width: `${uploadProgress}%` }}
              />
            </div>
          )}
        </div>
      ) : (
        onRemove && (
          <button
            onClick={onRemove}
            className="p-1 text-text-tertiary hover:text-error hover:bg-error/10 transition-colors"
            title={`Remove ${file.name}`}
          >
            <X className="w-4 h-4" />
          </button>
        )
      )}
    </div>
  );
}

// File upload drop zone component
interface FileDropZoneProps {
  onFilesSelected: (files: File[]) => void;
  isDragging?: boolean;
}

export function FileDropZone({ onFilesSelected: _onFilesSelected, isDragging = false }: FileDropZoneProps) {
  return (
    <div
      className={`
        flex flex-col items-center justify-center p-8 border-2 border-dashed rounded-xl
        transition-colors duration-150
        ${
          isDragging
            ? "border-primary bg-primary/5"
            : "border-border bg-bg-tertiary hover:border-border-dark"
        }
      `}
    >
      <Upload className="w-10 h-10 text-text-tertiary mb-3" />
      <p className="text-sm font-medium text-text-primary">
        Drop files here or click to browse
      </p>
      <p className="text-xs text-text-tertiary mt-1">
        Supports TXT, MD, JSON, code, and images
      </p>
    </div>
  );
}
