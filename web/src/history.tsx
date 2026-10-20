import { diffLines } from "diff";
import { useRef, useState } from "react";
import { type DocumentView, readText, request } from "./api";
import type { PageProps } from "./app";
import { useResource } from "./pages";
import { LocalTime, usePreferences } from "./preferences";
import { Empty, ErrorNotice, Loading, Modal, Panel } from "./ui";

type Version = {
	revision_id: string;
	revision_number: number;
	number?: number;
	created_at: number;
	stored_title?: string | null;
	name?: string;
	primary_file?: string | null;
};
type RestoreOperation = { path: string; body: string; key: string };
type RestoreReceipt = RestoreOperation & { version: Version };
function readRestoreReceipt(
	storageKey: string,
	resource: string,
	kind: "pages" | "skills",
): RestoreReceipt | null {
	try {
		const receipt = JSON.parse(
			sessionStorage.getItem(storageKey) || "null",
		) as RestoreReceipt | null;
		if (
			!receipt?.version ||
			typeof receipt.key !== "string" ||
			!/^[a-f0-9]{32}$/.test(receipt.key) ||
			typeof receipt.body !== "string"
		)
			return null;
		const target =
			kind === "skills"
				? receipt.version.revision_id
				: receipt.version.revision_number;
		if (!target || receipt.path !== `${resource}/revisions/${target}/restore`)
			return null;
		const body = JSON.parse(receipt.body);
		if (
			Object.keys(body).length !== 2 ||
			body.confirm_restore !== "yes" ||
			typeof body.expected_etag !== "string"
		)
			return null;
		return receipt;
	} catch {
		return null;
	}
}
export function VersionHistory(
	props: PageProps & { resource: string; kind: "pages" | "skills" },
) {
	const { t } = usePreferences();
	const [cursor, setCursor] = useState<number | string>("");
	const [error, setError] = useState<unknown>();
	const storageKey = `patchouli.pending.restore.${props.resource}`;
	const [initialReceipt] = useState(() =>
		readRestoreReceipt(storageKey, props.resource, props.kind),
	);
	const [restore, setRestore] = useState<Version | null>(
		initialReceipt?.version ?? null,
	);
	const [restoring, setRestoring] = useState(false);
	const [restoreUnknown, setRestoreUnknown] = useState(initialReceipt !== null);
	const restoreOperation = useRef<RestoreOperation | null>(initialReceipt);
	const clearReceipt = () => {
		try {
			sessionStorage.removeItem(storageKey);
		} catch {}
	};
	const [comparison, setComparison] = useState<{
		old: string;
		current: string;
	} | null>(null);
	const [comparing, setComparing] = useState(false);
	const data = useResource<{
		items?: Version[];
		revisions?: Version[];
		older_revisions_before?: number | null;
		next_offset?: number | null;
	}>(
		props.kind === "skills"
			? `${props.resource}/versions${cursor ? `?offset=${cursor}` : ""}`
			: props.resource + (cursor ? `?before_revision_number=${cursor}` : ""),
	);
	const select = (version: Version) =>
		props.kind === "skills" ? version.revision_id : version.revision_number;
	const compare = async (version: Version) => {
		setComparing(true);
		setError(undefined);
		try {
			const [old, current] = await Promise.all([
				request<DocumentView>(`${props.resource}/revisions/${select(version)}`),
				request<DocumentView>(props.resource),
			]);
			const text = async (doc: DocumentView) => {
				const file = doc.revision.files.find(
					(f) => f.path === doc.revision.primary_file && f.editable,
				);
				return file
					? readText(file)
					: t("history.thisRevisionHasNoComparableTextEntry");
			};
			setComparison({ old: await text(old), current: await text(current) });
		} catch (error) {
			setError(error);
		} finally {
			setComparing(false);
		}
	};
	return (
		<Panel title={t("history.revisionHistory")}>
			<p className="muted">
				{t("history.restoreCreatesANewRevisionAndPreserves")}
			</p>
			{Boolean(error) && <ErrorNotice error={error} />}{" "}
			{data.isPending ? (
				<Loading />
			) : data.error ? (
				<ErrorNotice error={data.error} />
			) : (
				(data.data?.items || data.data?.revisions || []).map((version) => (
					<div className="list-row" key={version.revision_id}>
						<div>
							<strong>
								{version.stored_title ||
									version.name ||
									t("directory.untitled")}{" "}
								· v{version.revision_number}
							</strong>
							<p className="muted">
								<LocalTime value={version.created_at} />
							</p>
						</div>
						<div className="actions">
							<button
								type="button"
								className="secondary"
								onClick={() =>
									props.go(`/${props.resource}/revisions/${select(version)}`)
								}
							>
								{t("history.view")}
							</button>
							<button
								type="button"
								className="secondary"
								disabled={comparing}
								onClick={() => compare(version)}
							>
								{t("history.compareWithCurrent")}
							</button>
							<button
								type="button"
								disabled={restoring || restoreUnknown}
								onClick={() => {
									setError(undefined);
									setRestore(version);
								}}
							>
								{t("history.restore")}
							</button>
						</div>
					</div>
				))
			)}
			{(data.data?.older_revisions_before || data.data?.next_offset) !=
				null && (
				<button
					type="button"
					className="secondary"
					onClick={() =>
						setCursor(
							data.data?.older_revisions_before ?? data.data?.next_offset ?? "",
						)
					}
				>
					{t("history.olderRevisions")}
				</button>
			)}
			<Modal
				open={restore !== null}
				onOpenChange={(value) =>
					!value && !restoring && !restoreUnknown && setRestore(null)
				}
				title={t("history.restoreThisRevision")}
			>
				<p>{t("history.allFilesInThisRevisionWillBecome")}</p>
				{restoreUnknown && (
					<p className="alert" role="status">
						{t("history.restoreOutcomeUnknown")}
					</p>
				)}
				<button
					type="button"
					disabled={restoring}
					onClick={async () => {
						if (!restore) return;
						setRestoring(true);
						setError(undefined);
						try {
							if (!restoreOperation.current) {
								const current = await request<DocumentView>(props.resource);
								restoreOperation.current = {
									path: `${props.resource}/revisions/${select(restore)}/restore`,
									body: JSON.stringify({
										expected_etag: current.current_etag,
										confirm_restore: "yes",
									}),
									key: crypto.randomUUID().replaceAll("-", ""),
								};
								try {
									sessionStorage.setItem(
										storageKey,
										JSON.stringify({
											...restoreOperation.current,
											version: restore,
										}),
									);
								} catch {}
							}
							const operation = restoreOperation.current;
							await request(operation.path, {
								method: "POST",
								body: operation.body,
								headers: { "Idempotency-Key": operation.key },
							});
							restoreOperation.current = null;
							clearReceipt();
							setRestoreUnknown(false);
							setRestore(null);
							props.done(t("history.restoredAsANewRevision"));
							props.go(`/${props.resource}`);
						} catch (error) {
							setError(error);
							if (
								error instanceof Error &&
								"status" in error &&
								Number(error.status) < 500
							) {
								restoreOperation.current = null;
								clearReceipt();
								setRestoreUnknown(false);
							} else setRestoreUnknown(restoreOperation.current !== null);
						} finally {
							setRestoring(false);
						}
					}}
				>
					{t("history.restore2")}
				</button>
				{Boolean(error) && <ErrorNotice error={error} />}
			</Modal>
			<Modal
				open={comparison !== null}
				onOpenChange={(value) => !value && setComparison(null)}
				title={t("history.compareWithCurrentRevision")}
			>
				{comparison && (
					<>
						<p className="muted">
							{t("history.comparesEachRevisionSExplicitTextEntry")}
						</p>
						<pre className="revision-diff">
							{diffLines(comparison.old, comparison.current).map((part) =>
								part.added ? (
									<ins key={part.value}>{part.value}</ins>
								) : part.removed ? (
									<del key={part.value}>{part.value}</del>
								) : (
									<span key={part.value}>{part.value}</span>
								),
							)}
						</pre>
						{comparison.old === comparison.current && (
							<Empty>{t("history.entryTextIsIdentical")}</Empty>
						)}
					</>
				)}
			</Modal>
		</Panel>
	);
}
