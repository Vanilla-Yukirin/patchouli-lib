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
): RestoreReceipt | "unreadable" | null {
	try {
		const raw = sessionStorage.getItem(storageKey);
		if (raw === null) return null;
		const receipt = JSON.parse(raw) as RestoreReceipt | null;
		if (
			!receipt?.version ||
			typeof receipt.version.revision_id !== "string" ||
			!/^rev_[a-f0-9]{32}$/.test(receipt.version.revision_id) ||
			!Number.isSafeInteger(receipt.version.revision_number) ||
			receipt.version.revision_number < 1 ||
			!Number.isSafeInteger(receipt.version.created_at) ||
			typeof receipt.key !== "string" ||
			!/^[a-f0-9]{32}$/.test(receipt.key) ||
			typeof receipt.body !== "string"
		)
			return "unreadable";
		const target =
			kind === "skills"
				? receipt.version.revision_id
				: receipt.version.revision_number;
		if (!target || receipt.path !== `${resource}/revisions/${target}/restore`)
			return "unreadable";
		const body = JSON.parse(receipt.body);
		if (
			!body ||
			typeof body !== "object" ||
			Array.isArray(body) ||
			Object.keys(body).length !== 2 ||
			body.confirm_restore !== "yes" ||
			typeof body.expected_etag !== "string" ||
			!/^"[^"\r\n]+"$/.test(body.expected_etag)
		)
			return "unreadable";
		return receipt;
	} catch {
		return "unreadable";
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
	const originalReceipt =
		initialReceipt && initialReceipt !== "unreadable" ? initialReceipt : null;
	const [restore, setRestore] = useState<Version | null>(
		originalReceipt?.version ?? null,
	);
	const [restoring, setRestoring] = useState(false);
	const [restoreUnknown, setRestoreUnknown] = useState(
		originalReceipt !== null,
	);
	const [receiptBlocked, setReceiptBlocked] = useState<
		"workspace.retryDetailsUnavailable" | "workspace.retryClearFailed" | null
	>(
		initialReceipt === "unreadable"
			? "workspace.retryDetailsUnavailable"
			: null,
	);
	const restoreOperation = useRef<RestoreReceipt | null>(originalReceipt);
	const unusedOperation = useRef<RestoreReceipt | null>(null);
	const clearReceipt = () => {
		try {
			sessionStorage.removeItem(storageKey);
			if (sessionStorage.getItem(storageKey) !== null) throw new Error();
			return true;
		} catch {
			setError(new Error(t("workspace.retryClearFailed")));
			return false;
		}
	};
	const abandonRestore = () => {
		if (!confirm(t("workspace.haveYouCheckedTheCurrentRevisionAnd"))) return;
		if (!clearReceipt()) return;
		restoreOperation.current = null;
		unusedOperation.current = null;
		setReceiptBlocked(null);
		setRestoreUnknown(false);
		setRestore(null);
		setError(undefined);
		data.refetch();
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
			{receiptBlocked && !restore && (
				<div className="alert" role="status">
					<p>{t(receiptBlocked)}</p>
					<button type="button" disabled={restoring} onClick={abandonRestore}>
						{t("workspace.checkedClearReminder")}
					</button>
				</div>
			)}
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
								disabled={
									restoring || restoreUnknown || Boolean(receiptBlocked)
								}
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
				onOpenChange={(value) => {
					if (value || restoring || restoreUnknown || receiptBlocked) return;
					if (unusedOperation.current && !clearReceipt()) return;
					unusedOperation.current = null;
					setRestore(null);
					setError(undefined);
				}}
				title={t("history.restoreThisRevision")}
			>
				<p>{t("history.allFilesInThisRevisionWillBecome")}</p>
				{restoreUnknown && (
					<p className="alert" role="status">
						{t("history.restoreOutcomeUnknown")}
					</p>
				)}
				{receiptBlocked && <p className="alert">{t(receiptBlocked)}</p>}
				{(restoreUnknown || receiptBlocked) && (
					<button type="button" disabled={restoring} onClick={abandonRestore}>
						{t("workspace.checkedClearReminder")}
					</button>
				)}
				<button
					type="button"
					disabled={restoring || Boolean(receiptBlocked)}
					onClick={async () => {
						if (!restore || receiptBlocked) return;
						setRestoring(true);
						setError(undefined);
						let sent = false;
						const retrying = restoreOperation.current !== null;
						try {
							let operation =
								restoreOperation.current || unusedOperation.current;
							const stored = readRestoreReceipt(
								storageKey,
								props.resource,
								props.kind,
							);
							if (stored === "unreadable") {
								setReceiptBlocked("workspace.retryDetailsUnavailable");
								throw new Error(t("workspace.retryDetailsUnavailable"));
							}
							if (
								stored &&
								(stored.key !== operation?.key ||
									stored.path !== operation?.path ||
									stored.body !== operation?.body)
							) {
								restoreOperation.current = stored;
								unusedOperation.current = null;
								setRestore(stored.version);
								setRestoreUnknown(true);
								throw new Error(t("workspace.retryDetailsUnavailable"));
							}
							if (!operation) {
								const current = await request<DocumentView>(props.resource);
								operation = {
									path: `${props.resource}/revisions/${select(restore)}/restore`,
									body: JSON.stringify({
										expected_etag: current.current_etag,
										confirm_restore: "yes",
									}),
									key: crypto.randomUUID().replaceAll("-", ""),
									version: restore,
								};
								unusedOperation.current = operation;
							}
							try {
								const serialized = JSON.stringify(operation);
								sessionStorage.setItem(storageKey, serialized);
								if (sessionStorage.getItem(storageKey) !== serialized)
									throw new Error();
							} catch {
								throw new Error(t("workspace.retryStorageUnavailable"));
							}
							restoreOperation.current = operation;
							unusedOperation.current = null;
							sent = true;
							const result = await request<{
								revision_id: string;
								revision_number: number;
								skill?: { revision_id: string; revision_number: number };
								changed: boolean;
								replayed?: boolean;
							}>(operation.path, {
								method: "POST",
								body: operation.body,
								headers: { "Idempotency-Key": operation.key },
							});
							const saved = result.skill || result;
							if (
								typeof saved.revision_id !== "string" ||
								!/^rev_[a-f0-9]{32}$/.test(saved.revision_id) ||
								!Number.isSafeInteger(saved.revision_number) ||
								saved.revision_number < 1
							)
								throw new Error("restore_receipt_invalid");
							const message =
								retrying || result.replayed
									? t("workspace.retryConfirmed")
									: result.changed
										? t("history.restoredAsANewRevision")
										: t("workspace.identicalContentNoNewRevision");
							props.done(
								`${message} v${saved.revision_number} · ${saved.revision_id}`,
							);
							setRestoreUnknown(false);
							setRestore(null);
							if (!clearReceipt()) {
								setReceiptBlocked("workspace.retryClearFailed");
								return;
							}
							restoreOperation.current = null;
							props.go(
								`/${props.resource}/revisions/${
									props.kind === "skills"
										? saved.revision_id
										: saved.revision_number
								}`,
							);
						} catch (error) {
							setError(error);
							if (
								sent &&
								!retrying &&
								error instanceof Error &&
								"status" in error &&
								Number(error.status) !== 401 &&
								Number(error.status) < 500
							) {
								setRestoreUnknown(false);
								if (clearReceipt()) restoreOperation.current = null;
								else {
									setReceiptBlocked("workspace.retryClearFailed");
									setRestore(null);
								}
							} else if (sent) setRestoreUnknown(true);
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
