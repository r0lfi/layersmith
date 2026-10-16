import { useState } from "react";
import { api, Build, formatBytes } from "../api";
import { isActive } from "./ui";

/** Delete button for a build row; confirms first and reports what was freed. */
export function DeleteBuildButton({ build, onDone }: {
  build: Build;
  onDone: (result: { error: boolean; text: string }) => void;
}) {
  const [deleting, setDeleting] = useState(false);
  const running = isActive(build.status);

  async function remove() {
    const archive = build.export_size ? ` its ${formatBytes(build.export_size)} archive,` : "";
    const confirmed = window.confirm(
      `Delete build #${build.number} (${build.project_name ?? "project"} ${build.version})?\n\n` +
      `This removes the build record,${archive} its air-gap bundle, log and scan results, and the image ` +
      `${build.image_ref ?? ""} from the local image store. Files and images another build still uses are kept.\n\n` +
      "This cannot be undone.");
    if (!confirmed) return;
    setDeleting(true);
    try {
      const result = await api.deleteBuild(build.id);
      const freed = result.freed_bytes ? `, ${formatBytes(result.freed_bytes)} freed` : "";
      const notes = result.notes.length ? ` ${result.notes.join(". ")}.` : "";
      onDone({ error: false, text: `Build #${result.number} deleted${freed}.${notes}` });
    } catch (exc) {
      onDone({ error: true, text: (exc as Error).message });
    } finally {
      setDeleting(false);
    }
  }

  return (
    <button
      className="danger"
      disabled={running || deleting}
      title={running ? "A running build cannot be deleted" : `Delete build #${build.number}`}
      onClick={(event) => {
        event.stopPropagation();
        remove();
      }}
    >
      {deleting ? "Deleting…" : "Delete"}
    </button>
  );
}
