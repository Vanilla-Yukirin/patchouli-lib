const identities = /^[a-z0-9_-]+$/i;
const revisionNumber = /^[1-9][0-9]{0,18}$/;

function validRevision(value: string | undefined): value is string {
	return Boolean(
		value &&
			revisionNumber.test(value) &&
			(value.length < 19 || value <= "9223372036854775807"),
	);
}

function validFilePath(parts: string[]): boolean {
	if (!parts.length) return false;
	try {
		const decoded = decodeURIComponent(parts.join("/"));
		return decoded
			.split("/")
			.every(
				(name) =>
					name !== "" &&
					name !== "." &&
					name !== ".." &&
					!name.includes("\\") &&
					!Array.from(name).some(
						(char) => char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127,
					),
			);
	} catch {
		return false;
	}
}

/** Only the implemented SPA screen shapes are canonical; unknown tails stay 404. */
export function isCanonicalAdminPath(pathname: string): boolean {
	if (
		[
			"/",
			"/libraries",
			"/search",
			"/tags",
			"/trash",
			"/credentials",
			"/requests",
			"/settings",
			"/help",
			"/help/agent",
			"/help/mcp",
		].includes(pathname)
	)
		return true;
	const parts = pathname.split("/");
	if (parts.shift() !== "" || parts.some((part) => !part)) return false;
	if (parts[0] === "credentials")
		return parts.length === 2 && identities.test(parts[1]);
	if (parts[0] !== "libraries" || !parts[1] || !identities.test(parts[1]))
		return false;
	if (parts.length === 2) return true;
	if (parts[2] === "search") return parts.length === 3;
	if (parts[2] === "tags")
		return parts.length === 4 && identities.test(parts[3]);
	if (parts[2] === "sections")
		return (
			Boolean(parts[3]) &&
			identities.test(parts[3]) &&
			(parts.length === 4 ||
				(parts.length === 6 &&
					parts[4] === "books" &&
					identities.test(parts[5])))
		);
	if (parts[2] === "skills" && parts.length === 3) return true;
	if (
		(parts[2] !== "pages" && parts[2] !== "skills") ||
		!parts[3] ||
		!identities.test(parts[3])
	)
		return false;
	if (parts.length === 4) return true;
	if (parts[3] === "new") return false;
	if (parts.length === 5 && parts[4] === "edit") return true;
	return (
		parts.length === 6 &&
		parts[4] === "revisions" &&
		(parts[2] === "pages"
			? validRevision(parts[5])
			: /^rev_[a-f0-9]{32}$/.test(parts[5]))
	);
}

/** Map retired GET screens; pathname is application-relative, without a query. */
export function legacyAdminDestination(pathname: string): string | null {
	const path = pathname.endsWith("/") ? pathname.slice(0, -1) : pathname;
	const fixed: Record<string, string> = {
		"/login": "/",
		"/setup": "/settings",
		"/master/setup": "/settings",
		"/agents": "/credentials",
		"/onboarding-tickets": "/credentials",
		"/guide": "/help",
		"/agent": "/help/agent",
		"/mcp": "/help/mcp",
	};
	if (Object.hasOwn(fixed, path)) return fixed[path];
	const parts = path.split("/");
	if (parts.shift() !== "" || parts.some((part) => !part)) return null;
	const [root, libraryId, group, identity] = parts;
	if (root !== "libraries" || !libraryId || !identities.test(libraryId))
		return null;
	const filtered = (screen: string) =>
		`/${screen}?${new URLSearchParams({ library_id: libraryId })}`;
	if (parts.length === 3 && (group === "trash" || group === "tags"))
		return filtered(group);
	if (group === "callers" && identity && identities.test(identity)) {
		if (
			parts.length === 4 ||
			(parts.length === 7 &&
				parts[4] === "credentials" &&
				identities.test(parts[5]) &&
				parts[6] === "grants")
		)
			return `/credentials/${identity}?${new URLSearchParams({ library_id: libraryId })}`;
		return null;
	}
	if (
		group === "pages" &&
		identity &&
		identities.test(identity) &&
		parts[4] === "revisions" &&
		validRevision(parts[5]) &&
		parts[6] === "preview-images" &&
		validFilePath(parts.slice(7))
	)
		return `/libraries/${libraryId}/pages/${identity}/revisions/${parts[5]}`;
	if (group !== "sections" || !identity || !identities.test(identity))
		return null;
	const sectionId = identity;
	if (
		parts[4] === "trash" &&
		(parts.length === 5 || (parts.length === 6 && identities.test(parts[5])))
	)
		return filtered("trash");
	if (parts[4] !== "books" || !parts[5] || !identities.test(parts[5]))
		return null;
	const bookId = parts[5];
	if (parts.length === 7 && parts[6] === "new-page")
		return `/libraries/${libraryId}/pages/new?${new URLSearchParams({ section: sectionId, book: bookId })}`;
	if (parts[6] !== "pages" || !parts[7] || !identities.test(parts[7]))
		return null;
	const page = `/libraries/${libraryId}/pages/${parts[7]}`;
	if (
		parts.length === 8 ||
		(parts.length === 9 && parts[8] === "move") ||
		(parts.length === 10 && parts[8] === "files" && parts[9] === "edit")
	)
		return page;
	if (parts[8] !== "revisions" || !validRevision(parts[9])) return null;
	if (
		parts.length === 10 ||
		(parts.length === 11 && parts[10] === "restore") ||
		(parts[10] === "files" && validFilePath(parts.slice(11)))
	)
		return `${page}/revisions/${parts[9]}`;
	return null;
}
