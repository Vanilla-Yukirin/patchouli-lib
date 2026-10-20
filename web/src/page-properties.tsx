import { useEffect, useRef, useState } from "react";
import { type DocumentView, request, type Structure } from "./api";
import type { PageProps } from "./app";
import type { components } from "./generated/admin";
import { useResource } from "./pages";
import { LocalTime, usePreferences } from "./preferences";
import { ErrorNotice, LocalDatePreview, Modal, Panel } from "./ui";

export function PageProperties(props: PageProps & { document: DocumentView }) {
	const { t } = usePreferences();
	const doc = props.document;
	const [error, setError] = useState<unknown>();
	const [busy, setBusy] = useState(false);
	const [removing, setRemoving] = useState(false);
	const [moving, setMoving] = useState(false);
	const [targetSection, setTargetSection] = useState(doc.section_id || "");
	const [targetBook, setTargetBook] = useState("");
	const [date, setDate] = useState("");
	const active = useRef(false);
	useEffect(() => {
		active.current = true;
		return () => {
			active.current = false;
		};
	}, []);
	const library = useResource<{ sections: Structure[] }>(
		`libraries/${doc.library_id}`,
	);
	const section = useResource<{ books: Structure[] }>(
		`sections/${targetSection || doc.section_id}`,
	);
	const base = `libraries/${doc.library_id}/pages/${doc.page_id}`;
	const change = async (path: string, body: unknown, method = "POST") => {
		setBusy(true);
		setError(undefined);
		try {
			await request(base + path, {
				method,
				body: JSON.stringify(body),
				headers:
					path === "/move"
						? { "Idempotency-Key": crypto.randomUUID().replaceAll("-", "") }
						: {},
			});
			props.done(t("page-properties.updated"));
			return active.current;
		} catch (e) {
			if (active.current) setError(e);
			return false;
		} finally {
			if (active.current) setBusy(false);
		}
	};
	const remove = async () => {
		setBusy(true);
		setError(undefined);
		try {
			const result = await request<components["schemas"]["PageDeleteResult"]>(
				base,
				{
					method: "DELETE",
					body: JSON.stringify({
						expected_etag: doc.current_etag,
						confirm_delete: "yes",
					}),
				},
			);
			props.done(t("page-properties.movedToTrash"), {
				libraryId: doc.library_id,
				pageId: doc.page_id,
				etag: result.restore_etag,
			});
			// Notify the same login even after leaving, but never interrupt a new page.
			if (!active.current) return;
			setRemoving(false);
			props.go(`/libraries/${doc.library_id}`);
		} catch (e) {
			if (active.current) setError(e);
		} finally {
			if (active.current) setBusy(false);
		}
	};
	return (
		<>
			<Panel title={t("page-properties.documentProperties")}>
				<p>
					{t("page-properties.declaredTime")} ·{" "}
					<LocalTime value={doc.occurred_at} />
				</p>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						if (date)
							await change("/occurrence", {
								occurred_at: new Date(date).toISOString(),
								expected_etag: doc.current_etag,
							});
					}}
				>
					<label>
						{t("page-properties.correctDeclaredTimeLocalTimezone")}
						<input
							type="datetime-local"
							value={date}
							onChange={(event) => setDate(event.target.value)}
							required
						/>
					</label>
					<LocalDatePreview value={date} />
					<button type="submit" className="secondary" disabled={busy}>
						{t("page-properties.updateTime")}
					</button>
				</form>
				<h3>{t("app.tags")}</h3>
				<div className="tag-options">
					{doc.tag_choices?.map((tag) => (
						<label className="chip" key={tag.id}>
							<input
								type="checkbox"
								checked={tag.attached}
								disabled={busy}
								onChange={() =>
									change("/tags", {
										tag_id: tag.id,
										operation: tag.attached ? "detach" : "attach",
									})
								}
							/>
							{tag.name}
						</label>
					))}
				</div>
				{!doc.tag_choices?.length && (
					<p className="muted">
						{t("page-properties.createTagsInTagManagementFirst")}
					</p>
				)}
				<div className="actions">
					<button
						type="button"
						className="secondary"
						onClick={() => setMoving(true)}
					>
						{t("page-properties.moveDocument")}
					</button>
					<button
						type="button"
						className="danger"
						onClick={() => setRemoving(true)}
					>
						{t("page-properties.moveToTrash")}
					</button>
				</div>
				{Boolean(error) && <ErrorNotice error={error} />}
			</Panel>
			<Modal
				open={moving}
				onOpenChange={setMoving}
				title={t("page-properties.moveDocument")}
			>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						if (
							await change("/move", {
								source_section_id: doc.section_id,
								source_book_id: doc.book_id,
								target_section_id: targetSection,
								target_book_id: targetBook,
								expected_etag: doc.current_etag,
							})
						)
							setMoving(false);
					}}
				>
					<label>
						{t("page-properties.targetSection")}
						<select
							value={targetSection}
							onChange={(event) => {
								setTargetSection(event.target.value);
								setTargetBook("");
							}}
							required
						>
							{library.data?.sections.map((item) => (
								<option key={item.id} value={item.id}>
									{item.name}
								</option>
							))}
						</select>
					</label>
					<label>
						{t("page-properties.targetBook")}
						<select
							value={targetBook}
							onChange={(event) => setTargetBook(event.target.value)}
							required
						>
							<option value="">{t("page-properties.chooseABook")}</option>
							{section.data?.books.map((item) => (
								<option value={item.id} key={item.id}>
									{item.name}
								</option>
							))}
						</select>
					</label>
					<p className="muted">
						{t("page-properties.theDocumentAddressFilesAndHistoryRemain")}
					</p>
					{Boolean(error) && <ErrorNotice error={error} />}
					<button type="submit" disabled={busy || !targetBook}>
						{t("page-properties.move")}
					</button>
				</form>
			</Modal>
			<Modal
				open={removing}
				onOpenChange={setRemoving}
				title={t("page-properties.moveToTrash2")}
			>
				<p>{t("page-properties.allFilesAndRevisionHistoryAreKept")}</p>
				<div className="dialog-actions">
					<button
						type="button"
						className="secondary"
						onClick={() => setRemoving(false)}
					>
						{t("page-properties.cancel")}
					</button>
					<button
						type="button"
						className="danger"
						disabled={busy}
						onClick={remove}
					>
						{t("page-properties.moveToTrash")}
					</button>
				</div>
				{Boolean(error) && <ErrorNotice error={error} />}
			</Modal>
		</>
	);
}
