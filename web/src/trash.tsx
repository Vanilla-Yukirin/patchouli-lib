import { useState } from "react";
import { request, type Structure } from "./api";
import type { PageProps } from "./app";
import { items, useResource } from "./pages";
import { LocalTime, usePreferences } from "./preferences";
import { Empty, ErrorNotice, Loading, Panel } from "./ui";
export function TrashManager(props: PageProps) {
	const { t } = usePreferences();
	const libraries = useResource<{ items: Structure[] }>("libraries");
	const [library, setLibrary] = useState(
		new URLSearchParams(location.search).get("library_id") || "",
	);
	const [cursor, setCursor] = useState("");
	const data = useResource<{
		pages: {
			id: string;
			title: string;
			deleted_at: number;
			section_name: string;
			book_name: string;
			restore_etag?: string;
		}[];
		next_cursor?: string;
	}>(
		"trash" +
			(library
				? "?library_id=" +
					library +
					(cursor ? `&before=${encodeURIComponent(cursor)}` : "")
				: ""),
	);
	const [error, setError] = useState<unknown>();
	return (
		<>
			<div className="page-heading">
				<div>
					<h1>{t("app.trash")}</h1>
					<p className="muted">
						{t("trash.allRevisionsAndFilesAreRetainedPermanent")}
					</p>
				</div>
				<select
					value={library}
					onChange={(event) => {
						setLibrary(event.target.value);
						setCursor("");
					}}
					aria-label={t("tags.library")}
				>
					<option value="">{t("tags.chooseALibrary")}</option>
					{items(libraries.data).map((item) => (
						<option key={item.id} value={item.id}>
							{item.name}
						</option>
					))}
				</select>
			</div>
			{Boolean(error) && <ErrorNotice error={error} />}
			<Panel title={t("trash.deletedDocuments")}>
				{!library ? (
					<Empty>{t("trash.chooseALibraryToBrowseTrash")}</Empty>
				) : data.isPending ? (
					<Loading />
				) : data.error ? (
					<ErrorNotice error={data.error} />
				) : data.data?.pages.length ? (
					data.data.pages.map((page) => (
						<div className="list-row" key={page.id}>
							<div>
								<strong>{page.title}</strong>
								<p className="muted">
									{page.section_name} / {page.book_name} ·{" "}
									<LocalTime value={page.deleted_at} />
								</p>
							</div>
							<button
								type="button"
								onClick={async () => {
									try {
										await request(
											`libraries/${library}/pages/${page.id}/restore`,
											{
												method: "POST",
												body: JSON.stringify({
													expected_etag: page.restore_etag,
												}),
											},
										);
										props.done(t("trash.restoredWithAllHistory"));
									} catch (error) {
										setError(error);
									}
								}}
							>
								{t("history.restore")}
							</button>
						</div>
					))
				) : (
					<Empty>{t("trash.trashIsEmpty")}</Empty>
				)}
				{data.data?.next_cursor && (
					<button
						type="button"
						className="secondary"
						onClick={() => setCursor(data.data?.next_cursor || "")}
					>
						{t("trash.loadMore")}
					</button>
				)}
			</Panel>
		</>
	);
}
