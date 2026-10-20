import * as Dialog from "@radix-ui/react-dialog";
import { LoaderCircle, X } from "lucide-react";
import { type ReactNode, useEffect, useRef } from "react";
import { usePreferences } from "./preferences";
export function Panel({
	title,
	children,
	actions,
}: {
	title: string;
	children: ReactNode;
	actions?: ReactNode;
}) {
	return (
		<section className="panel">
			<div className="panel-heading">
				<h2>{title}</h2>
				{actions}
			</div>
			{children}
		</section>
	);
}
export function Empty({ children }: { children: ReactNode }) {
	return <div className="empty">{children}</div>;
}
export function Loading() {
	const { t } = usePreferences();
	return (
		<div role="status" className="loading">
			<LoaderCircle className="spin" />
			{t("ui.loading")}
		</div>
	);
}
export function ErrorNotice({ error }: { error: unknown }) {
	const { t } = usePreferences();
	return (
		<p className="alert" role="alert">
			{t("ui.theActionCouldNotBeCompletedPlease")}{" "}
			<code>{error instanceof Error ? error.message : ""}</code>
		</p>
	);
}
export function LocalDatePreview({ value }: { value: string }) {
	const { t } = usePreferences();
	const date = value ? new Date(value) : null;
	const utc = date && !Number.isNaN(date.getTime()) ? date.toISOString() : null;
	return (
		<span className="date-preview muted" aria-live="polite">
			<span>
				{t("ui.browserTimezone")}:{" "}
				{Intl.DateTimeFormat().resolvedOptions().timeZone}
			</span>
			<span>
				{t("ui.utcToSubmit")}:{" "}
				{utc ? <time dateTime={utc}>{utc}</time> : t("ui.utcAfterChoosingTime")}
			</span>
		</span>
	);
}
export function Modal({
	open,
	onOpenChange,
	title,
	children,
	drawer = false,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	title: string;
	children: ReactNode;
	drawer?: boolean;
}) {
	const { t } = usePreferences();
	const returnFocus = useRef<HTMLElement | null>(null);
	const openingLocation = useRef("");
	useEffect(() => {
		if (open) return;
		const remember = (target: Element | null) => {
			returnFocus.current =
				target instanceof HTMLElement && target !== document.body
					? target
					: null;
			openingLocation.current = location.href;
		};
		const rememberPointer = (event: PointerEvent) => {
			remember(
				event.target instanceof Element
					? event.target.closest<HTMLElement>(
							"button, a[href], input, select, textarea, [tabindex]",
						)
					: null,
			);
		};
		const rememberKeyboard = () => {
			remember(document.activeElement);
		};
		document.addEventListener("pointerdown", rememberPointer, true);
		document.addEventListener("keydown", rememberKeyboard, true);
		return () => {
			document.removeEventListener("pointerdown", rememberPointer, true);
			document.removeEventListener("keydown", rememberKeyboard, true);
		};
	}, [open]);
	return (
		<Dialog.Root open={open} onOpenChange={onOpenChange}>
			<Dialog.Portal>
				<Dialog.Overlay className="overlay" />
				<Dialog.Content
					className={drawer ? "dialog drawer" : "dialog"}
					aria-describedby={undefined}
					onOpenAutoFocus={() => {
						if (
							openingLocation.current !== location.href ||
							!returnFocus.current?.isConnected
						) {
							const active = document.activeElement;
							returnFocus.current =
								active instanceof HTMLElement && active !== document.body
									? active
									: null;
							openingLocation.current = location.href;
						}
					}}
					onCloseAutoFocus={(event) => {
						event.preventDefault();
						const target = returnFocus.current;
						const active = document.activeElement;
						if (
							openingLocation.current === location.href &&
							target?.isConnected &&
							!target.matches(':disabled, [aria-disabled="true"]') &&
							!target.closest("[inert], [hidden]") &&
							target.getClientRects().length > 0 &&
							getComputedStyle(target).visibility === "visible" &&
							(active === document.body ||
								(event.target instanceof HTMLElement &&
									event.target.contains(active)))
						)
							target.focus({ preventScroll: true });
					}}
				>
					<div className="panel-heading">
						<Dialog.Title>{title}</Dialog.Title>
						<Dialog.Close className="icon-button" aria-label={t("ui.close")}>
							<X size={20} />
						</Dialog.Close>
					</div>
					{children}
				</Dialog.Content>
			</Dialog.Portal>
		</Dialog.Root>
	);
}
