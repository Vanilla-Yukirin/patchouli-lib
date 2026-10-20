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
	const parser = new MarkdownIt("commonmark", { html: true });
	for (const [file, source] of Object.entries(sources)) {
		if (!/\.(?:md|markdown|mdown|mkd)$/i.test(file)) continue;
		// Mask fenced and inline code, preserving positions and newlines.
		const lineOffsets = [0];
		for (let index = 0; index < source.length; index++)
			if (source[index] === "\n") lineOffsets.push(index + 1);
		const masked = source.split("");
		for (const token of parser.parse(source, {}))
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
			{ pattern: /!?\[[^\]\n]*\]\(\s*/g, inline: true },
			{ pattern: /^\s{0,3}\[[^\]\n]+\]:\s*/gm, inline: false },
		];
		for (const { pattern, inline } of patterns)
			for (const match of mask.matchAll(pattern)) {
				const destinationStart = match.index + match[0].length;
				const destination = parser.helpers.parseLinkDestination(
					mask,
					destinationStart,
					mask.length,
				);
				if (!destination.ok) continue;
				if (inline) {
					let pos = destination.pos;
					while (pos < mask.length && /[ \t\r\n]/.test(mask[pos])) pos++;
					if (pos > destination.pos) {
						const title = parser.helpers.parseLinkTitle(mask, pos, mask.length);
						if (title.ok) {
							pos = title.pos;
							while (pos < mask.length && /[ \t\r\n]/.test(mask[pos])) pos++;
						}
					}
					if (mask[pos] !== ")") continue;
				}
				const resolved = resolveLocalTarget(file, destination.str);
				if (!resolved || resolved.path !== oldName) continue;
				const angled = mask[destinationStart] === "<";
				const start = destinationStart + (angled ? 1 : 0);
				const end = destination.pos - (angled ? 1 : 0);
				const before = source.slice(start, end);
				// Keep the source spelling of the suffix, including escapes/entities.
				const marker = Array.from(
					before.matchAll(
						/\\[^\r\n]|&(?:#(?:x[\da-f]+|\d+)|[a-z][a-z\d]+);|[?#]/gi,
					),
				).find((part) => /^[?#]/.test(parser.utils.unescapeAll(part[0])));
				const suffix = marker ? before.slice(marker.index) : resolved.suffix;
				const prefix = source.slice(0, start);
				result.push({
					id: `${file}:${start}`,
					file,
					start,
					end,
					line: prefix.split("\n").length,
					column: start - prefix.lastIndexOf("\n"),
					before,
					after:
						relativeTarget(file === oldName ? newName : file, newName)
							.split("/")
							.map((part) =>
								encodeURIComponent(part).replace(
									/[!'()*]/g,
									(char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`,
								),
							)
							.join("/") + suffix,
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
