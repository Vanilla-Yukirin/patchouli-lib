import { describe, expect, it } from "vitest";
import vectors from "../../tests/vectors/revision-titles.json";
import { renderMarkdown, titleFromMarkdown } from "./markdown";
import { applyReferenceEdits, referencesForRename } from "./references";

describe("shared write-time title contract", () => {
	it("matches the same vectors as the Python writer", () => {
		for (const vector of vectors) {
			const actual =
				vector.filename && /\.(?:md|markdown|mdown|mkd)$/i.test(vector.filename)
					? titleFromMarkdown(vector.content)
					: null;
			expect(actual || "无标题", vector.name).toBe(vector.title);
		}
		const large = `# Large title\n\n${"<unsafe>&".repeat(140_000)}`;
		expect(titleFromMarkdown(large)).toBe("Large title");
		expect(renderMarkdown(large, [])).toBe(
			`<pre># Large title\n\n${"&lt;unsafe&gt;&amp;".repeat(140_000)}</pre>`,
		);
	});
});
it("applies only selected Markdown targets and rejects stale offsets", () => {
	const sources = {
		"note.md": "![image](old.png)\n`old.png`\n[text](old.png)\n",
		"data.json": '"old.png"',
	};
	const edits = referencesForRename(sources, "old.png", "new.png");
	expect(edits).toHaveLength(2);
	edits[1].selected = false;
	const output = applyReferenceEdits(sources, edits);
	expect(output["note.md"]).toBe(
		"![image](new.png)\n`old.png`\n[text](old.png)\n",
	);
	expect(output["data.json"]).toBe(sources["data.json"]);
	expect(() =>
		applyReferenceEdits(
			{ ...sources, "note.md": `${sources["note.md"]}changed` },
			edits,
		),
	).toThrow("reference_suggestions_stale");
});
