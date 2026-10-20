import { describe, expect, it } from "vitest";
import vectors from "../../tests/vectors/revision-titles.json";
import { isCanonicalAdminPath, legacyAdminDestination } from "./legacy-routes";
import { renderMarkdown, titleFromMarkdown } from "./markdown";
import {
	applyReferenceEdits,
	referencesForRename,
	resolveLocalTarget,
} from "./references";

describe("shared browser contracts", () => {
	it("matches write-time titles and retired navigation targets", () => {
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
		const oldPage = "/libraries/lib/sections/section/books/book/pages/page";
		const page = "/libraries/lib/pages/page";
		for (const [old, target] of [
			["/agents", "/credentials"],
			["/onboarding-tickets", "/credentials"],
			["/setup", "/settings"],
			["/master/setup", "/settings"],
			["/guide", "/help"],
			["/agent", "/help/agent"],
			["/mcp", "/help/mcp"],
			[oldPage, page],
			[`${oldPage}/files/edit`, page],
			[`${oldPage}/revisions/2/restore`, `${page}/revisions/2`],
			[`${oldPage}/revisions/2/files/docs%2Fnote.md`, `${page}/revisions/2`],
			["/libraries/lib/callers/caller", "/credentials/caller?library_id=lib"],
			[
				"/libraries/lib/callers/caller/credentials/key/grants",
				"/credentials/caller?library_id=lib",
			],
			["/libraries/lib/trash", "/trash?library_id=lib"],
			["/libraries/lib/sections/section/trash/page", "/trash?library_id=lib"],
			["/libraries/lib/tags", "/tags?library_id=lib"],
			[
				"/libraries/lib/sections/section/books/book/new-page",
				"/libraries/lib/pages/new?section=section&book=book",
			],
		]) {
			expect(legacyAdminDestination(old), old).toBe(target);
			expect(isCanonicalAdminPath(target.split("?")[0]), target).toBe(true);
		}
		for (const unknown of [
			"/libraries/lib/unknown",
			"/libraries/lib/pages",
			"/libraries/lib/callers/caller/unknown",
			"/libraries/lib/callers/caller/requests",
			`${oldPage}/revisions/2/extra`,
			`${oldPage}/revisions/2/files/%2e%2e%2Fsecret.md`,
			"/help/unknown",
		]) {
			expect(legacyAdminDestination(unknown), unknown).toBeNull();
			expect(isCanonicalAdminPath(unknown), unknown).toBe(false);
		}
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
	const nested = {
		"docs/note.md": "![image](../old.png?download=1%202#preview%20part)\n",
	};
	const renamed = referencesForRename(
		nested,
		"old.png",
		"assets/image#2?100% ready.(x).png",
	);
	expect(renamed).toHaveLength(1);
	expect(renamed[0].after).toBe(
		"../assets/image%232%3F100%25%20ready.%28x%29.png?download=1%202#preview%20part",
	);
	expect(applyReferenceEdits(nested, renamed)["docs/note.md"]).toBe(
		"![image](../assets/image%232%3F100%25%20ready.%28x%29.png?download=1%202#preview%20part)\n",
	);
	expect(resolveLocalTarget("docs/note.md", renamed[0].after)).toEqual({
		path: "assets/image#2?100% ready.(x).png",
		suffix: "?download=1%202#preview%20part",
	});
	expect(resolveLocalTarget("docs/note.md", "/absolute.png")).toBeNull();
	expect(resolveLocalTarget("docs/note.md", "../../outside.png")).toBeNull();
	expect(
		resolveLocalTarget("docs/note.md", "https://example.invalid/old.png"),
	).toBeNull();
	const parenthesized = {
		"paren.md":
			"[asset](image(2).png)\n[escaped](image\\(2\\).png?download=1%202#preview \"Keep (title) [literal](image(2).png)\")\n\n[asset-ref]: <image(2).png#anchor> 'Ref [literal](image(2).png)'\n[see [icon]](image(2).png)\n[escaped \\]](image(2).png)\n\\[literal](image(2).png)\n``literal ` tick\n[example](image(2).png)\n``\n\n[not a link\n\n](image(2).png)\n",
	};
	const parenEdits = referencesForRename(
		parenthesized,
		"image(2).png",
		"renamed.png",
	);
	expect(parenEdits).toHaveLength(5);
	expect(
		parenEdits.map((edit) =>
			parenthesized["paren.md"].slice(edit.start, edit.end),
		),
	).toEqual(parenEdits.map((edit) => edit.before));
	expect(applyReferenceEdits(parenthesized, parenEdits)["paren.md"]).toBe(
		"[asset](renamed.png)\n[escaped](renamed.png?download=1%202#preview \"Keep (title) [literal](image(2).png)\")\n\n[asset-ref]: <renamed.png#anchor> 'Ref [literal](image(2).png)'\n[see [icon]](renamed.png)\n[escaped \\]](renamed.png)\n\\[literal](image(2).png)\n``literal ` tick\n[example](image(2).png)\n``\n\n[not a link\n\n](image(2).png)\n",
	);
});
