import type { FileEntry } from "./api";
import { renderMarkdown } from "./markdown";
export function Markdown({
	source,
	files = [],
	guide = false,
	sourceFile,
}: {
	source: string;
	files?: FileEntry[];
	guide?: boolean;
	sourceFile?: string;
}) {
	return (
		<article
			className="prose"
			dangerouslySetInnerHTML={{
				__html: renderMarkdown(source, files, guide, sourceFile),
			}}
		/>
	);
}
