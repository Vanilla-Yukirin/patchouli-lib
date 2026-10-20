import DOMPurify from "dompurify";
import MarkdownIt from "markdown-it";
import type { FileEntry } from "./api";
import { resolveLocalTarget } from "./references";

// biome-ignore lint/suspicious/noControlCharactersInRegex: reject URL controls and backslashes before rendering.
const controls = /[\u0000-\u0020\u007f\\]/;
function rootUrl(value: string) {
	return value.startsWith("/") &&
		!value.startsWith("//") &&
		!controls.test(value)
		? value
		: null;
}
/** Only library-owned manifests have a matching editable management resource. */
export function librarySkillWorkspaceHref(
	target: string,
	origin: string,
	base = "/admin",
) {
	if (controls.test(target)) return null;
	try {
		const url = new URL(target, origin);
		if (url.origin !== origin || url.username || url.password) return null;
		const match =
			/^\/api\/v1\/libraries\/([a-f0-9]{32})\/skills\/([a-f0-9]{32})(?:\/revisions\/(rev_[a-f0-9]{32}))?\/manifest$/.exec(
				url.pathname,
			);
		if (!match) return null;
		return `${base}/libraries/${match[1]}/skills/${match[2]}${match[3] ? `/revisions/${match[3]}` : ""}`;
	} catch {
		return null;
	}
}
export function titleFromMarkdown(source: string) {
	if (
		new TextEncoder().encode(source).length > 1024 * 1024 ||
		source.includes("\0")
	)
		return null;
	const lines = source.match(/[^\r\n]*(?:\r\n|\n|\r|$)/g) || [];
	let clean = source;
	if (lines[0]?.trim() === "---") {
		const closing = lines.findIndex(
			(line, index) => index > 0 && ["---", "..."].includes(line.trim()),
		);
		if (closing > 0) clean = lines.slice(closing + 1).join("");
	}
	const tokens = new MarkdownIt("commonmark", { html: true }).parse(clean, {});
	for (let i = 0; i < tokens.length; i++)
		if (tokens[i].type === "heading_open" && tokens[i].tag === "h1")
			return (
				(tokens[i + 1]?.children || [])
					.filter((token) =>
						["text", "code_inline", "image", "softbreak", "hardbreak"].includes(
							token.type,
						),
					)
					.map((token) =>
						["softbreak", "hardbreak"].includes(token.type)
							? " "
							: token.content,
					)
					.join("")
					.trim() || null
			);
	return null;
}
export function renderMarkdown(
	source: string,
	files: FileEntry[],
	allowRootLinks = false,
	sourceFile = "document.md",
) {
	if (
		new TextEncoder().encode(source).length > 1024 * 1024 ||
		source.includes("\0")
	)
		return `<pre>${new MarkdownIt().utils.escapeHtml(source)}</pre>`;
	const parser = new MarkdownIt("default", {
		html: false,
		linkify: false,
		typographer: false,
	});
	parser.enable("table");
	const find = (target: string) => {
		const resolved = resolveLocalTarget(sourceFile, target);
		return resolved
			? files.find((file) => file.path === resolved.path)
			: undefined;
	};
	const links: boolean[] = [];
	parser.renderer.rules.image = (tokens, index) => {
		const token = tokens[index];
		const file = find(String(token.attrGet("src") || ""));
		const href = file?.preview_href && rootUrl(file.preview_href);
		return href && file?.preview_kind === "image"
			? `<img src="${parser.utils.escapeHtml(href)}" alt="${parser.utils.escapeHtml(token.content)}" loading="lazy">`
			: parser.utils.escapeHtml(token.content);
	};
	parser.renderer.rules.link_open = (tokens, index) => {
		const target = String(tokens[index].attrGet("href") || "");
		const file = find(target);
		let href = file?.download_href && rootUrl(file.download_href);
		let external = false;
		if (!href && allowRootLinks && typeof location !== "undefined")
			href = librarySkillWorkspaceHref(
				target,
				location.origin,
				location.pathname.startsWith("/admin/next") ? "/admin/next" : "/admin",
			);
		if (!href && allowRootLinks) href = rootUrl(target);
		if (!href && !controls.test(target)) {
			try {
				const url = new URL(target);
				if (url.protocol === "https:" && !url.username && !url.password) {
					href = target;
					external = true;
				}
			} catch {}
		}
		links.push(Boolean(href));
		return href
			? `<a href="${parser.utils.escapeHtml(href)}"${external ? ' rel="noreferrer noopener"' : ""}>`
			: "<span>";
	};
	parser.renderer.rules.link_close = () => (links.pop() ? "</a>" : "</span>");
	return DOMPurify.sanitize(parser.render(source), {
		USE_PROFILES: { html: true },
		FORBID_TAGS: ["style", "iframe", "form", "input", "video", "audio"],
		FORBID_ATTR: ["style"],
	});
}
