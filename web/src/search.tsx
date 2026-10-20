import { useQuery } from "@tanstack/react-query";
import { useRouterState } from "@tanstack/react-router";
import { Search } from "lucide-react";
import { useEffect, useState } from "react";
import { request, type Structure } from "./api";
import type { PageProps } from "./app";
import { items, useResource } from "./pages";
import { usePreferences } from "./preferences";
import { Empty, ErrorNotice, Loading } from "./ui";

type SearchInput = {
	keywords: string[];
	libraries?: string[];
	tags_any?: { library_id: string; tag_id: string }[];
	occurred_from_us?: number;
	occurred_before_us?: number;
};
type Tag = {
	id: string;
	name: string;
	library_id: string;
	library_name: string;
};
const identity = (tag: Tag) => `${tag.library_id}:${tag.id}`;

export function SearchWorkspace(props: PageProps & { libraryId?: string }) {
	const { t } = usePreferences();
	const search = useRouterState({
		select: (state) => state.location.searchStr,
	});
	const initialValue = new URLSearchParams(search).get("q") || "";
	const initialText = initialValue.trim() ? initialValue : "";
	const [text, setText] = useState(initialText);
	const [submitted, setSubmitted] = useState<SearchInput | null>(
		initialText ? { keywords: initialText.split(/\s+/).filter(Boolean) } : null,
	);
	const [advanced, setAdvanced] = useState(false);
	const [selectedLibraries, setSelectedLibraries] = useState<string[]>([]);
	const [selectedTags, setSelectedTags] = useState<string[]>([]);
	const [from, setFrom] = useState("");
	const [before, setBefore] = useState("");
	const [cursor, setCursor] = useState("");
	const [error, setError] = useState<unknown>();
	const libraries = useResource<{ items: Structure[] }>("libraries", advanced);
	const tags = useResource<{ items: Tag[] }>("tags", advanced);
	useEffect(() => {
		const value = new URLSearchParams(search).get("q");
		if (value?.trim()) {
			setText(value);
			setSubmitted({ keywords: value.split(/\s+/).filter(Boolean) });
		} else {
			setText("");
			setSubmitted(null);
		}
		setCursor("");
	}, [search]);
	useEffect(() => {
		setSelectedLibraries([]);
		setSelectedTags([]);
		setCursor("");
	}, [props.libraryId]);
	// A library entry point always pins the request scope, including before effects run.
	const query = submitted
		? {
				...submitted,
				...(props.libraryId
					? {
							libraries: [props.libraryId],
							tags_any: submitted.tags_any?.filter(
								(tag) => tag.library_id === props.libraryId,
							),
						}
					: {}),
			}
		: null;
	const data = useQuery({
		queryKey: ["search", props.libraryId, query, cursor],
		enabled: query !== null,
		queryFn: () =>
			request<{
				next_cursor?: string | null;
				items: {
					library_id: string;
					page_id: string;
					title: string;
					snippet?: { text: string };
				}[];
			}>("search", {
				method: "POST",
				body: JSON.stringify({ ...query, ...(cursor ? { cursor } : {}) }),
			}),
	});
	const tagChoices = items(tags.data).filter((tag) =>
		props.libraryId
			? tag.library_id === props.libraryId
			: !selectedLibraries.length || selectedLibraries.includes(tag.library_id),
	);
	return (
		<>
			<div className="page-heading">
				<div>
					<h1>
						{t(
							props.libraryId
								? "pages.searchThisLibrary"
								: "pages.searchAllLibraries",
						)}
					</h1>
					<p className="muted">
						{t(
							props.libraryId
								? "pages.limitedToThisLibraryResultsNeverAutomatically"
								: "pages.findDocumentsAcrossYourLibraries",
						)}
					</p>
				</div>
			</div>
			<form
				onSubmit={(event) => {
					event.preventDefault();
					setError(undefined);
					const keywords = text.split(/\s+/).filter(Boolean);
					const tags_any = tagChoices
						.filter((tag) => selectedTags.includes(identity(tag)))
						.map((tag) => ({ library_id: tag.library_id, tag_id: tag.id }));
					const fromUs = from ? new Date(from).getTime() * 1000 : undefined;
					const beforeUs = before
						? new Date(before).getTime() * 1000
						: undefined;
					if (!keywords.length && !tags_any.length && !from && !before) {
						setError(new Error(t("search.conditionRequired")));
						return;
					}
					if (
						(fromUs !== undefined && !Number.isFinite(fromUs)) ||
						(beforeUs !== undefined && !Number.isFinite(beforeUs)) ||
						(fromUs !== undefined &&
							beforeUs !== undefined &&
							fromUs >= beforeUs)
					) {
						setError(new Error(t("search.invalidTimeWindow")));
						return;
					}
					setSubmitted({
						keywords,
						...(props.libraryId
							? { libraries: [props.libraryId] }
							: selectedLibraries.length
								? { libraries: [...selectedLibraries] }
								: {}),
						...(tags_any.length ? { tags_any } : {}),
						...(fromUs !== undefined ? { occurred_from_us: fromUs } : {}),
						...(beforeUs !== undefined ? { occurred_before_us: beforeUs } : {}),
					});
					setCursor("");
				}}
			>
				<div className="search-box">
					<Search />
					<input
						aria-label={t("pages.enterKeywords")}
						autoFocus
						value={text}
						onChange={(event) => setText(event.target.value)}
						placeholder={t("pages.enterKeywords")}
					/>
					<button type="submit">{t("app.search")}</button>
				</div>
				<details
					className="advanced-search"
					open={advanced}
					onToggle={(event) => setAdvanced(event.currentTarget.open)}
				>
					<summary>{t("search.advancedFilters")}</summary>
					{!props.libraryId && (
						<fieldset className="scope-editor">
							<legend>{t("search.libraryScope")}</legend>
							<p className="muted">{t("search.noLibrarySelectionMeansAll")}</p>
							<div className="tag-options">
								{items(libraries.data).map((library) => (
									<label className="chip" key={library.id}>
										<input
											type="checkbox"
											checked={selectedLibraries.includes(library.id)}
											onChange={(event) => {
												const next = event.target.checked
													? [...selectedLibraries, library.id]
													: selectedLibraries.filter((id) => id !== library.id);
												setSelectedLibraries(next);
												if (next.length)
													setSelectedTags((previous) =>
														previous.filter((id) =>
															items(tags.data).some(
																(tag) =>
																	identity(tag) === id &&
																	next.includes(tag.library_id),
															),
														),
													);
											}}
										/>
										{library.name}
									</label>
								))}
							</div>
							{libraries.error && <ErrorNotice error={libraries.error} />}
						</fieldset>
					)}
					<fieldset className="scope-editor">
						<legend>{t("search.tagsAny")}</legend>
						<div className="tag-options">
							{tagChoices.map((tag) => (
								<label className="chip" key={identity(tag)}>
									<input
										type="checkbox"
										checked={selectedTags.includes(identity(tag))}
										onChange={(event) =>
											setSelectedTags(
												event.target.checked
													? [...selectedTags, identity(tag)]
													: selectedTags.filter((id) => id !== identity(tag)),
											)
										}
									/>
									{tag.name}
									{!props.libraryId && <small> · {tag.library_name}</small>}
								</label>
							))}
						</div>
						{tags.isFetching ? (
							<Loading />
						) : tags.error ? (
							<ErrorNotice error={tags.error} />
						) : (
							!tagChoices.length && (
								<p className="muted">{t("tags.noTagsYet")}</p>
							)
						)}
					</fieldset>
					<div className="filter-bar">
						<label>
							{t("filters.from")}
							<input
								type="datetime-local"
								value={from}
								onChange={(event) => setFrom(event.target.value)}
							/>
						</label>
						<label>
							{t("filters.until")}
							<input
								type="datetime-local"
								value={before}
								onChange={(event) => setBefore(event.target.value)}
							/>
						</label>
					</div>
					<p className="muted">{t("search.timeWindowHint")}</p>
					<button type="submit" className="secondary">
						{t("filters.apply")}
					</button>
				</details>
			</form>
			{Boolean(error) && <ErrorNotice error={error} />}
			{data.isFetching ? (
				<Loading />
			) : data.error ? (
				<ErrorNotice error={data.error} />
			) : data.data?.items.length ? (
				data.data.items.map((page) => (
					<button
						type="button"
						className="search-result"
						key={`${page.library_id}:${page.page_id}`}
						onClick={() =>
							props.go(`/libraries/${page.library_id}/pages/${page.page_id}`)
						}
					>
						<h3>{page.title || t("directory.untitled")}</h3>
						<p>{page.snippet?.text}</p>
					</button>
				))
			) : (
				submitted && <Empty>{t("pages.noResultsFound")}</Empty>
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
		</>
	);
}
