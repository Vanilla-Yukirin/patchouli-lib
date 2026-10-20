import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
	BookOpen,
	ChevronRight,
	FileText,
	Folder,
	Plus,
	Search,
} from "lucide-react";
import { useEffect, useState } from "react";
import { request, type Structure, setSessionCsrf } from "./api";
import type { PageProps } from "./app";
import { SourceEditor } from "./async-editor";
import { LocalTime, usePreferences } from "./preferences";
import { Markdown } from "./rendered-markdown";
import { Empty, ErrorNotice, Loading, Modal, Panel } from "./ui";
export function useResource<T>(path: string, enabled = true) {
	return useQuery({
		queryKey: ["resource", path],
		enabled,
		queryFn: () => request<T>(path),
	});
}
type List<T> = {
	items: T[];
	next_cursor?: string | null;
	next_offset?: number | null;
};
type ActivityItem = {
	library_id: string;
	actor_home_library_id: string | null;
	actor_id: string | null;
	actor_name: string;
	action: string;
	stored_title?: string | null;
	page_title: string | null;
	section_id: string | null;
	book_id: string | null;
	page_id: string | null;
	page_deleted: boolean;
	tag_id: string | null;
	tag_name: string | null;
	occurred_at: number;
	revision_number: number | null;
	revision_id?: string | null;
	skill_id?: string | null;
	skill_name?: string | null;
};
const activityActionKeys: Record<string, string> = {
	"content.archive.create": "activity.pageCreated",
	"content.page.file_set.create": "activity.pageCreated",
	"content.archive.revise": "activity.pageRevised",
	"content.page.file_set.revise": "activity.pageRevised",
	"content.page.workspace_save": "activity.pageSaved",
	"content.page.revision_restore": "activity.pageVersionRestored",
	"content.archive.correct_occurrence": "activity.timeCorrected",
	"content.page.occurrence.correct": "activity.timeCorrected",
	"content.archive.delete": "activity.pageDeleted",
	"content.page.delete": "activity.pageDeleted",
	"content.archive.restore": "activity.pageRestored",
	"content.page.restore": "activity.pageRestored",
	"content.page.title.edit": "activity.titleChanged",
	"content.page.move": "activity.pageMoved",
	"library.skill.workspace_save": "activity.skillSaved",
	"library.skill.revision_restore": "activity.skillVersionRestored",
	"tag.create": "activity.tagCreated",
	"tag.page.attach": "activity.tagAttached",
	"tag.page.detach": "activity.tagDetached",
};
export function items<T>(value: List<T> | T[] | undefined): T[] {
	return Array.isArray(value) ? value : value?.items || [];
}
function Header({
	title,
	description,
	children,
}: {
	title: string;
	description?: string;
	children?: React.ReactNode;
}) {
	return (
		<div className="page-heading">
			<div>
				<p className="eyebrow">PATCHOULILIB</p>
				<h1>{title}</h1>
				{description && <p className="muted">{description}</p>}
			</div>
			<div className="actions">{children}</div>
		</div>
	);
}
export function Dashboard(props: PageProps) {
	const { t } = usePreferences();
	const data = useResource<{
		counts: Record<string, number>;
		activity: {
			items: ActivityItem[];
		};
		notices?: string[];
	}>("dashboard");
	if (data.isPending) return <Loading />;
	if (data.error) return <ErrorNotice error={data.error} />;
	return (
		<>
			<Header
				title={t("pages.welcomeBack")}
				description={t("pages.browseDiscoverOrPickUpWhereYou")}
			>
				<button type="button" onClick={() => props.go("/libraries")}>
					{t("app.openLibraries")}
				</button>
			</Header>
			<div className="stats">
				{Object.entries(data.data?.counts || {}).map(([label, count]) => (
					<Panel
						key={label}
						title={
							label === "libraries"
								? t("app.libraries")
								: label === "pages"
									? t("workspace.document")
									: label
						}
					>
						<strong className="stat">{count}</strong>
					</Panel>
				))}
			</div>
			{data.data?.notices?.map((note) => (
				<p className="alert" key={note}>
					{note}
				</p>
			))}
			<ActivityFeed {...props} initial={data.data?.activity.items || []} />
		</>
	);
}
function ActivityFeed(
	props: PageProps & {
		initial: ActivityItem[];
	},
) {
	const { t } = usePreferences();
	const [expanded, setExpanded] = useState(false);
	const [cursor, setCursor] = useState("");
	const data = useResource<{
		items: ActivityItem[];
		next_cursor?: string | null;
	}>(
		`activity${cursor ? `?before=${encodeURIComponent(cursor)}` : ""}`,
		expanded,
	);
	const rows = expanded && data.data ? data.data.items : props.initial;
	return (
		<Panel title={t("pages.recentActivity")}>
			<ul className="activity">
				{rows.map((item) => (
					<ActivityRow
						key={`${item.occurred_at}:${item.action}:${item.library_id}:${item.page_id || item.skill_id}:${item.revision_id}:${item.tag_id}:${item.actor_id}`}
						item={item}
						go={props.go}
					/>
				))}
			</ul>
			{!rows.length && (
				<Empty>{t("pages.noActivityYetYourNextChapterStarts")}</Empty>
			)}
			{expanded && data.isFetching && <Loading />}
			{expanded && data.error && <ErrorNotice error={data.error} />}
			{!expanded ? (
				<button
					type="button"
					className="secondary"
					onClick={() => setExpanded(true)}
				>
					{t("activity.more")}
				</button>
			) : (
				data.data?.next_cursor && (
					<button
						type="button"
						className="secondary"
						onClick={() => setCursor(data.data?.next_cursor || "")}
					>
						{t("activity.older")}
					</button>
				)
			)}
			{cursor && (
				<button
					type="button"
					className="secondary"
					onClick={() => setCursor("")}
				>
					{t("activity.latest")}
				</button>
			)}
		</Panel>
	);
}
function ActivityRow({
	item,
	go,
}: {
	item: ActivityItem;
	go: PageProps["go"];
}) {
	const { t } = usePreferences();
	const actor =
		item.actor_name === "Administrator"
			? t("activity.administrator")
			: item.actor_name || t("activity.administrator");
	const pagePath = item.page_id
		? item.page_deleted
			? `/trash?library_id=${item.library_id}`
			: `/libraries/${item.library_id}/pages/${item.page_id}${item.revision_number ? `/revisions/${item.revision_number}` : ""}`
		: null;
	const skillPath = item.skill_id
		? `/libraries/${item.library_id}/skills/${item.skill_id}${item.revision_id ? `/revisions/${item.revision_id}` : ""}`
		: null;
	const version = item.revision_number ? ` · v${item.revision_number}` : "";
	return (
		<li>
			<div>
				<p className="muted">
					{actor} ·{" "}
					{t(activityActionKeys[item.action] || "activity.contentUpdated")}
				</p>
				<div className="actions wrap">
					{item.tag_id ? (
						<button
							type="button"
							className="secondary chip"
							onClick={() =>
								go(`/libraries/${item.library_id}/tags/${item.tag_id}`)
							}
						>
							{item.tag_name || t("activity.tagUnavailable")}
						</button>
					) : (
						item.action.startsWith("tag.") && (
							<span>{item.tag_name || t("activity.tagUnavailable")}</span>
						)
					)}
					{pagePath && (
						<button
							type="button"
							className="secondary"
							onClick={() => go(pagePath)}
						>
							{item.stored_title || item.page_title || t("directory.untitled")}
							{version}
							{item.page_deleted && ` · ${t("app.trash")}`}
						</button>
					)}
					{skillPath && (
						<button
							type="button"
							className="secondary"
							onClick={() => go(skillPath)}
						>
							{item.stored_title || item.skill_name || "Skill"}
							{version}
						</button>
					)}
					{!pagePath && !skillPath && !item.action.startsWith("tag.") && (
						<span>
							{item.stored_title ||
								item.page_title ||
								item.skill_name ||
								t("activity.contentUnavailable")}
						</span>
					)}
				</div>
			</div>
			<span className="muted">
				<LocalTime value={item.occurred_at} />
			</span>
		</li>
	);
}
export function Libraries(props: PageProps) {
	const { t } = usePreferences();
	const data = useResource<List<Structure>>("libraries");
	const [create, setCreate] = useState(false);
	return (
		<>
			<Header
				title={t("app.libraries")}
				description={t("pages.aPlaceForEveryCollection")}
			>
				<button type="button" onClick={() => setCreate(true)}>
					<Plus size={18} />
					{t("pages.newLibrary")}
				</button>
			</Header>
			{data.isPending ? (
				<Loading />
			) : data.error ? (
				<ErrorNotice error={data.error} />
			) : (
				<div className="card-grid">
					{items(data.data).map((library) => (
						<button
							type="button"
							className="library-card"
							key={library.id}
							onClick={() => props.go(`/libraries/${library.id}`)}
						>
							<BookOpen size={26} />
							<h2>{library.name}</h2>
							<p>{library.description}</p>
							<small>
								{library.page_count ?? 0} {t("pages.documents")}
							</small>
							<ChevronRight size={18} />
						</button>
					))}
				</div>
			)}
			<StructureForm
				{...props}
				open={create}
				close={() => setCreate(false)}
				endpoint="libraries"
				title={t("pages.newLibrary")}
			/>
		</>
	);
}
export function StructureForm({
	open,
	close,
	endpoint,
	title,
	initial,
	...props
}: PageProps & {
	open: boolean;
	close: () => void;
	endpoint: string;
	title: string;
	initial?: Structure;
}) {
	const { t } = usePreferences();
	const [source, setSource] = useState(initial?.readme || "");
	const [error, setError] = useState<unknown>();
	const [saving, setSaving] = useState(false);
	return (
		<Modal
			open={open}
			onOpenChange={(value) => !value && close()}
			title={title}
		>
			<form
				onSubmit={async (event) => {
					event.preventDefault();
					setSaving(true);
					setError(undefined);
					const values = Object.fromEntries(new FormData(event.currentTarget));
					try {
						await request(endpoint, {
							method: initial ? "PUT" : "POST",
							body: JSON.stringify({
								...values,
								readme: source,
								expected_updated_at: initial?.updated_at,
							}),
						});
						props.done(t("pages.saved"));
						close();
					} catch (error) {
						setError(error);
					} finally {
						setSaving(false);
					}
				}}
			>
				<label>
					{t("credentials.name")}
					<input name="name" defaultValue={initial?.name} required />
				</label>
				<label>
					{t("pages.shortPurpose")}
					<textarea
						name="description"
						defaultValue={initial?.description || initial?.summary}
						required
					/>
				</label>
				<h3>README</h3>
				<p className="muted">{t("pages.explainHowThisSpaceIsOrganizedAnd")}</p>
				<SourceEditor value={source} onChange={setSource} nonce={props.nonce} />
				{Boolean(error) && <ErrorNotice error={error} />}
				<button type="submit" disabled={saving}>
					{t("credentials.save")}
				</button>
			</form>
		</Modal>
	);
}
export function LibraryOverview(props: PageProps & { libraryId: string }) {
	const { t } = usePreferences();
	const base = `libraries/${props.libraryId}`;
	const data = useResource<{
		library: Structure;
		sections: Structure[];
		tags: { id: string; name: string; page_count: number }[];
		trash_count: number;
		skills?: {
			id: string;
			name: string;
			description: string;
			version: string;
		}[];
	}>(base);
	const skillsData = useResource<{
		items: {
			id: string;
			name: string;
			description: string;
			current_revision_number: number;
		}[];
	}>(`${base}/skills`);
	const [editing, setEditing] = useState(false);
	const [creating, setCreating] = useState(false);
	if (data.isPending) return <Loading />;
	if (data.error || !data.data) return <ErrorNotice error={data.error} />;
	const { library, sections } = data.data;
	const skills = items(skillsData.data);
	return (
		<>
			<Header title={library.name} description={library.description}>
				<button
					type="button"
					className="secondary"
					onClick={() => props.go(`/trash?library_id=${props.libraryId}`)}
				>
					{t("app.trash")} · {data.data.trash_count}
				</button>
				<button
					type="button"
					className="secondary"
					onClick={() => props.go(`/${base}/search`)}
				>
					<Search size={17} />
					{t("pages.searchLibrary")}
				</button>
				<button
					type="button"
					className="secondary"
					onClick={() => setEditing(true)}
				>
					{t("pages.editLibrary")}
				</button>
			</Header>
			<div className="overview-grid">
				<Panel title="README">
					<div className="readme-summary">
						<Markdown
							source={library.readme || t("pages.noUsageInstructionsYet")}
							guide
						/>
					</div>
				</Panel>
				<Panel
					title="Skills"
					actions={
						<button
							type="button"
							className="secondary"
							onClick={() => props.go(`/${base}/skills/new`)}
						>
							<Plus size={16} />
							{t("credentials.create")}
						</button>
					}
				>
					<button
						type="button"
						className="secondary"
						onClick={() => props.go(`/${base}/skills`)}
					>
						{t("skills.allSkills")}
					</button>
					{skillsData.error ? (
						<ErrorNotice error={skillsData.error} />
					) : skills.length ? (
						skills.map((skill) => (
							<button
								type="button"
								key={skill.id}
								className="list-row"
								onClick={() => props.go(`/${base}/skills/${skill.id}`)}
							>
								<div>
									<strong>{skill.name}</strong>
									<p className="muted">{skill.description}</p>
								</div>
								<small>v{skill.current_revision_number}</small>
							</button>
						))
					) : (
						<Empty>{t("pages.addWorkflowsAndRulePackagesForThis")}</Empty>
					)}
				</Panel>
			</div>
			<div className="tag-options">
				{data.data.tags?.map((tag) => (
					<button
						type="button"
						className="chip secondary"
						key={tag.id}
						onClick={() =>
							props.go(`/libraries/${props.libraryId}/tags/${tag.id}`)
						}
					>
						{tag.name} · {tag.page_count}
					</button>
				))}
			</div>
			<Panel
				title={t("pages.sections")}
				actions={
					<button type="button" onClick={() => setCreating(true)}>
						<Plus size={16} />
						{t("pages.newSection")}
					</button>
				}
			>
				<div className="card-grid">
					{sections.map((section) => (
						<button
							type="button"
							className="library-card"
							key={section.id}
							onClick={() => props.go(`/${base}/sections/${section.id}`)}
						>
							<Folder />
							<h3>{section.name}</h3>
							<p>{section.description}</p>
						</button>
					))}
				</div>
				{!sections.length && (
					<Empty>{t("pages.noSectionsYetCreateOneToStart")}</Empty>
				)}
			</Panel>
			<StructureForm
				{...props}
				open={editing}
				close={() => setEditing(false)}
				endpoint={base}
				initial={library}
				title={t("pages.editLibrary2")}
			/>
			<StructureForm
				{...props}
				open={creating}
				close={() => setCreating(false)}
				endpoint={`${base}/sections`}
				title={t("pages.createSection")}
			/>
		</>
	);
}
export function SectionWorkspace(
	props: PageProps & { libraryId: string; sectionId: string; bookId?: string },
) {
	const { t } = usePreferences();
	const base = `libraries/${props.libraryId}/sections/${props.sectionId}`;
	const [cursor, setCursor] = useState("");
	const [editBook, setEditBook] = useState(false);
	useEffect(() => {
		setCursor("");
		setEditBook(false);
	}, [props.bookId]);
	const data = useResource<{
		library: Structure;
		section: Structure;
		books: Structure[];
	}>(base);
	const book = useResource<{
		book: Structure;
		pages: { id: string; title: string; occurred_at: number }[];
		next_cursor?: string | null;
	}>(
		props.bookId
			? `${base}/books/${props.bookId}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ""}`
			: `${base}/books`,
		Boolean(props.bookId),
	);
	const [create, setCreate] = useState(false);
	const [edit, setEdit] = useState(false);
	if (data.isPending) return <Loading />;
	if (data.error || !data.data) return <ErrorNotice error={data.error} />;
	return (
		<>
			<div className="breadcrumb">
				<button
					type="button"
					onClick={() => props.go(`/libraries/${props.libraryId}`)}
				>
					{data.data.library.name}
				</button>
				<ChevronRight size={15} />
				<span>{data.data.section.name}</span>
			</div>
			<Header
				title={data.data.section.name}
				description={data.data.section.description}
			>
				<button
					type="button"
					className="secondary"
					onClick={() => setEdit(true)}
				>
					{t("pages.sectionGuide")}
				</button>
				<button type="button" onClick={() => setCreate(true)}>
					<Plus size={16} />
					{t("pages.newBook")}
				</button>
			</Header>
			<label className="mobile-menu">
				{t("directory.contents")}
				<select
					value={props.bookId || ""}
					onChange={(event) =>
						props.go(
							`/${base}${event.target.value ? `/books/${event.target.value}` : ""}`,
						)
					}
				>
					<option value="">{t("pages.sectionGuide")}</option>
					{data.data.books.map((book) => (
						<option value={book.id} key={book.id}>
							{book.name}
						</option>
					))}
				</select>
			</label>
			<div className="workspace">
				<aside className="directory">
					<h3>{t("directory.contents")}</h3>
					{data.data.books.map((item) => (
						<button
							type="button"
							key={item.id}
							className={
								props.bookId === item.id ? "tree-row active" : "tree-row"
							}
							onClick={() => props.go(`/${base}/books/${item.id}`)}
						>
							<BookOpen size={17} />
							{item.name}
						</button>
					))}
				</aside>
				<div className="reading-pane">
					{props.bookId ? (
						<Panel
							title={book.data?.book?.name || t("pages.book")}
							actions={
								<button
									type="button"
									onClick={() =>
										props.go(
											`/libraries/${props.libraryId}/pages/new?section=${props.sectionId}&book=${props.bookId}`,
										)
									}
								>
									<Plus size={16} />
									{t("directory.newDocument")}
								</button>
							}
						>
							{book.isPending ? (
								<Loading />
							) : book.error ? (
								<ErrorNotice error={book.error} />
							) : (
								book.data?.pages?.map((page) => (
									<button
										type="button"
										className="list-row"
										key={page.id}
										onClick={() =>
											props.go(`/libraries/${props.libraryId}/pages/${page.id}`)
										}
									>
										<FileText size={18} />
										<strong>{page.title}</strong>
										<span className="muted">
											<LocalTime value={page.occurred_at} />
										</span>
									</button>
								))
							)}
							{book.data?.book && (
								<>
									<p className="muted">{book.data.book.summary}</p>
									<details>
										<summary>README</summary>
										<Markdown source={book.data.book.readme} guide />
									</details>
									<button
										type="button"
										className="secondary"
										onClick={() => setEditBook(true)}
									>
										{t("pages.editBook")}
									</button>
								</>
							)}
							{book.data?.next_cursor && (
								<button
									type="button"
									className="secondary"
									onClick={() => setCursor(book.data?.next_cursor || "")}
								>
									{t("pages.nextPage")}
								</button>
							)}
						</Panel>
					) : (
						<Panel title={t("pages.sectionGuide")}>
							<Markdown source={data.data.section.readme} guide />
							{!data.data.books.length && (
								<Empty>{t("pages.createABookToStartSavingDocuments")}</Empty>
							)}
						</Panel>
					)}
				</div>
			</div>
			<StructureForm
				{...props}
				open={create}
				close={() => setCreate(false)}
				endpoint={`${base}/books`}
				title={t("pages.newBook")}
			/>
			<StructureForm
				{...props}
				open={edit}
				close={() => setEdit(false)}
				endpoint={base}
				initial={data.data.section}
				title={t("pages.editSection")}
			/>
			{book.data?.book && (
				<StructureForm
					key={book.data.book.id}
					{...props}
					open={editBook}
					close={() => setEditBook(false)}
					endpoint={`books/${book.data.book.id}`}
					initial={book.data.book}
					title={t("pages.editBook")}
				/>
			)}
		</>
	);
}
export function RequestsPage(_props: PageProps) {
	const { t } = usePreferences();
	const [filters, setFilters] = useState<Record<string, string>>({});
	const [cursor, setCursor] = useState("");
	const query = new URLSearchParams({
		...filters,
		...(cursor ? { before: cursor } : {}),
	});
	const data = useResource<
		List<{
			id: number;
			method: string;
			route_template: string;
			status_code: number;
			occurred_at: number;
			duration_us: number;
		}> & { routes: string[] }
	>(`requests${query.size ? `?${query}` : ""}`);
	return (
		<>
			<Header
				title={t("pages.apiRequests")}
				description={t("pages.necessaryMetadataOnlyTokensContentAndSearch")}
			/>
			<form
				className="filter-bar"
				onSubmit={(event) => {
					event.preventDefault();
					const fields = new FormData(event.currentTarget);
					const next: Record<string, string> = {};
					for (const [name, value] of fields) {
						if (!value) continue;
						next[name] =
							name === "since" || name === "until"
								? new Date(String(value)).toISOString()
								: String(value);
					}
					setFilters(next);
					setCursor("");
				}}
			>
				<label>
					{t("filters.route")}
					<select name="route" defaultValue="">
						<option value="">{t("filters.all")}</option>
						{data.data?.routes.map((route) => (
							<option key={route} value={route}>
								{route}
							</option>
						))}
					</select>
				</label>
				<label>
					{t("filters.method")}
					<select name="method">
						<option value="">{t("filters.all")}</option>
						{["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"].map(
							(method) => (
								<option key={method}>{method}</option>
							),
						)}
					</select>
				</label>
				<label>
					{t("filters.status")}
					<input name="status" placeholder="200 / 401 / interrupted" />
				</label>
				<label>
					{t("filters.from")}
					<input type="datetime-local" name="since" />
				</label>
				<label>
					{t("filters.until")}
					<input type="datetime-local" name="until" />
				</label>
				<button type="submit">{t("filters.apply")}</button>
			</form>
			<Panel title={t("pages.recentRequests")}>
				{data.isPending ? (
					<Loading />
				) : data.error ? (
					<ErrorNotice error={data.error} />
				) : (
					items(data.data).map((row) => (
						<div className="list-row request-row" key={row.id}>
							<code>{row.method}</code>
							<span>{row.route_template}</span>
							<span className="chip">{row.status_code}</span>
							<small>
								<LocalTime value={row.occurred_at} />
							</small>
							<small>{Math.round(row.duration_us / 1000)} ms</small>
						</div>
					))
				)}
				{data.data?.next_cursor && (
					<button
						type="button"
						className="secondary"
						onClick={() => setCursor(data.data?.next_cursor || "")}
					>
						{t("pages.nextPage")}
					</button>
				)}
			</Panel>
		</>
	);
}
export function SettingsPage(props: PageProps) {
	const { t } = usePreferences();
	const client = useQueryClient();
	return (
		<>
			<Header title={t("app.settings")} />
			<Panel title={t("app.masterToken")}>
				<p>{t("pages.theMasterTokenManagesThisServiceRotation")}</p>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						const form = event.currentTarget;
						const values = new FormData(form);
						try {
							await request("master-token/rotate", {
								method: "POST",
								body: JSON.stringify({
									current_token: values.get("current_token"),
									new_token: values.get("token"),
									confirmation: values.get("confirmation"),
								}),
							});
							form.reset();
							setSessionCsrf("");
							client.clear();
							client.setQueryData(["session"], { authenticated: false });
							props.done(t("pages.masterTokenUpdatedSignInAgain"));
						} catch (error) {
							props.done(error instanceof Error ? error.message : "Error");
						}
					}}
				>
					<label>
						{t("pages.currentMasterToken")}
						<input
							name="current_token"
							type="password"
							autoComplete="current-password"
							required
						/>
					</label>
					<label>
						{t("pages.newMasterToken")}
						<input
							name="token"
							type="password"
							autoComplete="new-password"
							required
						/>
					</label>
					<label>
						{t("pages.confirm")}
						<input
							name="confirmation"
							type="password"
							autoComplete="new-password"
							required
						/>
					</label>
					<button type="submit">{t("pages.rotateMasterToken")}</button>
				</form>
			</Panel>
		</>
	);
}
export function HelpPage(props: PageProps & { page?: string }) {
	const { t } = usePreferences();
	const source =
		props.page === "agent"
			? t("pages.agentConnectionCreateACredentialWithIndependent")
			: props.page === "mcp"
				? t("pages.mcpStandardHttpAndProtectedSkillsAre")
				: t("pages.usageGuideLibrarySectionBookDocumentReadme");
	return (
		<>
			<Header title={t("pages.connectionAndUsageGuide")} />
			<Panel title={props.page || t("app.guide")}>
				<Markdown source={source} guide />
			</Panel>
		</>
	);
}
