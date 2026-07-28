/**
 * Reading an uploaded file as text.
 *
 * `FileReader.readAsText` rather than `File.text()`: the latter is newer, and it is simply
 * absent in jsdom — which meant the two upload paths in this app (a run's input source, a
 * saved benchmark report) could only ever be exercised by hand. A path that cannot be tested
 * is a path that breaks quietly.
 *
 * UTF-8 with replacement, matching the old page's `decode("utf-8", errors="replace")`: one
 * undecodable byte in a log must not cost the operator the whole file.
 */
export function readTextFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error(`could not read ${file.name}`));
    reader.readAsText(file, "utf-8");
  });
}
