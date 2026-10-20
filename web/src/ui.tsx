import * as Dialog from "@radix-ui/react-dialog";
import { LoaderCircle, X } from "lucide-react";
import type { ReactNode } from "react";
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
	return (
		<Dialog.Root open={open} onOpenChange={onOpenChange}>
			<Dialog.Portal>
				<Dialog.Overlay className="overlay" />
				<Dialog.Content
					className={drawer ? "dialog drawer" : "dialog"}
					aria-describedby={undefined}
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
