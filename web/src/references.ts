/** Suggestions are tied to exact source bytes; applying stale offsets is rejected. */
import MarkdownIt from "markdown-it";
export function resolveLocalTarget(
	sourceFile: string,
	target: string,
): { path: string; suffix: string } | null {
	// biome-ignore lint/suspicious/noControlCharactersInRegex: reject unsafe local path controls.
	if (/^[a-z][a-z0-9+.-]*:|^[\\/]|[\u0000-\u001f]/i.test(target)) return null;
	const marker = target.search(/[?#]/);
	const path = marker < 0 ? target : target.slice(0, marker);
	const suffix = marker < 0 ? "" : target.slice(marker);
	let decoded: string;
	try {
		decoded = decodeURIComponent(path);
	} catch {
		return null;
	}
	const parts = sourceFile.split("/").slice(0, -1);
	for (const part of decoded.split("/")) {
		if (!part || part === ".") continue;
		if (part === "..") {
			if (!parts.length) return null;
			parts.pop();
		} else parts.push(part);
	}
	return { path: parts.join("/"), suffix };
}
function relativeTarget(sourceFile: string, target: string) {
	const from = sourceFile.split("/").slice(0, -1),
		to = target.split("/");
	while (from.length && to.length && from[0] === to[0]) {
		from.shift();
		to.shift();
	}
	return [...from.map(() => ".."), ...to].join("/");
}
export type ReferenceEdit = {
	id: string;
	file: string;
	start: number;
	end: number;
	line: number;
	column: number;
	before: string;
	after: string;
	source: string;
	selected: boolean;
};
export function referencesForRename(
	sources: Record<string, string>,
	oldName: string,
	newName: string,
): ReferenceEdit[] {
	const result: ReferenceEdit[] = [];
	for (const [file, source] of Object.entries(sources)) {
		if (!/\.(?:md|markdown|mdown|mkd)$/i.test(file)) continue;
		// Mask fenced and inline code, preserving positions and newlines.
		const lineOffsets = [0];
		for (let index = 0; index < source.length; index++)
			if (source[index] === "\n") lineOffsets.push(index + 1);
		const masked = source.split("");
		for (const token of new MarkdownIt("commonmark", { html: true }).parse(
			source,
			{},
		))
			if (
				token.map &&
				["fence", "code_block", "html_block"].includes(token.type)
			) {
				const start = lineOffsets[token.map[0]] ?? source.length;
				const end = lineOffsets[token.map[1]] ?? source.length;
				for (let index = start; index < end; index++)
					if (masked[index] !== "\n") masked[index] = " ";
			}
		const mask = masked
			.join("")
			.replace(/(`+)[^\n]*?\1/g, (value) => " ".repeat(value.length));
		const patterns = [
			/!?\[[^\]\n]*\]\(\s*(<[^>\n]+>|[^\s)]+)(?:\s+[^)]*)?\)/g,
			/^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)/gm,
		];
		for (const pattern of patterns)
			for (const match of mask.matchAll(pattern)) {
				const raw = match[1];
				const target = raw.replace(/^<|>$/g, "");
				const resolved = resolveLocalTarget(file, target);
				if (!resolved || resolved.path !== oldName) continue;
				const start =
					(match.index ?? 0) +
					match[0].indexOf(raw) +
					(raw.startsWith("<") ? 1 : 0);
				const prefix = source.slice(0, start);
				result.push({
					id: `${file}:${start}`,
					file,
					start,
					end: start + target.length,
					line: prefix.split("\n").length,
					column: start - prefix.lastIndexOf("\n"),
					before: target,
					after:
						encodeURI(
							relativeTarget(file === oldName ? newName : file, newName),
						) + resolved.suffix,
					source,
					selected: true,
				});
			}
	}
	return result;
}
export function applyReferenceEdits(
	sources: Record<string, string>,
	edits: ReferenceEdit[],
) {
	const output = { ...sources };
	for (const edit of [...edits]
		.filter((x) => x.selected)
		.sort((a, b) => b.start - a.start)) {
		if (sources[edit.file] !== edit.source)
			throw new Error("reference_suggestions_stale");
		output[edit.file] =
			output[edit.file].slice(0, edit.start) +
			edit.after +
			output[edit.file].slice(edit.end);
	}
	return output;
}
