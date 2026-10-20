import type { Structure } from "./api";
import type { PageProps } from "./app";
import { useResource } from "./pages";
import { LocalTime, usePreferences } from "./preferences";
import { Empty, ErrorNotice, Loading, Panel } from "./ui";
export function TagDetail(
	props: PageProps & { libraryId: string; tagId: string },
) {
	const { t } = usePreferences();
	const data = useResource<{
		library: Structure;
		tag: { id: string; name: string; page_count: number };
		pages: { id: string; title: string; occurred_at: number }[];
	}>(`libraries/${props.libraryId}/tags/${props.tagId}`);
	if (data.isPending) return <Loading />;
	if (data.error || !data.data) return <ErrorNotice error={data.error} />;
	return (
		<>
			<div className="breadcrumb">
				<button type="button" onClick={() => props.go("/tags")}>
					{t("app.tags")}
				</button>
				<span>/</span>
				{data.data.library.name}
			</div>
			<Panel title={`${data.data.tag.name} · ${data.data.tag.page_count}`}>
				{data.data.pages.length ? (
					data.data.pages.map((page) => (
						<button
							type="button"
							className="list-row"
							key={page.id}
							onClick={() =>
								props.go(`/libraries/${props.libraryId}/pages/${page.id}`)
							}
						>
							<strong>{page.title || t("directory.untitled")}</strong>
							<span className="muted">
								<LocalTime value={page.occurred_at} />
							</span>
						</button>
					))
				) : (
					<Empty>{t("tag-detail.noDocumentsHaveThisTagYet")}</Empty>
				)}
			</Panel>
		</>
	);
}
