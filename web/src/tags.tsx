import { useState } from "react";
import { request, type Structure } from "./api";
import type { PageProps } from "./app";
import { items, useResource } from "./pages";
import { usePreferences } from "./preferences";
import { Empty, ErrorNotice, Loading, Modal, Panel } from "./ui";

type Tag = {
	id: string;
	name: string;
	library_id: string;
	library_name: string;
	page_count: number;
};
const key = (tag: Tag) => `${tag.library_id}:${tag.id}`;
export function TagsManager(props: PageProps) {
	const { t } = usePreferences();
	const data = useResource<{ items: Tag[] }>("tags");
	const libraries = useResource<{ items: Structure[] }>("libraries");
	const [selected, setSelected] = useState<string[]>([]);
	const [names, setNames] = useState<Record<string, string>>({});
	const [search, setSearch] = useState("");
	const [library, setLibrary] = useState("");
	const [create, setCreate] = useState(false);
	const [deleting, setDeleting] = useState(false);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<unknown>();
	const rows = items(data.data).filter(
		(tag) =>
			(!library || tag.library_id === library) &&
			tag.name.toLocaleLowerCase().includes(search.toLocaleLowerCase()),
	);
	const batch = async (operation: "rename" | "delete") => {
		setBusy(true);
		setError(undefined);
		try {
			await request("tags/batch", {
				method: "POST",
				body: JSON.stringify({
					operation,
					confirm_delete: operation === "delete",
					changes: items(data.data)
						.filter((tag) => selected.includes(key(tag)))
						.map((tag) => ({
							library_id: tag.library_id,
							tag_id: tag.id,
							expected_name: tag.name,
							...(operation === "rename"
								? { new_name: names[key(tag)] ?? tag.name }
								: {}),
						})),
				}),
			});
			setSelected([]);
			setDeleting(false);
			props.done(t("tags.tagsUpdated"));
		} catch (error) {
			setError(error);
		} finally {
			setBusy(false);
		}
	};
	return (
		<>
			<div className="page-heading">
				<div>
					<h1>{t("app.tags")}</h1>
					<p className="muted">
						{t("tags.discoverContentByTagOrOrganizeNames")}
					</p>
				</div>
				<div className="actions">
					<button
						type="button"
						className="secondary"
						disabled={!selected.length || busy}
						onClick={() => batch("rename")}
					>
						{t("tags.saveSelectedNames")}
					</button>
					<button
						type="button"
						className="danger"
						disabled={!selected.length || busy}
						onClick={() => setDeleting(true)}
					>
						{t("tags.deleteSelected")}
					</button>
					<button type="button" onClick={() => setCreate(true)}>
						{t("tags.newTag")}
					</button>
				</div>
			</div>
			<div className="filter-bar">
				<label>
					{t("tags.findTags")}
					<input
						value={search}
						onChange={(event) => setSearch(event.target.value)}
					/>
				</label>
				<label>
					{t("tags.library")}
					<select
						value={library}
						onChange={(event) => setLibrary(event.target.value)}
					>
						<option value="">{t("tags.allLibraries")}</option>
						{items(libraries.data).map((item) => (
							<option key={item.id} value={item.id}>
								{item.name}
							</option>
						))}
					</select>
				</label>
			</div>
			{Boolean(error) && <ErrorNotice error={error} />}
			<Panel
				title={`${t("app.tags")} · ${rows.length}`}
				actions={
					<button
						type="button"
						className="secondary"
						onClick={() =>
							setSelected(
								rows.every((tag) => selected.includes(key(tag)))
									? []
									: rows.map(key),
							)
						}
					>
						{t("tags.selectCurrentResults")}
					</button>
				}
			>
				{data.isPending ? (
					<Loading />
				) : data.error ? (
					<ErrorNotice error={data.error} />
				) : rows.length ? (
					rows.map((tag) => (
						<div className="tag-row" key={key(tag)}>
							<input
								type="checkbox"
								aria-label={tag.name}
								checked={selected.includes(key(tag))}
								onChange={(event) =>
									setSelected(
										event.target.checked
											? [...selected, key(tag)]
											: selected.filter((id) => id !== key(tag)),
									)
								}
							/>
							<button
								type="button"
								className="chip secondary"
								onClick={() =>
									props.go(`/libraries/${tag.library_id}/tags/${tag.id}`)
								}
							>
								{tag.name}
							</button>
							<small>
								{tag.library_name} · {tag.page_count} {t("pages.documents")}
							</small>
							<input
								aria-label={t("tags.newName")}
								value={names[key(tag)] ?? tag.name}
								onChange={(event) =>
									setNames({ ...names, [key(tag)]: event.target.value })
								}
							/>
						</div>
					))
				) : (
					<Empty>{t("tags.noTagsYet")}</Empty>
				)}
			</Panel>
			<Modal open={create} onOpenChange={setCreate} title={t("tags.newTag")}>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						const form = event.currentTarget,
							values = new FormData(form);
						setBusy(true);
						try {
							await request(`libraries/${values.get("library")}/tags`, {
								method: "POST",
								body: JSON.stringify({ name: values.get("name") }),
							});
							setCreate(false);
							props.done(t("tags.tagCreated"));
						} catch (error) {
							setError(error);
						} finally {
							setBusy(false);
						}
					}}
				>
					<label>
						{t("tags.library")}
						<select name="library" defaultValue={library} required>
							<option value="">{t("tags.chooseALibrary")}</option>
							{items(libraries.data).map((item) => (
								<option key={item.id} value={item.id}>
									{item.name}
								</option>
							))}
						</select>
					</label>
					<label>
						{t("credentials.name")}
						<input name="name" required />
					</label>
					{Boolean(error) && <ErrorNotice error={error} />}
					<button type="submit" disabled={busy}>
						{t("credentials.create")}
					</button>
				</form>
			</Modal>
			<Modal
				open={deleting}
				onOpenChange={setDeleting}
				title={t("tags.deleteSelectedTags")}
			>
				<p>
					{t("tags.deleteTagsDocumentsFilesAndHistoryAre", {
						p0: selected.length,
					})}
				</p>
				<button
					type="button"
					className="danger"
					disabled={busy}
					onClick={() => batch("delete")}
				>
					{t("tags.delete")}
				</button>
				{Boolean(error) && <ErrorNotice error={error} />}
			</Modal>
		</>
	);
}
