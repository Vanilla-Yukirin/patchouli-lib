import {
	BookOpen,
	ChevronDown,
	ChevronRight,
	FileText,
	Plus,
} from "lucide-react";
import { useState } from "react";
import type { Structure } from "./api";
import type { PageProps } from "./app";
import { useResource } from "./pages";
import { usePreferences } from "./preferences";
import { ErrorNotice, Loading } from "./ui";
export function DirectoryTree(
	props: PageProps & {
		libraryId: string;
		sectionId: string;
		bookId?: string;
		pageId?: string;
	},
) {
	const { t } = usePreferences();
	const data = useResource<{ section: Structure; books: Structure[] }>(
		`sections/${props.sectionId}`,
	);
	return (
		<>
			<h3>{data.data?.section.name || t("directory.contents")}</h3>
			{data.isPending ? (
				<Loading />
			) : data.error ? (
				<ErrorNotice error={data.error} />
			) : (
				data.data?.books.map((book) => (
					<BookNode {...props} key={book.id} book={book} />
				))
			)}
		</>
	);
}
function BookNode(
	props: PageProps & {
		libraryId: string;
		sectionId: string;
		bookId?: string;
		pageId?: string;
		book: Structure;
	},
) {
	const [open, setOpen] = useState(props.book.id === props.bookId);
	const { t } = usePreferences();
	return (
		<div className="book-node">
			<div className="tree-book">
				<button
					type="button"
					className="tree-row"
					onClick={() => setOpen(!open)}
					aria-expanded={open}
				>
					{open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
					<BookOpen size={16} />
					<span>{props.book.name}</span>
				</button>
				<button
					type="button"
					className="icon-button"
					aria-label={t("directory.newDocument")}
					onClick={() =>
						props.go(
							`/libraries/${props.libraryId}/pages/new?section=${props.sectionId}&book=${props.book.id}`,
						)
					}
				>
					<Plus size={15} />
				</button>
			</div>
			{open && <BookPages {...props} />}
		</div>
	);
}
function BookPages(
	props: PageProps & {
		libraryId: string;
		sectionId: string;
		pageId?: string;
		book: Structure;
	},
) {
	const data = useResource<{
		pages: { id: string; title: string }[];
		next_cursor?: string;
	}>(`books/${props.book.id}`);
	const { t } = usePreferences();
	return (
		<div className="tree-pages">
			{data.isPending ? (
				<Loading />
			) : data.error ? (
				<ErrorNotice error={data.error} />
			) : (
				data.data?.pages.map((page) => (
					<button
						type="button"
						key={page.id}
						className={
							page.id === props.pageId ? "tree-row active" : "tree-row"
						}
						onClick={() =>
							props.go(`/libraries/${props.libraryId}/pages/${page.id}`)
						}
					>
						<FileText size={15} />
						<span>{page.title || t("directory.untitled")}</span>
					</button>
				))
			)}
			{data.data?.next_cursor && (
				<button
					type="button"
					className="secondary"
					onClick={() =>
						props.go(
							`/libraries/${props.libraryId}/sections/${props.sectionId}/books/${props.book.id}`,
						)
					}
				>
					{t("directory.viewAll")}
				</button>
			)}
		</div>
	);
}
