/** The only management-API adapter. No Bearer token or credential persistence. */
import createClient from "openapi-fetch";
import type { components, paths } from "./generated/admin";
export type Session = Pick<
	components["schemas"]["SessionState"],
	"authenticated"
> &
	Partial<components["schemas"]["SessionState"]>;
export type Structure = {
	id: string;
	name: string;
	description?: string;
	summary?: string;
	readme: string;
	updated_at: number;
	page_count?: number;
};
export type FileEntry = {
	path: string;
	filename?: string;
	size_bytes: number;
	sha256: string;
	download_href: string;
	content_href?: string;
	preview_href?: string;
	preview_kind?: "markdown" | "text" | "image" | "binary";
	editable?: boolean;
};
export type Revision = {
	revision_id: string;
	revision_number: number;
	title: string;
	primary_file: string | null;
	created_at: number;
	files: FileEntry[];
};
export type DocumentView = {
	tag_choices?: { id: string; name: string; attached: boolean }[];
	revisions?: Revision[];
	older_revisions_before?: number | null;
	description?: string;
	library_id: string;
	section_id?: string;
	book_id?: string;
	page_id: string;
	title: string;
	occurred_at: number;
	updated_at: number;
	current_etag: string;
	revision: Revision;
};
export class ApiError extends Error {
	constructor(
		public status: number,
		public code: string,
		public fields: Record<string, string> = {},
	) {
		super(code);
	}
}
let csrf = "";
let sessionEpoch = 0;
export const SESSION_EXPIRED_EVENT = "patchouli:session-expired";
export function setSessionCsrf(value: string, renewed = false) {
	csrf = value;
	// A background session read is not a new login. Only explicit renewal
	// invalidates old requests independently of the CSRF synchronization effect.
	if (renewed) sessionEpoch += 1;
}
function expireSession(epoch: number) {
	if (epoch !== sessionEpoch) return;
	csrf = "";
	sessionEpoch += 1;
	window.dispatchEvent(new Event(SESSION_EXPIRED_EVENT));
}
export async function request<T>(
	path: string,
	options: RequestInit = {},
): Promise<T> {
	path = path
		.replace(
			/^(libraries\/[^/]+\/pages\/[^/]+)\/revisions\/([^/?]+)$/,
			"$1?revision_number=$2",
		)
		.replace(
			/^(libraries\/[^/]+\/skills\/[^/]+)\/revisions\/([^/?]+)$/,
			"$1?revision_id=$2",
		)
		.replace(/^(libraries\/[^/]+\/skills\/[^/?]+)\/files$/, "$1")
		.replace(/^(libraries\/[^/]+\/skills\/[^/?]+)\/revisions$/, "$1/versions")
		.replace(
			/^libraries\/[^/]+\/sections\/([^/]+)\/books\/([^/?]+)(.*)$/,
			"books/$2$3",
		)
		.replace(/^libraries\/[^/]+\/sections\/([^/?]+)(.*)$/, "sections/$1$2");
	if (
		options.body &&
		typeof options.body === "string" &&
		/^(?:sections\/[^/]+\/books|books\/[^/?]+)$/.test(path)
	) {
		const body = JSON.parse(options.body);
		if ("description" in body) {
			body.summary = body.description;
			delete body.description;
			options = { ...options, body: JSON.stringify(body) };
		}
	}
	const headers = new Headers(options.headers);
	if (options.method && !["GET", "HEAD"].includes(options.method))
		headers.set("X-CSRF-Token", csrf);
	if (
		options.body &&
		!(options.body instanceof FormData) &&
		!headers.has("Content-Type")
	)
		headers.set("Content-Type", "application/json");
	const epoch = sessionEpoch;
	const response = await fetch(
		path.startsWith("/admin/api/") ? path : `/admin/api/${path}`,
		{
			...options,
			headers,
			credentials: "same-origin",
			redirect: "error",
			cache: "no-store",
		},
	);
	if (!response.ok) {
		let data: {
			code?: string;
			error?: { code: string; message: string };
			field_errors?: Record<string, string>;
		} = {};
		try {
			data = await response.json();
		} catch {}
		const code = data.error?.code || data.code || "request_failed";
		if (response.status === 401 && code === "sign_in_required")
			expireSession(epoch);
		throw new ApiError(response.status, code, data.field_errors);
	}
	if (response.status === 204) return undefined as T;
	const result = await response.json();
	if (Array.isArray(result?.pages))
		for (const page of result.pages)
			if ("stored_title" in page) page.title = page.stored_title || "";
	if (path === "dashboard")
		return {
			...result,
			activity: {
				items: Array.isArray(result.activity)
					? result.activity
					: result.activity.items,
			},
			counts: {
				libraries: result.libraries.length,
				pages: result.libraries.reduce(
					(sum: number, item: { page_count: number }) => sum + item.page_count,
					0,
				),
			},
		} as T;
	if (path === "tags")
		return {
			...result,
			items: result.items.map(
				(item: { library_id: string; library_name: string; tag: object }) => ({
					...item.tag,
					library_id: item.library_id,
					library_name: item.library_name,
				}),
			),
		} as T;
	if (result?.skill && result.files)
		return {
			description: (result.selected_revision || result.skill).description,
			library_id: result.skill.library_id,
			page_id: result.skill.id,
			title: (result.selected_revision || result.skill).name,
			occurred_at:
				result.selected_revision?.created_at ?? result.skill.updated_at,
			updated_at: result.skill.updated_at,
			current_etag: result.skill.etag,
			revision: {
				revision_id: (result.selected_revision || result.skill).revision_id,
				revision_number: (result.selected_revision || result.skill)
					.revision_number,
				title: (result.selected_revision || result.skill).name,
				primary_file: "SKILL.md",
				created_at: (result.selected_revision || result.skill).created_at,
				files: result.files.map(
					(file: { name: string; text_href?: string; editable: boolean }) => ({
						...file,
						path: file.name,
						content_href: file.text_href,
						preview_kind: file.editable
							? /\.(?:md|markdown|mdown|mkd)$/i.test(file.name)
								? "markdown"
								: "text"
							: "binary",
					}),
				),
			},
		} as T;
	if (result?.page && result.files && result.revision_id)
		return {
			...result.page,
			title: result.stored_title || "",
			tag_choices: result.tag_choices,
			older_revisions_before: result.older_revisions_before,
			revisions: result.revisions,
			library_id: result.library.id,
			section_id: result.section?.id,
			book_id: result.book?.id,
			page_id: result.page.id,
			current_etag: result.current_etag,
			revision: {
				revision_id: result.revision_id,
				revision_number: result.revision_number,
				title: result.stored_title || "",
				primary_file: result.primary_file,
				created_at:
					result.selected_revision_created_at || result.page.updated_at,
				files: result.files.map(
					(file: {
						name: string;
						editable: boolean;
						preview_href?: string;
						text_href?: string;
					}) => ({
						...file,
						path: file.name,
						content_href: file.text_href,
						preview_kind: file.preview_href
							? "image"
							: file.editable
								? /\.(?:md|markdown|mdown|mkd)$/i.test(file.name)
									? "markdown"
									: "text"
								: "binary",
					}),
				),
			},
		} as T;
	return result;
}
export const api = {
	session: async () => {
		const { data, error, response } =
			await browserClient.GET("/admin/api/session");
		if (!data)
			throw new ApiError(
				response.status,
				error ? "session_unavailable" : "invalid_session_response",
			);
		return data;
	},
	login: async (token: string) => {
		const { data, error, response } = await browserClient.POST(
			"/admin/api/session",
			{ body: { token } },
		);
		if (!data)
			throw new ApiError(
				response.status,
				error ? "invalid_master_token" : "invalid_session_response",
			);
		return data;
	},
	logout: () => request<void>("session", { method: "DELETE" }),
};
const browserClient = createClient<paths>({
	credentials: "same-origin",
	cache: "no-store",
	redirect: "error",
});
export function safeHref(value: string) {
	const url = new URL(value, location.origin);
	if (url.origin !== location.origin)
		throw new Error("Cross-origin resource rejected");
	return url.pathname + url.search;
}
export async function readText(file: FileEntry) {
	const epoch = sessionEpoch;
	const response = await fetch(
		safeHref(file.content_href || file.download_href),
		{ credentials: "same-origin", cache: "no-store", redirect: "error" },
	);
	if (!response.ok) {
		if (response.status === 401) {
			const data = await response.json().catch(() => null);
			if ((data?.error?.code || data?.code) === "sign_in_required")
				expireSession(epoch);
		}
		throw new ApiError(response.status, "file_unavailable");
	}
	const bytes = new Uint8Array(await response.arrayBuffer());
	if (bytes.includes(0)) throw new Error("Binary file");
	return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
		bytes,
	);
}
