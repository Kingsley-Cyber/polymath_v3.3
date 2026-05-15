/**
 * attachments.ts — convert browser File objects into ChatAttachment
 * payloads ready for inclusion in a ChatRequest.
 *
 * Image files are read as base64 (the `data:` URL prefix is stripped
 * so the backend can rebuild it from the mime_type field — keeps the
 * shape symmetric with what the validator expects).
 *
 * Text files (.md/.txt/.json/code files) are read as UTF-8 text. NOT
 * base64. The backend inlines the raw text into the augmented prompt.
 *
 * Out-of-scope for v1: PDF, DOCX, PPTX, image-bearing PDFs. The user
 * gets a friendly error and is steered toward extracting text or
 * exporting an image first.
 */

import type { ChatAttachment } from "../types/chat";

// Per-attachment binary size cap. Mirrors the backend validator
// (20MB). Reject earlier so the user gets immediate feedback instead
// of a backend 422.
export const ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;

// Max attachments per turn. Mirrors backend cap.
export const ATTACHMENT_MAX_COUNT = 4;

// MIME-prefix sets. Images flow through FileReader.readAsDataURL;
// everything else (text-ish) flows through readAsText.
const IMAGE_MIME_PREFIXES: ReadonlyArray<string> = ["image/"];

// Text-file MIME types we accept. Conservative allowlist — easier to
// add than to retract.
const TEXT_MIME_TYPES: ReadonlySet<string> = new Set([
  "text/plain",
  "text/markdown",
  "text/csv",
  "text/html",
  "text/xml",
  "text/css",
  "text/javascript",
  "application/json",
  "application/xml",
  "application/x-yaml",
  "application/yaml",
  "application/toml",
  "application/x-toml",
  "application/x-typescript",
  "application/typescript",
  "application/javascript",
]);

// File extensions that get treated as text regardless of MIME (browsers
// often serve up empty or generic types for source files).
const TEXT_EXTENSIONS: ReadonlySet<string> = new Set([
  "md", "txt", "json", "yaml", "yml", "toml", "ini", "cfg",
  "csv", "tsv", "log",
  "ts", "tsx", "js", "jsx", "mjs", "cjs",
  "py", "rb", "go", "rs", "java", "kt", "swift",
  "c", "cc", "cpp", "h", "hpp",
  "cs", "php", "scala", "groovy",
  "lua", "luau",
  "sh", "bash", "zsh", "fish",
  "sql", "graphql", "gql",
  "html", "htm", "xml", "svg",
  "css", "scss", "sass", "less",
  "dockerfile",
  "vue", "svelte",
  "r", "jl", "ex", "exs",
]);

function extOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0) return "";
  return filename.slice(dot + 1).toLowerCase();
}

export type AttachmentKind = "image" | "text";

export function inferAttachmentKind(file: File): AttachmentKind | null {
  const mime = (file.type || "").toLowerCase();
  for (const prefix of IMAGE_MIME_PREFIXES) {
    if (mime.startsWith(prefix)) return "image";
  }
  if (TEXT_MIME_TYPES.has(mime)) return "text";
  if (TEXT_EXTENSIONS.has(extOf(file.name))) return "text";
  return null;
}

function readAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("FileReader returned non-string for image"));
        return;
      }
      // result is `data:image/png;base64,xxx` — strip the prefix.
      // The backend stores the base64 portion only and reconstructs
      // the data URI from mime_type + content.
      const comma = result.indexOf(",");
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

function readAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result;
      if (typeof result !== "string") {
        reject(new Error("FileReader returned non-string for text"));
        return;
      }
      resolve(result);
    };
    reader.onerror = () => reject(reader.error ?? new Error("read failed"));
    reader.readAsText(file, "utf-8");
  });
}

export type ConvertResult =
  | { ok: true; attachment: ChatAttachment }
  | { ok: false; reason: string; filename: string };

export async function fileToAttachment(file: File): Promise<ConvertResult> {
  if (file.size > ATTACHMENT_MAX_BYTES) {
    return {
      ok: false,
      filename: file.name,
      reason: `File exceeds ${Math.round(ATTACHMENT_MAX_BYTES / (1024 * 1024))}MB limit (got ${Math.round(
        file.size / 1024,
      )}KB).`,
    };
  }
  const kind = inferAttachmentKind(file);
  if (!kind) {
    return {
      ok: false,
      filename: file.name,
      reason: "Unsupported file type. Only images and text files (markdown, code, JSON, etc.) are accepted as attachments. PDFs need to be exported to text or images first.",
    };
  }
  try {
    const content =
      kind === "image" ? await readAsBase64(file) : await readAsText(file);
    return {
      ok: true,
      attachment: {
        filename: file.name,
        mime_type: file.type || (kind === "image" ? "image/png" : "text/plain"),
        size_bytes: file.size,
        kind,
        content,
      },
    };
  } catch (exc) {
    return {
      ok: false,
      filename: file.name,
      reason:
        exc instanceof Error ? exc.message : "Failed to read file contents.",
    };
  }
}

/**
 * Best-effort batch conversion. Returns successes and failures
 * separately so the caller can show a single composite error toast.
 */
export async function filesToAttachments(
  files: File[],
): Promise<{ ok: ChatAttachment[]; failed: Array<{ filename: string; reason: string }> }> {
  const results = await Promise.all(files.map(fileToAttachment));
  const ok: ChatAttachment[] = [];
  const failed: Array<{ filename: string; reason: string }> = [];
  for (const r of results) {
    if (r.ok) ok.push(r.attachment);
    else failed.push({ filename: r.filename, reason: r.reason });
  }
  return { ok, failed };
}
