import * as Tabs from "@radix-ui/react-tabs";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useBlocker, useRouterState } from "@tanstack/react-router";
import {
	Download,
	File,
	FileText,
	Folder,
	Pencil,
	Plus,
	Save,
	Trash2,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { type DocumentView, type FileEntry, readText, request } from "./api";
import type { PageProps } from "./app";
import { SourceEditor } from "./async-editor";
import { DirectoryTree } from "./directory";
import { VersionHistory } from "./history";
import { titleFromMarkdown } from "./markdown";
import { PageProperties } from "./page-properties";
import { items, useResource } from "./pages";
import { LocalTime, usePreferences } from "./preferences";
import {
	applyReferenceEdits,
	type ReferenceEdit,
	referencesForRename,
} from "./references";
import { Markdown } from "./rendered-markdown";
import { Empty, ErrorNotice, Loading, Modal, Panel } from "./ui";

type DraftFile = {
	path: string;
	entry?: FileEntry;
	source?: string;
	changed?: boolean;
	upload?: File;
	removed?: boolean;
};
function editableText(file: DraftFile | undefined): boolean {
	if (!file) return false;
	return file.upload
		? file.source !== undefined
		: file.source !== undefined || Boolean(file.entry?.editable);
}
type Props = PageProps & {
	libraryId: string;
	pageId?: string;
	revision?: string;
	editing?: boolean;
};
export function DocumentWorkspace(props: Props) {
	const searchStr = useRouterState({
		select: (state) => state.location.searchStr,
	});
	const targetBookId =
		props.pageId === "new"
			? new URLSearchParams(searchStr).get("book") || undefined
			: undefined;
	return (
		<ResourceWorkspace
			key={`pages:${props.libraryId}:${props.pageId}:${props.revision ?? "current"}:${targetBookId ?? ""}`}
			{...props}
			targetBookId={targetBookId}
			kind="pages"
			resourceId={props.pageId}
		/>
	);
}
export function SkillWorkspace(
	props: PageProps & {
		libraryId: string;
		skillId?: string;
		editing?: boolean;
		revision?: string;
	},
) {
	const { t } = usePreferences();
	const [offset, setOffset] = useState(0);
	useEffect(() => setOffset(0), [props.libraryId]);
	const list = useResource<{
		items: { id: string; name: string; description: string }[];
		next_offset?: number | null;
	}>(`libraries/${props.libraryId}/skills${offset ? `?offset=${offset}` : ""}`);
	if (!props.skillId)
		return (
			<Panel
				title="Skills"
				actions={
					<button
						type="button"
						onClick={() => props.go(`/libraries/${props.libraryId}/skills/new`)}
					>
						<Plus size={16} />
						{t("workspace.createSkill")}
					</button>
				}
			>
				{list.isPending ? (
					<Loading />
				) : list.error ? (
					<ErrorNotice error={list.error} />
				) : (
					items(list.data).map((item) => (
						<button
							type="button"
							className="list-row"
							key={item.id}
							onClick={() =>
								props.go(`/libraries/${props.libraryId}/skills/${item.id}`)
							}
						>
							<div>
								<strong>{item.name}</strong>
								<p>{item.description}</p>
							</div>
						</button>
					))
				)}
				{list.data?.next_offset != null && (
					<button
						type="button"
						className="secondary"
						onClick={() => setOffset(list.data?.next_offset ?? 0)}
					>
						{t("pages.nextPage")}
					</button>
				)}
			</Panel>
		);
	return (
		<ResourceWorkspace
			key={`skills:${props.libraryId}:${props.skillId}:${props.revision ?? "current"}`}
			{...props}
			kind="skills"
			resourceId={props.skillId}
		/>
	);
}
function ResourceWorkspace(
	props: PageProps & {
		libraryId: string;
		resourceId?: string;
		revision?: string;
		editing?: boolean;
		kind: "pages" | "skills";
		targetBookId?: string;
	},
) {
	const { t } = usePreferences();
	const client = useQueryClient();
	const creating = props.resourceId === "new";
	const historical = props.revision !== undefined;
	const base = `libraries/${props.libraryId}/${props.kind}`;
	const resource = `${base}/${props.resourceId}`;
	const writeTarget =
		creating && props.kind === "pages"
			? `books/${props.targetBookId}/pages`
			: creating
				? base
				: `${resource}/files`;
	const data = useQuery({
		queryKey: ["resource", resource, props.revision],
		enabled: !creating && Boolean(props.resourceId),
		queryFn: () =>
			request<DocumentView>(
				resource + (props.revision ? `/revisions/${props.revision}` : ""),
			),
	});
	const [editing, setEdit] = useState(
		!historical && (Boolean(props.editing) || creating),
	);
	const edit = !historical && editing;
	const [files, setFiles] = useState<DraftFile[]>([]);
	const [selected, setSelected] = useState<string>("");
	const [primary, setPrimary] = useState<string | null>(null);
	const [dirty, setDirty] = useState(false);
	const leaveAllowed = useRef(false);
	const [saving, setSaving] = useState(false);
	const [unconfirmed, setUnconfirmed] = useState(false);
	const receiptKey = `patchouli.pending.${resource}:revision:${props.revision ?? "current"}${creating && props.kind === "pages" ? `:book:${props.targetBookId ?? "missing"}` : ""}`;
	const [previousOperation, setPreviousOperation] = useState<{
		key: string;
		signature: string;
		base_etag?: string;
		primary_file: string | null;
		occurred_at?: string | null;
	} | null>(() => {
		if (historical) return null;
		try {
			return JSON.parse(sessionStorage.getItem(receiptKey) || "null");
		} catch {
			return null;
		}
	});
	const pendingOperation = useRef<{ key: string; body: FormData } | null>(null);
	const [error, setError] = useState<unknown>();
	const [suggestions, setSuggestions] = useState<ReferenceEdit[] | null>(null);
	const [rename, setRename] = useState<{ from: string; to: string } | null>(
		null,
	);
	const [savedNotice, setSavedNotice] = useState("");
	const [mobileTree, setMobileTree] = useState(false);
	const [occurrence, setOccurrence] = useState("");
	const [skillName, setSkillName] = useState("");
	const [skillDescription, setSkillDescription] = useState("");
	const fileInput = useRef<HTMLInputElement>(null);
	const replaceInput = useRef<HTMLInputElement>(null);
	const [replacePath, setReplacePath] = useState("");
	const version = data.data?.revision;
	useEffect(() => {
		if (dirty) return;
		if (creating) {
			const path = props.kind === "skills" ? "SKILL.md" : "document.md";
			setFiles([
				{
					path,
					changed: true,
					source:
						props.kind === "skills"
							? "---\nname: new-skill\ndescription: Describe when this Skill should be used.\n---\n\n# New Skill\n"
							: `# ${t("workspace.newDocument")}\n\n`,
				},
			]);
			setSelected(path);
			setPrimary(path);
		} else if (version) {
			if (props.kind === "skills") {
				setSkillName(data.data?.title || "");
				setSkillDescription(data.data?.description || "");
			}
			setFiles(version.files.map((entry) => ({ path: entry.path, entry })));
			setPrimary(version.primary_file);
			setSelected(version.primary_file || "");
		}
	}, [version, creating]);
	useBlocker({
		shouldBlockFn: () =>
			dirty &&
			!leaveAllowed.current &&
			!confirm(t("workspace.leaveWithUnsavedChanges")),
		enableBeforeUnload: () => dirty && !leaveAllowed.current,
	});
	const active = files.find((file) => file.path === selected && !file.removed);
	const text = useQuery({
		queryKey: [
			"file-text",
			active?.entry?.content_href || active?.entry?.download_href,
			selected,
		],
		enabled: Boolean(
			active?.entry &&
				!active.upload &&
				(active.entry.preview_kind === "markdown" ||
					active.entry.preview_kind === "text" ||
					active.entry.editable),
		),
		queryFn: () => {
			if (!active?.entry) throw new Error("file_unavailable");
			return readText(active.entry);
		},
	});
	const source = active?.upload ? active.source : (active?.source ?? text.data);
	const update = (value: string) => {
		if (!edit || !editableText(active)) return;
		setFiles((previous) =>
			previous.map((file) =>
				file.path === selected
					? { ...file, source: value, changed: true }
					: file,
			),
		);
		setDirty(true);
	};
	const choose = async (path: string) => {
		setSelected(path);
		const file = files.find((item) => item.path === path);
		if (
			file?.entry &&
			!file.upload &&
			file.source === undefined &&
			file.entry.editable
		) {
			try {
				const source = await readText(file.entry);
				setFiles((previous) =>
					previous.map((item) =>
						item.path === path &&
						!item.upload &&
						!item.changed &&
						item.source === undefined &&
						item.entry === file.entry
							? { ...item, source }
							: item,
					),
				);
			} catch (error) {
				setError(error);
			}
		}
	};
	const addUploads = async (uploads: FileList | null) => {
		if (!edit || saving || unconfirmed || !uploads) return;
		const added: DraftFile[] = [];
		for (const file of Array.from(uploads)) {
			if (file.size > 16 * 1024 * 1024) {
				setError(new Error("body_too_large"));
				continue;
			}
			const path = file.webkitRelativePath
				? file.webkitRelativePath.split("/").slice(1).join("/")
				: file.name;
			if (files.some((item) => item.path === path && !item.removed)) {
				setError(new Error(t("workspace.aFileWithThisNameExistsUse")));
				continue;
			}
			let source: string | undefined;
			try {
				const bytes = new Uint8Array(await file.arrayBuffer());
				if (!bytes.includes(0))
					source = new TextDecoder("utf-8", {
						fatal: true,
						ignoreBOM: true,
					}).decode(bytes);
			} catch {}
			added.push({ path, upload: file, source, changed: false });
		}
		setFiles((previous) => [...previous, ...added]);
		setDirty(true);
	};
	const save = async () => {
		if (!edit || historical || saving) return;
		setSaving(true);
		setError(undefined);
		try {
			if (creating && props.kind === "pages" && !props.targetBookId)
				throw new Error(t("page-properties.chooseABook"));
			const body = new FormData();
			const kept = files.filter((file) => !file.removed);
			const metadata: { [key: string]: unknown } = {
				primary_file: primary,
				base_etag: previousOperation?.base_etag ?? data.data?.current_etag,
				edits: [],
				remove: files
					.filter((file) => file.removed && file.entry)
					.map((file) => file.entry?.path),
				renames: kept
					.filter((file) => file.entry && file.path !== file.entry.path)
					.map((file) => ({ from: file.entry?.path, to: file.path })),
			};
			if (creating && props.kind === "pages") {
				metadata.occurred_at = occurrence
					? new Date(occurrence).toISOString()
					: null;
			}
			if (props.kind === "skills")
				metadata.name =
					skillName ||
					titleFromMarkdown(
						files.find((file) => file.path === "SKILL.md")?.source || "",
					) ||
					"Skill";
			if (props.kind === "skills") {
				delete metadata.primary_file;
				metadata.description = skillDescription;
			}
			if (previousOperation) {
				metadata.primary_file = previousOperation.primary_file;
				if (previousOperation.occurred_at !== undefined)
					metadata.occurred_at = previousOperation.occurred_at;
				if (props.kind === "skills") delete metadata.primary_file;
			}
			body.append("metadata", JSON.stringify(metadata));
			const uploads: { path: string; body: Blob }[] = [];
			for (const file of kept) {
				if (file.source !== undefined && file.changed) {
					const bytes = new TextEncoder().encode(file.source);
					if (bytes.length > 16 * 1024 * 1024)
						throw new Error("body_too_large");
					if (
						new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(
							bytes,
						) !== file.source
					)
						throw new Error("invalid_text_edit");
					uploads.push({
						path: file.path,
						body: new Blob([bytes], { type: "application/octet-stream" }),
					});
				} else if (file.upload) {
					uploads.push({ path: file.path, body: file.upload });
				}
			}
			for (const file of uploads) body.append("files", file.body, file.path);
			const hash = async (bytes: BufferSource) =>
				Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)))
					.map((byte) => byte.toString(16).padStart(2, "0"))
					.join("");
			const identity = JSON.stringify({
				target: writeTarget,
				metadata,
				uploads: await Promise.all(
					uploads.map(async (file) => [
						file.path,
						await hash(await file.body.arrayBuffer()),
					]),
				),
			});
			const signature = await hash(new TextEncoder().encode(identity));
			if (previousOperation && signature !== previousOperation.signature)
				throw new Error(
					t("workspace.reselectTheOriginalFilesAndContentBefore"),
				);
			if (!pendingOperation.current) {
				pendingOperation.current = {
					key:
						previousOperation?.key ?? crypto.randomUUID().replaceAll("-", ""),
					body,
				};
				try {
					sessionStorage.setItem(
						receiptKey,
						JSON.stringify({
							key: pendingOperation.current.key,
							signature,
							base_etag: metadata.base_etag,
							primary_file: primary,
							...(metadata.occurred_at !== undefined
								? { occurred_at: metadata.occurred_at }
								: {}),
						}),
					);
				} catch {}
			}
			const result = await request<{
				page_id?: string;
				skill?: { id: string };
				id?: string;
				revision_id: string;
				revision_number: number;
				changed: boolean;
				overwritten_newer_revision?: boolean;
			}>(writeTarget, {
				method: creating ? "POST" : "PUT",
				body: pendingOperation.current.body,
				headers: { "Idempotency-Key": pendingOperation.current.key },
			});
			pendingOperation.current = null;
			setPreviousOperation(null);
			try {
				sessionStorage.removeItem(receiptKey);
			} catch {}
			setUnconfirmed(false);
			setDirty(false);
			leaveAllowed.current = true;
			setEdit(false);
			setSavedNotice(
				result.overwritten_newer_revision
					? t("workspace.savedTheInterveningVersionRemainsInHistory")
					: result.changed
						? t("pages.saved")
						: t("workspace.identicalContentNoNewRevision"),
			);
			client.invalidateQueries({ queryKey: ["resource"] });
			if (creating)
				props.go(`/${base}/${result.page_id || result.skill?.id || result.id}`);
		} catch (error) {
			setError(error);
			if (
				error instanceof Error &&
				"status" in error &&
				Number(error.status) !== 401 &&
				Number(error.status) < 500
			) {
				pendingOperation.current = null;
				setPreviousOperation(null);
				try {
					sessionStorage.removeItem(receiptKey);
				} catch {}
				setUnconfirmed(false);
			} else if (pendingOperation.current) setUnconfirmed(true);
		} finally {
			setSaving(false);
		}
	};
	const startRename = async (file: DraftFile) => {
		if (!edit || saving || unconfirmed) return;
		const next = prompt(t("workspace.newFileName"), file.path);
		if (!next || next === file.path) return;
		if (files.some((item) => item.path === next && !item.removed)) {
			setError(new Error("name_conflict"));
			return;
		}
		const sources: Record<string, string> = {};
		const scanned = new Map(files.map((item) => [item.path, item]));
		for (const item of files.filter(
			(file) =>
				!file.removed && /\.(?:md|markdown|mdown|mkd)$/i.test(file.path),
		)) {
			if (item.source !== undefined) sources[item.path] = item.source;
			else if (item.entry && !item.upload)
				try {
					sources[item.path] = await readText(item.entry);
				} catch {}
		}
		setFiles((previous) =>
			previous.map((item) =>
				sources[item.path] !== undefined && item === scanned.get(item.path)
					? { ...item, source: sources[item.path] }
					: item,
			),
		);
		setSuggestions(referencesForRename(sources, file.path, next));
		setRename({ from: file.path, to: next });
	};
	const applyRename = () => {
		if (!edit || saving || unconfirmed || !rename || !suggestions) return;
		try {
			const sources = Object.fromEntries(
				files
					.filter((file) => file.source !== undefined)
					.map((file) => [file.path, file.source ?? ""]),
			);
			const changed = applyReferenceEdits(sources, suggestions);
			setFiles((previous) =>
				previous.map((file) => ({
					...file,
					path: file.path === rename.from ? rename.to : file.path,
					source: changed[file.path] ?? file.source,
					changed:
						file.changed ||
						(changed[file.path] !== undefined &&
							changed[file.path] !== file.source),
				})),
			);
			if (primary === rename.from) setPrimary(rename.to);
			if (selected === rename.from) setSelected(rename.to);
			setDirty(true);
			setSuggestions(null);
			setRename(null);
		} catch (error) {
			setError(error);
		}
	};
	if (!creating && data.isPending) return <Loading />;
	if (!creating && (data.error || !data.data))
		return <ErrorNotice error={data.error} />;
	const primarySource =
		files.find((file) => file.path === primary)?.source ??
		(selected === primary ? source : undefined);
	const heading =
		(edit &&
		primary &&
		/\.(?:md|markdown|mdown|mkd)$/i.test(primary) &&
		primarySource !== undefined
			? titleFromMarkdown(primarySource)
			: version?.title) || t("directory.untitled");
	return (
		<>
			<div className="breadcrumb">
				<button
					type="button"
					onClick={() => props.go(`/libraries/${props.libraryId}`)}
				>
					{t("workspace.libraryOverview")}
				</button>
				<span>/</span>
				<span>
					{props.kind === "skills" ? "Skills" : t("workspace.document")}
				</span>
			</div>
			<div className="page-heading">
				<div>
					<p className="eyebrow">
						{edit ? t("workspace.editor") : t("workspace.reading")}
					</p>
					<h1>
						<span className="title-mark">#</span> {heading}
					</h1>
					<p className="muted">
						{primary || t("workspace.noEntryFile")}
						{historical && ` · v${version?.revision_number ?? ""}`}
						{dirty && ` · ${t("workspace.unsaved")}`}
					</p>
				</div>
				<div className="actions">
					{!creating && props.kind === "skills" && (
						<>
							<a
								className="button secondary"
								href={`/admin/api/${resource}/download${props.revision ? `?revision_id=${props.revision}` : ""}`}
								download
							>
								<Download size={16} />
								{t("workspace.downloadPackage")}
							</a>
							<button
								type="button"
								className="secondary"
								onClick={async () => {
									try {
										await navigator.clipboard.writeText(
											`${location.origin}/api/v1/${resource}${props.revision ? `/revisions/${props.revision}` : ""}/manifest`,
										);
										setSavedNotice(t("workspace.manifestCopied"));
									} catch (error) {
										setError(error);
									}
								}}
							>
								{t("workspace.copyManifestLink")}
							</button>
						</>
					)}
					{!edit && (
						<button
							type="button"
							className="secondary"
							disabled={source === undefined}
							onClick={async () => {
								if (source === undefined) return;
								try {
									await navigator.clipboard.writeText(source);
									setSavedNotice(t("workspace.sourceCopied"));
								} catch (error) {
									setError(error);
								}
							}}
						>
							{t("workspace.copySource")}
						</button>
					)}
					<button
						type="button"
						className="secondary mobile-menu"
						onClick={() => setMobileTree(true)}
					>
						<Folder size={17} />
						{t("workspace.files")}
					</button>
					{edit ? (
						<>
							<button
								type="button"
								disabled={saving || unconfirmed}
								className="secondary"
								onClick={() => {
									if (!dirty || confirm(t("workspace.discardUnsavedChanges"))) {
										setDirty(false);
										leaveAllowed.current = true;
										if (creating) {
											props.go(`/libraries/${props.libraryId}`);
											return;
										}
										setFiles(
											version?.files.map((entry) => ({
												path: entry.path,
												entry,
											})) || [],
										);
										setPrimary(version?.primary_file ?? null);
										setSelected(version?.primary_file || "");
										setSkillName(data.data?.title || "");
										setSkillDescription(data.data?.description || "");
										setSuggestions(null);
										setRename(null);
										setError(undefined);
										setEdit(false);
										data.refetch();
									}
								}}
							>
								{t("page-properties.cancel")}
							</button>
							<button type="button" disabled={saving} onClick={save}>
								<Save size={17} />
								{saving ? t("workspace.saving") : t("credentials.save")}
							</button>
						</>
					) : !historical ? (
						<button
							type="button"
							onClick={() => {
								leaveAllowed.current = false;
								setEdit(true);
							}}
						>
							<Pencil size={17} />
							{t("editor.edit")}
						</button>
					) : (
						<button
							type="button"
							className="secondary"
							onClick={() => props.go(`/${resource}`)}
						>
							{t("activity.latest")}
						</button>
					)}
				</div>
			</div>
			{savedNotice && (
				<p className="success" role="status">
					{savedNotice}
				</p>
			)}
			{Boolean(error) && <ErrorNotice error={error} />}
			{(unconfirmed || previousOperation) && (
				<div className="alert" role="status">
					{unconfirmed
						? t("workspace.saveOutcomeIsUnknownPressSaveTo")
						: t("workspace.aSaveWasPendingBeforeRefreshReselect")}{" "}
					{!unconfirmed && (
						<button
							type="button"
							className="secondary"
							onClick={() => {
								if (
									confirm(t("workspace.haveYouCheckedTheCurrentRevisionAnd"))
								) {
									setPreviousOperation(null);
									try {
										sessionStorage.removeItem(receiptKey);
									} catch {}
								}
							}}
						>
							{t("workspace.checkedClearReminder")}
						</button>
					)}
				</div>
			)}
			{edit && props.kind === "skills" && (
				<label>
					{t("workspace.skillName")}
					<input
						value={skillName}
						disabled={saving || unconfirmed}
						onChange={(event) => {
							setSkillName(event.target.value);
							setDirty(true);
						}}
						required
					/>
				</label>
			)}
			{edit && props.kind === "skills" && (
				<label>
					{t("workspace.purpose")}
					<textarea
						disabled={saving || unconfirmed}
						value={skillDescription}
						onChange={(event) => {
							setSkillDescription(event.target.value);
							setDirty(true);
						}}
						required
					/>
				</label>
			)}
			<fieldset disabled={saving || unconfirmed} className="workspace-controls">
				<Tabs.Root defaultValue="content" className="document-tabs">
					<Tabs.List className="tab-list">
						{[
							["content", t("app.content")],
							["files", t("workspace.files")],
							["history", t("workspace.history")],
							["properties", t("workspace.properties")],
						].map(([value, label]) => (
							<Tabs.Trigger key={value} value={value}>
								{label}
							</Tabs.Trigger>
						))}
					</Tabs.List>
					<Tabs.Content value="content">
						<div className="workspace">
							<aside className="directory">
								{!edit && data.data?.section_id && props.kind === "pages" && (
									<DirectoryTree
										{...props}
										sectionId={data.data.section_id}
										bookId={data.data.book_id}
										pageId={props.resourceId}
									/>
								)}
								<div className="panel-heading">
									<h3>{t("workspace.files")}</h3>
									{edit && (
										<button
											type="button"
											className="icon-button"
											onClick={() => {
												const path = prompt(
													t("workspace.newFileName2"),
													"notes.md",
												);
												if (path && !files.some((file) => file.path === path)) {
													setFiles([
														...files,
														{ path, source: "", changed: true },
													]);
													setSelected(path);
													setDirty(true);
												}
											}}
										>
											<Plus size={17} />
										</button>
									)}
								</div>
								{files
									.filter((file) => !file.removed)
									.map((file) => (
										<button
											type="button"
											key={file.path}
											className={
												selected === file.path ? "tree-row active" : "tree-row"
											}
											onClick={() => choose(file.path)}
										>
											<FileText size={16} />
											<span>{file.path}</span>
											{file.path === primary && <small>#</small>}
										</button>
									))}
							</aside>
							<section className="reading-pane">
								{!primary && !edit && (
									<Empty>
										{t("workspace.thisRevisionHasNoEntryFileSelect")}
									</Empty>
								)}
								{active ? (
									edit && editableText(active) ? (
										<SourceEditor
											key={active.path}
											value={source || ""}
											sourceFile={active.path}
											readOnly={saving || unconfirmed || source === undefined}
											onChange={update}
											nonce={props.nonce}
											preview={/\.(?:md|markdown|mdown|mkd)$/i.test(
												active.path,
											)}
											files={files.flatMap((file) =>
												file.entry &&
												!file.removed &&
												!file.upload &&
												!file.changed
													? [{ ...file.entry, path: file.path }]
													: [],
											)}
										/>
									) : source !== undefined ? (
										/\.(?:md|markdown|mdown|mkd)$/i.test(active.path) ? (
											<Markdown
												source={source}
												files={version?.files}
												sourceFile={active.path}
											/>
										) : (
											<pre>{source}</pre>
										)
									) : text.isFetching ? (
										<Loading />
									) : !active.upload &&
										active.entry?.preview_kind === "image" &&
										active.entry.preview_href ? (
										<img
											className="document-image"
											src={active.entry.preview_href}
											alt={active.path}
										/>
									) : (
										<Empty>
											<p>{t("workspace.thisFileCannotBePreviewed")}</p>
											{active.entry && !active.upload && (
												<a
													className="button"
													href={active.entry.download_href}
													download
												>
													{t("workspace.downloadFile")}
												</a>
											)}
										</Empty>
									)
								) : (
									<Empty>{t("workspace.selectAFileFromTheDirectory")}</Empty>
								)}
							</section>
						</div>
					</Tabs.Content>
					<Tabs.Content value="files">
						{edit && (
							<button
								type="button"
								className="drop-zone"
								onClick={() => fileInput.current?.click()}
								onDragOver={(event) => event.preventDefault()}
								onDrop={(event) => {
									event.preventDefault();
									addUploads(event.dataTransfer.files);
								}}
							>
								{t("workspace.dropFilesHereOrUseAddFiles")}
							</button>
						)}
						<input
							type="file"
							ref={replaceInput}
							className="visually-hidden"
							onChange={async (event) => {
								if (!edit || saving || unconfirmed) return;
								const upload = event.target.files?.[0];
								if (!upload) return;
								if (upload.size > 16 * 1024 * 1024) {
									setError(new Error("body_too_large"));
									return;
								}
								let source: string | undefined;
								try {
									const bytes = new Uint8Array(await upload.arrayBuffer());
									if (!bytes.includes(0))
										source = new TextDecoder("utf-8", {
											fatal: true,
											ignoreBOM: true,
										}).decode(bytes);
								} catch {}
								setFiles((previous) =>
									previous.map((file) =>
										file.path === replacePath
											? { ...file, source, upload, changed: false }
											: file,
									),
								);
								if (
									source === undefined &&
									primary === replacePath &&
									props.kind === "pages"
								)
									setPrimary(null);
								setDirty(true);
								event.target.value = "";
							}}
						/>
						<Panel
							title={t("workspace.revisionFiles")}
							actions={
								edit && (
									<>
										<input
											ref={fileInput}
											type="file"
											multiple
											className="visually-hidden"
											onChange={(event) => addUploads(event.target.files)}
										/>
										<button
											type="button"
											onClick={() => fileInput.current?.click()}
										>
											<Plus size={17} />
											{t("workspace.addFiles")}
										</button>
									</>
								)
							}
						>
							{files
								.filter((file) => !file.removed)
								.map((file) => (
									<div className="file-row" key={file.path}>
										<File size={18} />
										<div>
											<strong>{file.path}</strong>
											<p className="muted">
												{file.path === primary ? t("workspace.entryFile") : ""}
											</p>
										</div>
										<div className="actions">
											{edit && (
												<>
													<button
														type="button"
														disabled={
															props.kind === "skills" || !editableText(file)
														}
														className="secondary"
														onClick={() => {
															if (!editableText(file)) return;
															setPrimary(file.path);
															setDirty(true);
														}}
													>
														{t("workspace.setAsEntry")}
													</button>
													<button
														type="button"
														className="secondary"
														onClick={() => {
															setReplacePath(file.path);
															replaceInput.current?.click();
														}}
													>
														{t("workspace.replace")}
													</button>
													<button
														type="button"
														className="secondary"
														onClick={() => startRename(file)}
													>
														{t("workspace.rename")}
													</button>
													<button
														type="button"
														className="danger"
														onClick={() => {
															setFiles(
																files.map((item) =>
																	item === file
																		? { ...item, removed: true }
																		: item,
																),
															);
															if (primary === file.path) setPrimary(null);
															setDirty(true);
														}}
														aria-label={t("workspace.removeFile")}
													>
														<Trash2 size={16} />
													</button>
												</>
											)}
											{file.entry && (
												<a
													className="button secondary"
													href={file.entry.download_href}
													download
												>
													<Download size={16} />
													{t("workspace.download")}
												</a>
											)}
										</div>
									</div>
								))}
						</Panel>
					</Tabs.Content>
					<Tabs.Content value="history">
						{!creating && <VersionHistory {...props} resource={resource} />}
					</Tabs.Content>
					<Tabs.Content value="properties">
						<Panel title={t("workspace.properties")}>
							{data.data && props.kind === "pages" && !historical && (
								<PageProperties {...props} document={data.data} />
							)}
							{data.data && props.kind === "pages" && historical && (
								<>
									<p>
										{t("page-properties.declaredTime")} ·{" "}
										<LocalTime value={data.data.occurred_at} />
									</p>
									<div className="tag-options">
										{data.data.tag_choices
											?.filter((tag) => tag.attached)
											.map((tag) => (
												<span className="chip" key={tag.id}>
													{tag.name}
												</span>
											))}
									</div>
								</>
							)}
							{data.data && props.kind === "skills" && (
								<p>
									<LocalTime value={data.data.occurred_at} />
								</p>
							)}
							{creating && (
								<label>
									{t("workspace.declaredTimeOptionalLocalTimezone")}
									<input
										type="datetime-local"
										value={occurrence}
										onChange={(event) => {
											setOccurrence(event.target.value);
											setDirty(true);
										}}
									/>
								</label>
							)}
							<details>
								<summary>{t("workspace.technicalDetails")}</summary>
								<pre>
									{JSON.stringify(
										{
											page_id: props.resourceId,
											revision_id: version?.revision_id,
											files: version?.files.map((file) => ({
												path: file.path,
												size: file.size_bytes,
												sha256: file.sha256,
											})),
										},
										null,
										2,
									)}
								</pre>
							</details>
						</Panel>
					</Tabs.Content>
				</Tabs.Root>
			</fieldset>
			<Modal
				open={mobileTree}
				onOpenChange={setMobileTree}
				title={t("workspace.contentsAndFiles")}
				drawer
			>
				{data.data?.section_id && props.kind === "pages" ? (
					<DirectoryTree
						{...props}
						sectionId={data.data.section_id}
						bookId={data.data.book_id}
						pageId={props.resourceId}
					/>
				) : null}
				{files
					.filter((file) => !file.removed)
					.map((file) => (
						<button
							type="button"
							className="tree-row"
							key={file.path}
							onClick={() => {
								choose(file.path);
								setMobileTree(false);
							}}
						>
							{file.path}
						</button>
					))}
			</Modal>
			<Modal
				open={suggestions !== null}
				onOpenChange={(value) => !value && setSuggestions(null)}
				title={t("workspace.updateAttachmentReferences")}
			>
				<p>
					{rename?.from} → {rename?.to}
				</p>
				<p className="muted">
					{t("workspace.onlyRecognizedMarkdownLinksInThisFile")}
				</p>
				{!suggestions?.length && (
					<Empty>{t("workspace.noUnambiguousReferencesFound")}</Empty>
				)}
				{suggestions?.map((item) => (
					<label className="reference-row" key={item.id}>
						<input
							type="checkbox"
							checked={item.selected}
							onChange={(event) =>
								setSuggestions(
									suggestions.map((edit) =>
										edit.id === item.id
											? { ...edit, selected: event.target.checked }
											: edit,
									),
								)
							}
						/>
						<div>
							<strong>
								{item.file}:{item.line}:{item.column}
							</strong>
							<pre>
								<span>
									{item.source.split("\n")[item.line - 1]}
									{"\n"}
								</span>
								<del>{item.before}</del>
								{" → "}
								<ins>{item.after}</ins>
							</pre>
						</div>
					</label>
				))}
				<div className="dialog-actions">
					<button
						type="button"
						className="secondary"
						onClick={() => setSuggestions(null)}
					>
						{t("page-properties.cancel")}
					</button>
					<button type="button" onClick={applyRename}>
						{t("workspace.applyToEditor")}
					</button>
				</div>
			</Modal>
		</>
	);
}
