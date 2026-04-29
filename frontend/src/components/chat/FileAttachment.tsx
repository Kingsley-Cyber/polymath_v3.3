// FileAttachment.tsx - File attachment display and upload component
import { X, Upload, Loader2 } from "lucide-react";

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

  const formatFileSize = (bytes: number): string => {
    if (bytes === 0) return "0 B";
    const k = 1024;
    const sizes = ["B", "KB", "MB", "GB"];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(1))} ${sizes[i]}`;
  };

  const getFileIcon = () => {
    const type = file.type;
    if (type.startsWith("image/")) return "🖼️";
    if (type.includes("pdf")) return "📄";
    if (type.includes("text")) return "📝";
    if (type.includes("json")) return "📋";
    return "📎";
  };

  return (
    <div
      className="relative flex items-center gap-2 px-3 py-2 bg-bg-tertiary border border-border rounded-lg group"
    >
      {/* File Icon */}
      <span className="text-lg">{getFileIcon()}</span>

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
          <Loader2 className="w-4 h-4 animate-spin text-primary" />
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
            className="opacity-0 group-hover:opacity-100 p-1 text-text-tertiary hover:text-error hover:bg-error/10 rounded transition-all"
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
        Supports PDF, TXT, MD, JSON, and images
      </p>
    </div>
  );
}
