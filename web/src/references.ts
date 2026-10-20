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
	// Retain parsed definition source maps instead of guessing their label syntax.
	parser.core.ruler.disable("strip_references");
	for (const [file, source] of Object.entries(sources)) {
		if (!/\.(?:md|markdown|mdown|mkd)$/i.test(file)) continue;
		// Mask block code and HTML, preserving source positions and newlines.
		const lineOffsets = [0];
		for (let index = 0; index < source.length; index++)
			if (source[index] === "\n") lineOffsets.push(index + 1);
		const masked = source.split("");
		const environment = {};
		const tokens = parser.parse(source, environment);
		const definitions = new Map<number, number>();
		const inlineRanges: { start: number; end: number }[] = [];
		for (const token of tokens) {
			if (token.map && token.type === "inline")
				inlineRanges.push({
					start: lineOffsets[token.map[0]] ?? source.length,
					end: lineOffsets[token.map[1]] ?? source.length,
				});
			if (token.map && token.type === "reference_definition") {
				const start = lineOffsets[token.map[0]] ?? source.length;
				const end = lineOffsets[token.map[1]] ?? source.length;
				const label = source.indexOf("[", start);
				if (label >= start && label < end) definitions.set(label, end);
			}
			if (
				token.map &&
				["fence", "code_block", "html_block"].includes(token.type)
			) {
				const start = lineOffsets[token.map[0]] ?? source.length;
				const end = lineOffsets[token.map[1]] ?? source.length;
				for (let index = start; index < end; index++)
					if (masked[index] !== "\n") masked[index] = " ";
			}
		}
		const mask = masked.join("");
		const state = new parser.inline.State(mask, parser, environment, []);
		const codeState = new parser.inline.State(mask, parser, environment, []);
		const escaped = (position: number) => {
			let previous = position - 1;
			while (previous >= 0 && mask[previous] === "\\") previous--;
			return (position - previous - 1) % 2 === 1;
		};
		let skippedThrough = 0;
		let inlineRangeIndex = 0;
		const nonLinks: { start: number; end: number }[] = [];
		for (const candidate of mask.matchAll(/\[|<|`/g)) {
			const opening = candidate.index;
			let excluded = nonLinks.at(-1);
			while (excluded && opening >= excluded.end) {
				nonLinks.pop();
				excluded = nonLinks.at(-1);
			}
			if (excluded && opening >= excluded.start) continue;
			if (opening < skippedThrough || escaped(opening)) continue;
			while (
				inlineRanges[inlineRangeIndex] &&
				opening >= inlineRanges[inlineRangeIndex].end
			)
				inlineRangeIndex++;
			const range = inlineRanges[inlineRangeIndex];
			if (mask[opening] === "`") {
				if (range && opening >= range.start) {
					codeState.pos = opening;
					codeState.posMax = range.end;
					parser.inline.skipToken(codeState);
					skippedThrough = codeState.pos;
				}
				continue;
			}
			const parseEnd =
				definitions.get(opening) ??
				(range && opening >= range.start ? range.end : undefined);
			if (parseEnd === undefined) continue;
			state.pos = opening;
			state.posMax = parseEnd;
			if (mask[opening] === "<") {
				parser.inline.skipToken(state);
				skippedThrough = state.pos;
				continue;
			}
			const image = mask[opening - 1] === "!" && !escaped(opening - 1);
			const labelEnd = parser.helpers.parseLinkLabel(state, opening, !image);
			const inline = mask[labelEnd + 1] === "(";
			if (
				labelEnd < 0 ||
				(!inline && (!definitions.has(opening) || mask[labelEnd + 1] !== ":"))
			)
				continue;
			let destinationStart = labelEnd + 2;
			while (
				destinationStart < parseEnd &&
				/[ \t\r\n]/.test(mask[destinationStart])
			)
				destinationStart++;
			const destination = parser.helpers.parseLinkDestination(
				mask,
				destinationStart,
				parseEnd,
			);
			if (!destination.ok) continue;
			let nonLinkEnd = definitions.get(opening) ?? destination.pos;
			if (inline) {
				let pos = destination.pos;
				while (pos < parseEnd && /[ \t\r\n]/.test(mask[pos])) pos++;
				if (pos > destination.pos) {
					const title = parser.helpers.parseLinkTitle(mask, pos, parseEnd);
					if (title.ok) {
						pos = title.pos;
						while (pos < parseEnd && /[ \t\r\n]/.test(mask[pos])) pos++;
					}
				}
				if (mask[pos] !== ")") continue;
				nonLinkEnd = pos + 1;
			}
			// Labels can contain images; only the destination/title is non-link text.
			nonLinks.push({ start: destinationStart, end: nonLinkEnd });
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
