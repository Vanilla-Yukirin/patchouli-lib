import { Copy, Eye, KeyRound, Plus, RefreshCw } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { request, type Structure } from "./api";
import type { PageProps } from "./app";
import { items, useResource } from "./pages";
import { LocalTime, usePreferences } from "./preferences";
import { ErrorNotice, Loading, Modal, Panel } from "./ui";

type Caller = {
	id: string;
	name: string;
	description?: string;
	library_id: string;
	updated_at: number;
	disabled_at: number | null;
	credentials?: Credential[];
};
type Credential = {
	id: string;
	expires_at: number | null;
	revoked_at: number | null;
	rotated_at: number | null;
	read_mode: "include" | "exclude";
	write_mode: "include" | "exclude";
	scope_revision: number;
	library_grants: { library_id: string; action: string }[];
};
type Scope = {
	read_mode: "include" | "exclude";
	write_mode: "include" | "exclude";
	read_libraries: string[];
	write_libraries: string[];
};
const emptyScope: Scope = {
	read_mode: "include",
	write_mode: "include",
	read_libraries: [],
	write_libraries: [],
};
function ScopeFields({
	value,
	onChange,
	libraries,
}: {
	value: Scope;
	onChange: (scope: Scope) => void;
	libraries: Structure[];
}) {
	const { t } = usePreferences();
	return (
		<div className="scope-grid">
			{(["read", "write"] as const).map((action) => (
				<fieldset className="scope-editor" key={action}>
					<legend>
						{action === "read"
							? t("credentials.readScope")
							: t("credentials.writeScope")}
					</legend>
					<label>
						<select
							value={value[`${action}_mode`]}
							onChange={(event) =>
								onChange({ ...value, [`${action}_mode`]: event.target.value })
							}
						>
							<option value="include">
								{t("credentials.onlySelectedLibraries")}
							</option>
							<option value="exclude">{t("credentials.allLibraries")}</option>
						</select>
					</label>
					<p className="muted">
						{value[`${action}_mode`] === "exclude"
							? t(
									"credentials.includesFutureLibrariesSelectedLibrariesBelowAre",
								)
							: t("credentials.onlyTheLibrariesSelectedBelowAreAllowed")}
					</p>
					<div className="scope-options">
						{libraries.map((library) => (
							<label key={library.id}>
								<input
									type="checkbox"
									checked={value[`${action}_libraries`].includes(library.id)}
									onChange={(event) =>
										onChange({
											...value,
											[`${action}_libraries`]: event.target.checked
												? [...value[`${action}_libraries`], library.id]
												: value[`${action}_libraries`].filter(
														(id) => id !== library.id,
													),
										})
									}
								/>
								<span
									className={
										value[`${action}_mode`] === "exclude"
											? "scope-excluded"
											: ""
									}
								>
									{library.name}
								</span>
							</label>
						))}
					</div>
					<button
						type="button"
						className="secondary"
						onClick={() => onChange({ ...value, [`${action}_libraries`]: [] })}
					>
						{t("credentials.clearSelection")}
					</button>
				</fieldset>
			))}
		</div>
	);
}
function Lifetime({
	unlimited,
	setUnlimited,
}: {
	unlimited: boolean;
	setUnlimited: (value: boolean) => void;
}) {
	const { t } = usePreferences();
	return (
		<>
			<label className="checkbox-label">
				<input
					type="checkbox"
					checked={unlimited}
					onChange={(event) => setUnlimited(event.target.checked)}
				/>
				{t("credentials.neverExpires")}
			</label>
			{!unlimited && (
				<label>
					{t("credentials.lifetime")}
					<select name="credential_ttl_seconds" defaultValue="31536000">
						<option value="86400">{t("credentials.oneDay")}</option>
						<option value="2592000">{t("credentials.30Days")}</option>
						<option value="31536000">{t("credentials.oneYear")}</option>
						<option value="315360000">{t("credentials.tenYears")}</option>
					</select>
				</label>
			)}
		</>
	);
}
export function CredentialManager(
	props: PageProps & { credentialId?: string },
) {
	const { t } = usePreferences();
	const callers = useResource<{ items: Caller[] }>("credentials");
	const libraries = useResource<{ items: Structure[] }>("libraries");
	const match = items(callers.data).find(
		(caller) => caller.id === props.credentialId,
	);
	const selected = useResource<Caller>(
		props.credentialId
			? "credentials/" +
					props.credentialId +
					"?library_id=" +
					(match?.library_id || "")
			: "credentials",
		Boolean(props.credentialId && match),
	);
	const [create, setCreate] = useState(false);
	const [scope, setScope] = useState<Scope>(emptyScope);
	const [unlimited, setUnlimited] = useState(false);
	const [scopeEdit, setScopeEdit] = useState<Credential | null>(null);
	const [rotate, setRotate] = useState<Credential | null>(null);
	const [metadata, setMetadata] = useState(false);
	const [error, setError] = useState<unknown>();
	const [busy, setBusy] = useState(false);
	const [ticket, setTicket] = useState<{
		code: string;
		expires_at: string;
		origin: string;
		remaining_ms: number;
	} | null>(null);
	const [now, setNow] = useState(performance.now());
	const [deadline, setDeadline] = useState(0);
	const [tokenOpen, setTokenOpen] = useState(false);
	const secret = useRef("");
	const output = useRef<HTMLTextAreaElement>(null);
	useEffect(() => {
		if (tokenOpen && output.current) {
			output.current.value = secret.current;
			secret.current = "";
		}
		return () => {
			if (output.current) output.current.value = "";
		};
	}, [tokenOpen]);
	useEffect(() => {
		if (!ticket) return;
		const interval = window.setInterval(() => setNow(performance.now()), 1000);
		return () => clearInterval(interval);
	}, [ticket]);
	const showToken = (value: string) => {
		secret.current = value;
		setTokenOpen(true);
	};
	const perform = async (path: string, body: unknown, method = "POST") => {
		setError(undefined);
		setBusy(true);
		try {
			const result = await request<{ token?: string }>(path, {
				method,
				body: JSON.stringify(body),
			});
			if (result?.token) showToken(result.token);
			props.done(t("credentials.actionCompleted"));
			return true;
		} catch (error) {
			setError(error);
			return false;
		} finally {
			setBusy(false);
		}
	};
	const caller =
		selected.data && "credentials" in selected.data ? selected.data : undefined;
	const credentialPath = (credential: Credential) =>
		`credentials/${caller?.id}/${credential.id}`;
	const suffix = `?library_id=${caller?.library_id}`;
	const openScope = (credential: Credential) => {
		setScope({
			read_mode: credential.read_mode,
			write_mode: credential.write_mode,
			read_libraries: credential.library_grants
				.filter((grant) => grant.action === "read")
				.map((grant) => grant.library_id),
			write_libraries: credential.library_grants
				.filter((grant) => grant.action === "write")
				.map((grant) => grant.library_id),
		});
		setScopeEdit(credential);
	};
	const ticketText = t(
		"credentials.connectToPatchoulilibTicketExpiresOneRedemption",
		{ p0: ticket?.origin, p1: ticket?.code, p2: ticket?.expires_at },
	);
	return (
		<>
			<div className="page-heading">
				<div>
					<p className="eyebrow">ACCESS</p>
					<h1>{caller?.name || t("app.credentials")}</h1>
					<p className="muted">
						{t("credentials.nameScopeAndRotateEveryCredentialIndependently")}
					</p>
				</div>
				<div className="actions">
					<button
						type="button"
						className="secondary"
						onClick={async () => {
							try {
								const issued = await request<NonNullable<typeof ticket>>(
									"onboarding-tickets",
									{
										method: "POST",
										body: "{}",
									},
								);
								setTicket(issued);
								setDeadline(performance.now() + issued.remaining_ms);
								setNow(performance.now());
							} catch (error) {
								setError(error);
							}
						}}
					>
						{t("credentials.onboardingTicket")}
					</button>
					<button
						type="button"
						onClick={() => {
							setScope(emptyScope);
							setUnlimited(false);
							setCreate(true);
						}}
					>
						<Plus size={16} />
						{t("credentials.createCredential")}
					</button>
				</div>
			</div>
			{Boolean(error) && <ErrorNotice error={error} />}
			{props.credentialId && selected.error && (
				<ErrorNotice error={selected.error} />
			)}
			{caller ? (
				<>
					<Panel
						title={t("credentials.credentialIdentity")}
						actions={
							<button
								type="button"
								className="secondary"
								onClick={() => setMetadata(true)}
							>
								{t("credentials.editNameAndDescription")}
							</button>
						}
					>
						<p>{caller.description}</p>
						{caller.disabled_at && (
							<p className="alert">{t("credentials.disabled")}</p>
						)}
					</Panel>
					<Panel title={t("credentials.tokensAndGrants")}>
						{caller.credentials?.map((credential) => (
							<div className="credential-card" key={credential.id}>
								<p>
									<KeyRound size={16} />{" "}
									{credential.expires_at ? (
										<LocalTime value={credential.expires_at} />
									) : (
										t("credentials.neverExpires")
									)}
								</p>
								{credential.revoked_at || credential.rotated_at ? (
									<p className="muted">{t("credentials.inactive")}</p>
								) : (
									<div className="actions wrap">
										<button
											type="button"
											className="secondary"
											onClick={() => openScope(credential)}
										>
											{t("credentials.readWriteScopes")}
										</button>
										<button
											type="button"
											className="secondary"
											onClick={() =>
												perform(
													`${credentialPath(credential)}/reveal${suffix}`,
													{},
												)
											}
										>
											<Eye size={16} />
											{t("credentials.revealToken")}
										</button>
										<button
											type="button"
											className="secondary"
											onClick={() => {
												setUnlimited(false);
												setRotate(credential);
											}}
										>
											<RefreshCw size={16} />
											{t("credentials.rotate")}
										</button>
										<button
											type="button"
											className="danger"
											onClick={() =>
												confirm(t("credentials.revokeThisTokenImmediately")) &&
												perform(
													`${credentialPath(credential)}/revoke${suffix}`,
													{},
												)
											}
										>
											{t("credentials.revoke")}
										</button>
									</div>
								)}
							</div>
						))}
						<button
							type="button"
							className="danger"
							onClick={() =>
								confirm(t("credentials.disableEveryTokenForThisIdentity")) &&
								perform(`credentials/${caller.id}/disable${suffix}`, {})
							}
						>
							{t("credentials.disableIdentity")}
						</button>
					</Panel>
				</>
			) : (
				<Panel title={t("credentials.credentialIdentities")}>
					{callers.isPending ? (
						<Loading />
					) : callers.error ? (
						<ErrorNotice error={callers.error} />
					) : (
						items(callers.data).map((item) => (
							<button
								type="button"
								key={item.id}
								className="list-row"
								onClick={() => props.go(`/credentials/${item.id}`)}
							>
								<div>
									<strong>{item.name}</strong>
									<p className="muted">{item.description}</p>
								</div>
								<span>
									{item.disabled_at
										? t("credentials.disabled")
										: t("app.manage")}
								</span>
							</button>
						))
					)}
				</Panel>
			)}
			<Modal
				open={create}
				onOpenChange={setCreate}
				title={t("credentials.createCredential")}
			>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						const fields = Object.fromEntries(
							new FormData(event.currentTarget),
						);
						if (
							await perform("credentials", {
								...fields,
								...scope,
								never_expires: unlimited,
								credential_ttl_seconds: unlimited
									? undefined
									: Number(fields.credential_ttl_seconds),
							})
						)
							setCreate(false);
					}}
				>
					<label>
						{t("credentials.credentialName")}
						<input name="agent_name" required />
					</label>
					<label>
						{t("credentials.description")}
						<textarea name="agent_description" />
					</label>
					<Lifetime unlimited={unlimited} setUnlimited={setUnlimited} />
					<ScopeFields
						value={scope}
						onChange={setScope}
						libraries={items(libraries.data)}
					/>
					<button type="submit" disabled={busy}>
						{t("credentials.create")}
					</button>
				</form>
			</Modal>
			<Modal
				open={Boolean(scopeEdit)}
				onOpenChange={(open) => !open && setScopeEdit(null)}
				title={t("credentials.editReadWriteScopes")}
			>
				<ScopeFields
					value={scope}
					onChange={setScope}
					libraries={items(libraries.data)}
				/>
				<button
					type="button"
					disabled={busy}
					onClick={async () => {
						if (
							scopeEdit &&
							(await perform(
								`${credentialPath(scopeEdit)}/scope${suffix}`,
								{ ...scope, expected_scope_revision: scopeEdit.scope_revision },
								"PUT",
							))
						)
							setScopeEdit(null);
					}}
				>
					{t("credentials.saveGrants")}
				</button>
			</Modal>
			<Modal
				open={Boolean(rotate)}
				onOpenChange={(open) => !open && setRotate(null)}
				title={t("credentials.rotateToken")}
			>
				<p className="alert">
					{t("credentials.theOldTokenStopsWorkingImmediatelyUpdate")}
				</p>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						const fields = new FormData(event.currentTarget);
						if (
							rotate &&
							(await perform(`${credentialPath(rotate)}/rotate${suffix}`, {
								never_expires: unlimited,
								credential_ttl_seconds: unlimited
									? undefined
									: Number(fields.get("credential_ttl_seconds")),
							}))
						)
							setRotate(null);
					}}
				>
					<Lifetime unlimited={unlimited} setUnlimited={setUnlimited} />
					<button type="submit" disabled={busy}>
						{t("credentials.rotate2")}
					</button>
				</form>
			</Modal>
			<Modal
				open={metadata}
				onOpenChange={setMetadata}
				title={t("credentials.nameAndDescription")}
			>
				<form
					onSubmit={async (event) => {
						event.preventDefault();
						if (
							caller &&
							(await perform(
								`credentials/${caller.id}${suffix}`,
								{
									...Object.fromEntries(new FormData(event.currentTarget)),
									expected_updated_at: caller.updated_at,
								},
								"PUT",
							))
						)
							setMetadata(false);
					}}
				>
					<label>
						{t("credentials.name")}
						<input name="name" defaultValue={caller?.name} required />
					</label>
					<label>
						{t("credentials.description")}
						<textarea name="description" defaultValue={caller?.description} />
					</label>
					<button type="submit">{t("credentials.save")}</button>
				</form>
			</Modal>
			<Modal
				open={tokenOpen}
				onOpenChange={setTokenOpen}
				title={t("credentials.token")}
				drawer
			>
				<p className="alert">
					{t("credentials.saveInPrivateDeviceStorageDoNot")}
				</p>
				<textarea ref={output} readOnly autoComplete="off" spellCheck={false} />
				<button
					type="button"
					onClick={() =>
						output.current &&
						navigator.clipboard.writeText(output.current.value)
					}
				>
					<Copy size={16} />
					{t("credentials.copy")}
				</button>
			</Modal>
			<Modal
				open={Boolean(ticket)}
				onOpenChange={(open) => !open && setTicket(null)}
				title={t("credentials.onboardingTicket2")}
				drawer
			>
				{ticket && (
					<>
						<p className="ticket-code">{ticket.code}</p>
						<p>
							{Math.max(0, Math.ceil((deadline - now) / 1000))}{" "}
							{t("credentials.secondsRemaining")}
						</p>
						<button
							type="button"
							disabled={deadline <= now}
							onClick={() => navigator.clipboard.writeText(ticketText)}
						>
							<Copy size={16} />
							{t("credentials.copyConnectionInstructions")}
						</button>
						<button
							type="button"
							className="secondary"
							disabled={deadline <= now}
							onClick={() => navigator.clipboard.writeText(ticket.code)}
						>
							{t("credentials.copyCodeOnly")}
						</button>
						<details>
							<summary>{t("credentials.instructionPreview")}</summary>
							<pre>{ticketText}</pre>
						</details>
					</>
				)}
			</Modal>
		</>
	);
}
